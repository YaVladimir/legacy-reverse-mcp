"""Flat architecture JSON: export from the index and import back in.

The *flat* schema mirrors the reference produced by the GigaCode
``architecture-generator`` skill (``project_architecture_flat.json``):

    {
      "project": "...", "generated_at": "YYYY-MM-DD", "total_classes": N,
      "classes": [
        { "id", "pkg", "name", "description", "type", "kind",
          "class_modifiers": [...], "extends": str|null,
          "methods": [{"sig", "modifiers", "description"}],
          "fields": [{"name", "type"}], "implements": [...]|null }
      ]
    }

- ``export_flat`` renders this from our index (reusing :func:`index.queries.class_detail`),
  so we are a drop-in producer of the same artifact.
- ``import_flat`` reads such a file and loads its descriptions back into the index
  (``class.summary`` / ``method.summary``) and the durable imported-description store,
  so ``find_feature`` / ``get_class_card`` / ``explain_class`` serve them and a later
  ``describe`` keeps them (imported wins over LLM/fallback).

Class matching: by ``fqn = pkg + "." + name`` (fallback: simple name). Method matching:
by name, disambiguating overloads by parameter *type* simple-names (parameter names and
return type are ignored, since the reference ``sig`` is ``name(Type paramName)``).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from index import repository as repo
from index.queries import class_detail
from index.search import build_search_index
from summarizer import describe

_GENERIC = re.compile(r"<[^<>]*>")


# ------------------------------------------------------------
# export
# ------------------------------------------------------------

def _flat_id(file_path: str | None, fqn: str, repo_root: Path) -> str:
    if not file_path:
        return fqn
    p = Path(file_path)
    try:
        rel = p.as_posix() if not p.is_absolute() else p.resolve().relative_to(repo_root).as_posix()
    except (ValueError, OSError):
        return fqn
    return rel[:-5] if rel.endswith(".java") else rel


def _to_flat_class(d: dict, repo_root: Path) -> dict:
    return {
        "id": _flat_id(d.get("file_path"), d["fqn"], repo_root),
        "pkg": d.get("package"),
        "name": d["simple_name"],
        "description": d.get("description") or "",
        "type": d.get("type") or d.get("role"),
        "kind": d["kind"],
        "class_modifiers": d.get("class_modifiers") or [],
        "extends": d.get("extends"),
        "methods": [
            {"sig": m["sig"], "modifiers": m.get("modifiers") or "", "description": m.get("description") or ""}
            for m in d.get("methods", [])
        ],
        "fields": [
            {"name": f["name"], "type": f.get("type_fqn")} for f in d.get("fields", [])
        ],
        "implements": d.get("implements") or None,
    }


def export_flat(conn: sqlite3.Connection, repo_path: str) -> dict:
    """Render the whole index as a flat architecture dict (reference schema)."""
    repo_root = Path(repo_path).resolve()
    classes = []
    for row in conn.execute("SELECT fqn FROM class ORDER BY fqn"):
        d = class_detail(conn, row["fqn"])
        if d is not None:
            classes.append(_to_flat_class(d, repo_root))

    manifest = conn.execute(
        "SELECT repo_path FROM scan_manifest ORDER BY id DESC LIMIT 1"
    ).fetchone()
    project = (
        Path(manifest["repo_path"]).name if manifest and manifest["repo_path"] else repo_root.name
    )
    return {
        "project": project,
        "generated_at": date.today().isoformat(),
        "total_classes": len(classes),
        "classes": classes,
    }


# ------------------------------------------------------------
# import — signature/type normalisation + matching
# ------------------------------------------------------------

def _simple_type(t: str | None) -> str:
    if not t:
        return ""
    t = _GENERIC.sub("", t).strip().rstrip("[]").strip()
    if "." in t:
        t = t.rsplit(".", 1)[-1]
    return t


def _name_and_params(sig: str) -> tuple[str, list[str]]:
    """('createDeposit', ['DepositRequest']) from 'createDeposit(DepositRequest req): Deposit'."""
    if "(" not in sig:
        return sig.strip(), []
    name = sig[: sig.index("(")].strip()
    inner = sig[sig.index("(") + 1 : sig.rfind(")")] if ")" in sig else sig[sig.index("(") + 1 :]
    inner = _GENERIC.sub("", inner).strip()
    if not inner:
        return name, []
    params: list[str] = []
    for raw in inner.split(","):
        tokens = raw.strip().split()
        # 'Type name' -> Type (drop trailing identifier); 'Type' -> Type
        type_str = " ".join(tokens[:-1]) if len(tokens) >= 2 else (tokens[0] if tokens else "")
        params.append(_simple_type(type_str))
    return name, params


def _match_method(db_methods: list[sqlite3.Row], flat_sig: str) -> sqlite3.Row | None:
    name, want = _name_and_params(flat_sig)
    cands = [m for m in db_methods if m["name"] == name]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for m in cands:
        _, have = _name_and_params(m["signature"])
        if have == want:
            return m
    return None  # ambiguous overload with no type match — leave unmatched


def _entry_fqn(entry: dict) -> str:
    pkg, name = entry.get("pkg"), entry.get("name") or ""
    return f"{pkg}.{name}" if pkg else name


def _resolve_class_row(conn: sqlite3.Connection, fqn: str, name: str | None) -> sqlite3.Row | None:
    row = conn.execute("SELECT id, fqn FROM class WHERE fqn = ?", (fqn,)).fetchone()
    if row is None and name:
        row = conn.execute(
            "SELECT id, fqn FROM class WHERE simple_name = ? ORDER BY fqn LIMIT 1", (name,)
        ).fetchone()
    return row


def import_flat(
    conn: sqlite3.Connection, repo_path: str, data: dict, *, source: str = "flat-json"
) -> dict:
    """Load descriptions from a flat architecture dict into the index + imported store."""
    classes = data.get("classes") if isinstance(data, dict) else None
    stats = {
        "classes_total": len(classes) if classes else 0,
        "classes_matched": 0,
        "methods_matched": 0,
        "methods_unmatched": 0,
        "unmatched_classes": [],
    }
    if not classes:
        return stats

    repo_root = Path(repo_path).resolve()
    cache = describe._open_cache(repo_path)
    try:
        for entry in classes:
            if not isinstance(entry, dict):
                continue
            fqn = entry.get("fqn") or _entry_fqn(entry)
            row = _resolve_class_row(conn, fqn, entry.get("name"))
            if row is None:
                if len(stats["unmatched_classes"]) < 25:
                    stats["unmatched_classes"].append(fqn)
                continue
            class_id, class_fqn = row["id"], row["fqn"]

            class_desc = (entry.get("description") or "").strip()
            db_methods = conn.execute(
                "SELECT id, name, signature FROM method WHERE class_id = ?", (class_id,)
            ).fetchall()

            method_map: dict[str, str] = {}  # canonical signature -> description
            for m in entry.get("methods") or []:
                if not isinstance(m, dict):
                    continue
                mdesc = (m.get("description") or "").strip()
                if not mdesc:
                    continue
                hit = _match_method(db_methods, m.get("sig") or "")
                if hit is None:
                    stats["methods_unmatched"] += 1
                    continue
                repo.set_method_summary(conn, hit["id"], mdesc, commit=False)
                method_map[hit["signature"]] = mdesc
                stats["methods_matched"] += 1

            if class_desc:
                repo.set_class_summary(conn, class_id, class_desc, commit=False)
            if class_desc or method_map:
                # structure hash at import time: a later `describe` ignores these rows
                # once the class structurally changes, instead of serving stale text
                skeleton = describe._class_skeleton(conn, class_id)
                snippet = describe._source_snippet(skeleton, repo_root)
                describe.set_imported(
                    cache, class_fqn, class_text=class_desc or None, methods=method_map,
                    source=source, content_hash=describe.structure_hash(skeleton, snippet),
                    commit=False,
                )
            stats["classes_matched"] += 1

        conn.commit()
        cache.commit()
        stats["search_rows"] = build_search_index(conn)
    finally:
        cache.close()
    return stats
