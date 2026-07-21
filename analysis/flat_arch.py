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

Class matching: by ``id`` (repo-relative source path -> ``class.file_path``) first,
then ``fqn = pkg + "." + name``, then simple name. Method matching:
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
# Java method modifiers are reserved words, always lowercase — so the membership
# check must be case-sensitive: `Final`/`Static` are legal method names and must
# not be mistaken for modifiers.
_MODIFIERS = frozenset((
    "public", "private", "protected", "static", "final", "abstract",
    "synchronized", "native", "default", "strictfp",
))


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
    """('createDeposit', ['DepositRequest']) from 'createDeposit(DepositRequest req): Deposit'.

    A foreign ``sig`` may carry leading Java modifiers and a return type before the
    name (``public static void main(String[] args)``): the method name is the last
    token before ``(`` that isn't a modifier, not the whole prefix. Our own export /
    index stores a clean ``name(types): ret`` form, so this only bites when consuming
    an externally produced arch.json — but there a naive prefix grab would mis-name
    the method and drop its description on the floor."""
    if "(" not in sig:
        return sig.strip(), []
    before_paren = sig[: sig.index("(")].strip()
    tokens = before_paren.split()
    name = tokens[-1] if tokens else before_paren  # last token = name (return type precedes it)
    if name in _MODIFIERS and len(tokens) > 1:
        name = tokens[-2]
    inner = sig[sig.index("(") + 1 : sig.rfind(")")] if ")" in sig else sig[sig.index("(") + 1 :]
    inner = _GENERIC.sub("", inner).strip()
    if not inner:
        return name, []
    params: list[str] = []
    for raw in inner.split(","):
        parts = raw.strip().split()
        # 'Type name' -> Type (drop trailing identifier); 'Type' -> Type
        type_str = " ".join(parts[:-1]) if len(parts) >= 2 else (parts[0] if parts else "")
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


def _normalize_id(cid: str) -> str:
    """A flat ``id`` is an extension-less repo-relative source path (see _flat_id);
    tolerate the cosmetic mutations a model/export may introduce (``.java`` suffix,
    backslashes, ``./`` prefix) so it lines up with ``class.file_path``."""
    cid = cid.replace("\\", "/").strip()
    while cid.startswith("./"):
        cid = cid[2:]
    return cid[:-5] if cid.endswith(".java") else cid


def _resolve_class_row(conn: sqlite3.Connection, entry: dict) -> sqlite3.Row | None:
    """Resolve a flat-JSON class entry to an index class row.

    Priority is ``id`` first: it maps to ``class.file_path`` (repo-relative source
    path) and is the most reliable key — gigacode's architecture-generator returns
    a correct ``id`` even when it drops ``pkg`` entirely, which would otherwise make
    a whole class unmatchable. Falls back to ``pkg+name`` (fqn), then simple name."""
    cid = entry.get("id")
    name = entry.get("name")
    if cid:
        want = _normalize_id(str(cid)) + ".java"
        rows = conn.execute(
            "SELECT id, fqn, simple_name FROM class WHERE file_path = ?", (want,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1 and name:  # several classes in one file: disambiguate by name
            for r in rows:
                if r["simple_name"] == name:
                    return r

    fqn = entry.get("fqn") or _entry_fqn(entry)
    if fqn:
        row = conn.execute("SELECT id, fqn FROM class WHERE fqn = ?", (fqn,)).fetchone()
        if row is not None:
            return row
    if name:
        # last-resort fallback: only trust a UNIQUE simple name. Silently picking
        # the first of several same-named classes would write the description to
        # the wrong class AND persist it in the durable imported store under the
        # wrong fqn (surviving even scan --force).
        rows = conn.execute(
            "SELECT id, fqn FROM class WHERE simple_name = ? ORDER BY fqn LIMIT 2", (name,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


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
            row = _resolve_class_row(conn, entry)
            if row is None:
                if len(stats["unmatched_classes"]) < 25:
                    stats["unmatched_classes"].append(
                        entry.get("fqn") or _entry_fqn(entry) or str(entry.get("id") or "")
                    )
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
