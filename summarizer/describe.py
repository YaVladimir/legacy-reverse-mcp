"""Phase 2, Step 2: meaningful natural-language descriptions for classes, methods
and the package/module/project hierarchy.

This is an *offline* step, decoupled from ``scan``: it reads the already-built
index, asks a pluggable LLM (see :mod:`summarizer.llm`) for concise descriptions
of *what each class/method does and why*, and falls back to deterministic text
when the LLM is disabled or fails. Results are denormalised into
``class.summary`` / ``method.summary`` (so cards and FTS pick them up) and the
hierarchy goes into the ``summary`` table.

A durable content-hash cache lives in a **separate** sqlite file
(``.reverse/descriptions.sqlite3``) that ``scan`` never deletes, so re-scans do
not re-spend the LLM budget on unchanged code (manifest Step 5, incremental).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from index import repository as repo
from index.search import build_search_index
from summarizer.class_summary import render_class_summary
from summarizer.llm import LLMClient

# Bump when the prompt or skeleton shape changes, to invalidate cached entries.
PROMPT_VERSION = "1"

_CACHE_RELATIVE = Path(".reverse") / "descriptions.sqlite3"
_WHOLE_FILE_MAX_LINES = 200
_METHOD_BODY_HEAD = 12
_SNIPPET_MAX_CHARS = 6000

# verb prefix -> action phrase, for the deterministic method fallback (ru).
_VERB_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("get", "find", "list", "load", "read", "fetch", "query", "search", "is", "has", "can", "should"), "возвращает данные"),
    (("create", "add", "save", "insert", "register", "open", "new", "build", "make"), "создаёт/сохраняет сущность"),
    (("update", "edit", "change", "modify", "set", "patch", "apply"), "изменяет состояние"),
    (("delete", "remove", "close", "cancel", "revoke", "drop"), "удаляет/закрывает сущность"),
    (("validate", "check", "verify", "ensure", "assert"), "проверяет условия/валидацию"),
    (("process", "handle", "execute", "run", "perform", "calc", "calculate", "compute"), "выполняет обработку/расчёт"),
    (("send", "publish", "notify", "emit", "dispatch"), "отправляет сообщение/событие"),
    (("map", "convert", "to", "from", "parse", "serialize", "deserialize"), "преобразует данные"),
]


# ------------------------------------------------------------
# durable description cache (separate sqlite file, survives re-scans)
# ------------------------------------------------------------

def _open_cache(repo_path: str | Path) -> sqlite3.Connection:
    path = Path(repo_path) / _CACHE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS description_cache (
            ref_key      TEXT NOT NULL PRIMARY KEY,
            content_hash TEXT NOT NULL,
            content      TEXT NOT NULL,
            model        TEXT,
            lang         TEXT,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Imported descriptions (e.g. from a gigacode architecture-generator flat JSON).
    # These take priority over LLM/fallback in describe, and survive re-scans —
    # but only while ``content_hash`` still matches the class's current
    # ``structure_hash`` (NULL = legacy import, always considered valid).
    # ref_key: "<fqn>" for a class, "<fqn>#<canonical_signature>" for a method.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_description (
            ref_key      TEXT NOT NULL PRIMARY KEY,
            kind         TEXT NOT NULL,   -- 'class' | 'method'
            content      TEXT NOT NULL,
            source       TEXT,            -- e.g. 'gigacode' | 'flat-json'
            content_hash TEXT,            -- structure_hash of the class at import time
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # migration for caches created before content_hash existed
    cols = {r[1] for r in conn.execute("PRAGMA table_info(imported_description)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE imported_description ADD COLUMN content_hash TEXT")
    conn.commit()
    return conn


def set_imported(
    cache: sqlite3.Connection,
    fqn: str,
    *,
    class_text: str | None,
    methods: dict[str, str] | None = None,
    source: str = "flat-json",
    content_hash: str | None = None,
    commit: bool = True,
) -> None:
    """Persist imported class/method descriptions. ``methods`` maps a method's
    canonical ``signature`` (the index column) to its description text.
    ``content_hash`` is the class's :func:`structure_hash` at import time; later
    ``describe`` runs ignore these rows once the hash stops matching."""
    if class_text:
        cache.execute(
            "INSERT INTO imported_description (ref_key, kind, content, source, content_hash) "
            "VALUES (?, 'class', ?, ?, ?) "
            "ON CONFLICT(ref_key) DO UPDATE SET content=excluded.content, source=excluded.source, "
            "content_hash=excluded.content_hash, created_at=CURRENT_TIMESTAMP",
            (fqn, class_text, source, content_hash),
        )
    for signature, text in (methods or {}).items():
        if not text:
            continue
        cache.execute(
            "INSERT INTO imported_description (ref_key, kind, content, source, content_hash) "
            "VALUES (?, 'method', ?, ?, ?) "
            "ON CONFLICT(ref_key) DO UPDATE SET content=excluded.content, source=excluded.source, "
            "content_hash=excluded.content_hash, created_at=CURRENT_TIMESTAMP",
            (f"{fqn}#{signature}", text, source, content_hash),
        )
    if commit:
        cache.commit()


def imported_for_class(
    cache: sqlite3.Connection, fqn: str
) -> tuple[str | None, dict[str, str], str | None]:
    """Return (class_description_or_None, {canonical_signature: method_description},
    stored_content_hash_or_None) for any imported descriptions of this class.

    The freshness hash is taken from the *class* row deterministically (the method
    rows share it at import time, but a partial re-import could leave them
    disagreeing — picking an arbitrary row without ORDER BY made staleness a
    coin-flip). Falls back to any method row's hash only when there's no class row."""
    class_text: str | None = None
    methods: dict[str, str] = {}
    class_hash: str | None = None
    any_hash: str | None = None
    prefix = f"{fqn}#"
    plen = len(prefix)
    for r in cache.execute(
        "SELECT ref_key, kind, content, content_hash FROM imported_description "
        "WHERE ref_key = ? OR substr(ref_key, 1, ?) = ?",
        (fqn, plen, prefix),
    ):
        if r["kind"] == "class" and r["ref_key"] == fqn:
            class_text = r["content"]
            class_hash = r["content_hash"]
        elif r["kind"] == "method" and r["ref_key"].startswith(prefix):
            methods[r["ref_key"][plen:]] = r["content"]
        if any_hash is None and r["content_hash"]:
            any_hash = r["content_hash"]
    stored_hash = class_hash if class_hash is not None else any_hash
    return class_text, methods, stored_hash


def reapply_imported(conn: sqlite3.Connection, repo_path: str | Path) -> dict:
    """Re-apply descriptions from the durable imported store to a freshly built index.

    A forced rescan rebuilds ``index.sqlite3`` from scratch, wiping the
    ``class.summary``/``method.summary`` values a previous import/``describe`` had
    applied — while ``descriptions.sqlite3`` survives. Called at the end of the
    scan pipeline so imported descriptions don't silently vanish until the next
    manual ``describe``/``import-arch`` run. No LLM involved; imports whose stored
    :func:`structure_hash` no longer matches the current code are skipped, same
    freshness rule as ``describe`` itself.
    """
    stats = {"classes": 0, "methods": 0, "stale": 0, "unmatched": 0}
    cache_path = Path(repo_path) / _CACHE_RELATIVE
    if not cache_path.exists():
        return stats
    repo_root = Path(repo_path).resolve()
    # _open_cache carries the schema migration (older caches lack content_hash); a
    # raw connect + SELECT content_hash would raise mid-scan. A corrupt/non-sqlite
    # file also raises here — a best-effort restore stage must never fail the scan,
    # so degrade to a warning in stats and leave the (rebuildable) summaries empty.
    try:
        cache = _open_cache(repo_path)
    except sqlite3.DatabaseError as exc:
        stats["error"] = f"description cache unreadable, skipped restore: {exc}"
        return stats
    try:
        # single pass over the imported rows, grouped by class fqn (was one indexed
        # scan of the whole table per class — O(classes x rows)).
        by_fqn: dict[str, dict] = {}
        for r in cache.execute(
            "SELECT ref_key, kind, content, content_hash FROM imported_description"
        ):
            fqn = r["ref_key"].split("#", 1)[0]
            g = by_fqn.setdefault(
                fqn, {"class_text": None, "class_hash": None, "methods": {}, "any_hash": None}
            )
            if r["kind"] == "class" and r["ref_key"] == fqn:
                g["class_text"] = r["content"]
                g["class_hash"] = r["content_hash"]
            elif r["kind"] == "method" and r["ref_key"].startswith(f"{fqn}#"):
                g["methods"][r["ref_key"][len(fqn) + 1:]] = r["content"]
            if g["any_hash"] is None and r["content_hash"]:
                g["any_hash"] = r["content_hash"]

        for fqn, g in sorted(by_fqn.items()):
            row = conn.execute("SELECT id FROM class WHERE fqn = ?", (fqn,)).fetchone()
            if row is None:
                stats["unmatched"] += 1
                continue
            # freshness judged by the class row's hash (see imported_for_class)
            stored_hash = g["class_hash"] if g["class_hash"] is not None else g["any_hash"]
            skeleton = _class_skeleton(conn, row["id"])
            if stored_hash is not None and stored_hash != structure_hash(
                skeleton, _source_snippet(skeleton, repo_root)
            ):
                stats["stale"] += 1
                continue
            if g["class_text"]:
                repo.set_class_summary(conn, row["id"], g["class_text"], commit=False)
                stats["classes"] += 1
            sig_to_id = {m["signature"]: m["id"] for m in skeleton["methods"]}
            for signature, text in g["methods"].items():
                method_id = sig_to_id.get(signature)
                if method_id is not None:
                    repo.set_method_summary(conn, method_id, text, commit=False)
                    stats["methods"] += 1
        conn.commit()
    except sqlite3.DatabaseError as exc:
        stats["error"] = f"description cache read failed, restore incomplete: {exc}"
    finally:
        cache.close()
    return stats


def _cache_get(cache: sqlite3.Connection, ref_key: str, content_hash: str) -> dict | None:
    row = cache.execute(
        "SELECT content, content_hash FROM description_cache WHERE ref_key = ?", (ref_key,)
    ).fetchone()
    if row is None or row["content_hash"] != content_hash:
        return None
    try:
        return json.loads(row["content"])
    except json.JSONDecodeError:
        return None


def _cache_put(
    cache: sqlite3.Connection, ref_key: str, content_hash: str, content: dict, model: str, lang: str
) -> None:
    cache.execute(
        "INSERT INTO description_cache (ref_key, content_hash, content, model, lang) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(ref_key) DO UPDATE SET content_hash=excluded.content_hash, "
        "content=excluded.content, model=excluded.model, lang=excluded.lang, "
        "created_at=CURRENT_TIMESTAMP",
        (ref_key, content_hash, json.dumps(content, ensure_ascii=False), model, lang),
    )


# ------------------------------------------------------------
# skeleton extraction (from the index)
# ------------------------------------------------------------

def _pretty_sig(name: str, params: list[sqlite3.Row], return_type: str | None) -> str:
    """`createDeposit(DepositRequest req): Deposit` — type + param name, like the reference JSON."""
    parts = []
    for p in params:
        t = p["type_fqn"] or "?"
        parts.append(f"{t} {p['name']}" if p["name"] else t)
    sig = f"{name}({', '.join(parts)})"
    if return_type:
        sig += f": {return_type}"
    return sig


def _class_skeleton(conn: sqlite3.Connection, class_id: int) -> dict:
    cls = conn.execute("SELECT * FROM class WHERE id = ?", (class_id,)).fetchone()
    pkg = conn.execute(
        "SELECT p.fqn AS pkg, mo.name AS module FROM class cl "
        "LEFT JOIN package p ON p.id = cl.package_id "
        "LEFT JOIN module mo ON mo.id = cl.module_id WHERE cl.id = ?",
        (class_id,),
    ).fetchone()
    annotations = [r["name"] for r in conn.execute(
        "SELECT name FROM class_annotation WHERE class_id = ?", (class_id,))]
    interfaces = [r["interface_fqn"] for r in conn.execute(
        "SELECT interface_fqn FROM class_interface WHERE class_id = ?", (class_id,))]
    fields = [
        {"name": r["name"], "type": r["type_fqn"], "injected": bool(r["is_injected"])}
        for r in conn.execute(
            "SELECT name, type_fqn, is_injected FROM field WHERE class_id = ? ORDER BY name", (class_id,))
    ]
    methods = []
    for m in conn.execute(
        "SELECT id, name, signature, return_type, visibility, is_static, is_constructor, "
        "line_start, line_end FROM method WHERE class_id = ? ORDER BY line_start", (class_id,)
    ):
        params = conn.execute(
            "SELECT name, type_fqn FROM method_parameter WHERE method_id = ? ORDER BY position", (m["id"],)
        ).fetchall()
        anns = [r["name"] for r in conn.execute(
            "SELECT name FROM method_annotation WHERE method_id = ?", (m["id"],))]
        methods.append({
            "id": m["id"],
            "name": m["name"],
            "signature": m["signature"],
            "sig_pretty": _pretty_sig(m["name"], params, m["return_type"]),
            "modifiers": " ".join(filter(None, [m["visibility"], "static" if m["is_static"] else ""])).strip(),
            "annotations": anns,
            "is_constructor": bool(m["is_constructor"]),
            "line_start": m["line_start"],
            "line_end": m["line_end"],
        })
    endpoints = [
        {"http_method": r["http_method"], "path": r["full_path"], "handler": r["handler_name"]}
        for r in conn.execute(
            "SELECT http_method, full_path, handler_name FROM v_endpoint_full "
            "WHERE controller_fqn = ? ORDER BY full_path", (cls["fqn"],))
    ]
    return {
        "fqn": cls["fqn"],
        "simple_name": cls["simple_name"],
        "kind": cls["kind"],
        "role": cls["role"],
        "package": pkg["pkg"] if pkg else None,
        "module": pkg["module"] if pkg else None,
        "extends": cls["superclass_fqn"],
        "implements": interfaces,
        "annotations": annotations,
        "fields": fields,
        "methods": methods,
        "endpoints": endpoints,
        "file_path": cls["file_path"],
        "line_start": cls["line_start"],
        "line_end": cls["line_end"],
    }


def _source_snippet(skeleton: dict, repo_root: Path) -> str:
    """Per the manifest: small files whole; large files = headers + first lines of
    each method body. Bounded so a 3B model is not flooded. ``file_path`` on the
    skeleton is repo-relative; resolve it against ``repo_root`` to actually read it."""
    path = skeleton.get("file_path")
    if not path:
        return ""
    full_path = Path(path)
    if not full_path.is_absolute():
        full_path = repo_root / full_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if len(lines) <= _WHOLE_FILE_MAX_LINES:
        return "\n".join(lines)[:_SNIPPET_MAX_CHARS]

    picked: list[str] = []
    cstart = skeleton.get("line_start") or 1
    picked.extend(lines[cstart - 1: cstart + 2])  # class header
    for m in skeleton["methods"]:
        start = m.get("line_start")
        if not start:
            picked.append(f"    // {m['sig_pretty']}")
            continue
        end = m.get("line_end") or start
        head_end = min(end, start + _METHOD_BODY_HEAD)
        picked.append("")
        picked.extend(lines[start - 1: head_end])  # signature + first lines of the body
    text = "\n".join(picked)
    return text[:_SNIPPET_MAX_CHARS]


def _stable_projection(skeleton: dict) -> dict:
    """Names/signatures/annotations only. Volatile fields (db ids, line numbers,
    file path) are excluded so a mere line shift does not invalidate anything;
    real structure/body changes do (they change the projection or ``snippet``)."""
    return {
        "fqn": skeleton["fqn"],
        "kind": skeleton["kind"],
        "role": skeleton["role"],
        "extends": skeleton["extends"],
        "implements": skeleton["implements"],
        "annotations": skeleton["annotations"],
        "fields": skeleton["fields"],
        "methods": [
            {"signature": m["signature"], "modifiers": m["modifiers"], "annotations": m["annotations"]}
            for m in skeleton["methods"]
        ],
        "endpoints": skeleton["endpoints"],
    }


def structure_hash(skeleton: dict, snippet: str) -> str:
    """Model/lang-independent hash of the class structure + source snippet. Stored
    alongside *imported* descriptions so they stop winning once the class changes."""
    payload = json.dumps(
        {"skeleton": _stable_projection(skeleton), "snippet": snippet},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _class_hash(skeleton: dict, snippet: str, model: str, lang: str) -> str:
    """Cache key for LLM-generated descriptions: the stable projection + snippet,
    plus model/lang/prompt-version (a different model or language must regenerate)."""
    payload = json.dumps(
        {"skeleton": _stable_projection(skeleton), "snippet": snippet,
         "model": model, "lang": lang, "v": PROMPT_VERSION},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# prompt + parsing
# ------------------------------------------------------------

def _system_prompt(lang: str) -> str:
    if lang.startswith("ru"):
        return (
            "Ты — старший Java/Spring инженер, документируешь банковский бэкенд. "
            "По структуре и фрагменту кода опиши КРАТКО и ПО СУЩЕСТВУ, что делает класс "
            "и каждый его метод и ЗАЧЕМ (бизнес-смысл, а не пересказ сигнатуры). "
            "Пиши на русском. Верни СТРОГО один JSON-объект без markdown и пояснений: "
            '{\"class\": \"<1-3 предложения>\", \"methods\": {\"<сигнатура>\": \"<1-2 предложения>\"}}. '
            "Ключи methods — РОВНО те сигнатуры, что даны во входных данных. "
            "Если смысл неясен — опиши по имени и типам, ничего не выдумывай."
        )
    return (
        "You are a senior Java/Spring engineer documenting a banking backend. "
        "From the structure and code snippet, concisely describe what the class and each "
        "method do and WHY (business meaning, not a restatement of the signature). "
        "Return STRICTLY one JSON object, no markdown, no preamble: "
        '{\"class\": \"<1-3 sentences>\", \"methods\": {\"<signature>\": \"<1-2 sentences>\"}}. '
        "The methods keys must be EXACTLY the signatures given in the input. "
        "If meaning is unclear, describe from name and types; do not invent."
    )


def _user_prompt(skeleton: dict, snippet: str) -> str:
    lines = [
        f"Класс: {skeleton['simple_name']} (kind={skeleton['kind']}, role={skeleton['role']})",
        f"Пакет: {skeleton['package']}  Модуль: {skeleton['module']}",
    ]
    if skeleton["annotations"]:
        lines.append(f"Аннотации: {', '.join(skeleton['annotations'])}")
    if skeleton["extends"]:
        lines.append(f"extends: {skeleton['extends']}")
    if skeleton["implements"]:
        lines.append(f"implements: {', '.join(skeleton['implements'])}")
    if skeleton["fields"]:
        flds = ", ".join(f"{f['name']}:{f['type']}" for f in skeleton["fields"][:20])
        lines.append(f"Поля: {flds}")
    if skeleton["endpoints"]:
        eps = "; ".join(f"{e['http_method']} {e['path']}" for e in skeleton["endpoints"][:15])
        lines.append(f"Endpoints: {eps}")
    lines.append("Сигнатуры методов (используй их как ключи):")
    for m in skeleton["methods"]:
        ann = f" [{', '.join(m['annotations'])}]" if m["annotations"] else ""
        lines.append(f"  - {m['signature']}{ann}")
    if snippet:
        lines.append("\nФрагмент исходника:\n```java\n" + snippet + "\n```")
    return "\n".join(lines)


def _parse_class_json(text: str) -> dict | None:
    """Tolerant: extract the first {...} block and parse it; validate shape."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "class" not in obj:
        return None
    methods = obj.get("methods")
    if not isinstance(methods, dict):
        obj["methods"] = {}
    return obj


# ------------------------------------------------------------
# deterministic fallbacks
# ------------------------------------------------------------

def _method_fallback(method: dict, skeleton: dict) -> str:
    if method["is_constructor"]:
        return f"Конструктор {skeleton['simple_name']}."
    action = "выполняет операцию"
    low = method["name"].lower()
    for prefixes, phrase in _VERB_HINTS:
        if any(low.startswith(p) for p in prefixes):
            action = phrase
            break
    ann = f" Аннотации: {', '.join(method['annotations'])}." if method["annotations"] else ""
    return f"Метод `{method['sig_pretty']}` {action}.{ann}".strip()


def _class_fallback(conn: sqlite3.Connection, class_id: int, skeleton: dict) -> dict:
    endpoints = [{"http_method": e["http_method"], "full_path": e["path"]} for e in skeleton["endpoints"]]
    injected = [f["name"] for f in skeleton["fields"] if f["injected"]]
    class_text = render_class_summary(
        simple_name=skeleton["simple_name"],
        role=skeleton["role"],
        kind=skeleton["kind"],
        module=skeleton["module"],
        endpoints=endpoints,
        injected=injected,
        method_count=len(skeleton["methods"]),
    )
    methods = {m["signature"]: _method_fallback(m, skeleton) for m in skeleton["methods"]}
    return {"class": class_text, "methods": methods}


# ------------------------------------------------------------
# per-class description
# ------------------------------------------------------------

def _describe_class(
    conn: sqlite3.Connection,
    cache: sqlite3.Connection,
    class_id: int,
    llm: LLMClient,
    *,
    force: bool,
    stats: dict,
    repo_root: Path,
) -> None:
    skeleton = _class_skeleton(conn, class_id)
    snippet = _source_snippet(skeleton, repo_root)
    model = llm.describe()
    lang = llm.config.lang
    ref_key = skeleton["fqn"]

    # Imported descriptions (e.g. a gigacode architecture-generator flat JSON) win
    # over LLM/fallback and survive re-scans — but only while the class hasn't
    # structurally changed since the import (stale imports would confidently
    # describe old behaviour, which is worse than a bland fallback).
    imp_class, imp_methods, imp_hash = imported_for_class(cache, ref_key)
    imports_fresh = imp_hash is None or imp_hash == structure_hash(skeleton, snippet)
    if not imports_fresh and (imp_class is not None or imp_methods):
        stats["stale_imported"] += 1
        imp_class, imp_methods = None, {}
    if imp_class is not None:
        result = {"class": imp_class, "methods": dict(imp_methods)}
        stats["from_imported"] += 1
    else:
        h = _class_hash(skeleton, snippet, model, lang)
        result = None if force else _cache_get(cache, ref_key, h)
        if result is not None:
            stats["from_cache"] += 1
        else:
            result = None
            if llm.enabled:
                raw = llm.complete(system=_system_prompt(lang), user=_user_prompt(skeleton, snippet))
                result = _parse_class_json(raw) if raw else None
                if result is not None:
                    stats["from_llm"] += 1
            if result is None:
                result = _class_fallback(conn, class_id, skeleton)
                stats["from_fallback"] += 1
            _cache_put(cache, ref_key, h, result, model, lang)
        # a class with no imported class-text may still have per-method imports
        if imp_methods:
            result.setdefault("methods", {}).update(imp_methods)

    # denormalise into class.summary / method.summary
    repo.set_class_summary(conn, class_id, result["class"], commit=False)
    stats["classes"] += 1
    llm_methods = result.get("methods", {})
    for m in skeleton["methods"]:
        text = llm_methods.get(m["signature"]) or _method_fallback(m, skeleton)
        repo.set_method_summary(conn, m["id"], text, commit=False)
        stats["methods"] += 1


# ------------------------------------------------------------
# hierarchy (package / module / project) — deterministic aggregation,
# now enriched with the generated class descriptions
# ------------------------------------------------------------

def _describe_hierarchy(conn: sqlite3.Connection, llm: LLMClient, stats: dict) -> None:
    repo.clear_summaries(conn, kind="package", commit=False)
    repo.clear_summaries(conn, kind="module", commit=False)
    repo.clear_summaries(conn, kind="project", commit=False)
    model = llm.describe()

    for p in conn.execute("SELECT id, fqn FROM package"):
        rows = conn.execute(
            "SELECT simple_name, role, summary FROM class WHERE package_id = ? ORDER BY role, simple_name",
            (p["id"],),
        ).fetchall()
        if not rows:
            continue
        roles: dict[str, int] = {}
        for r in rows:
            roles[r["role"]] = roles.get(r["role"], 0) + 1
        dist = ", ".join(f"{n} {role}" for role, n in sorted(roles.items(), key=lambda t: -t[1]))
        notable = [f"{r['simple_name']} — {r['summary']}" for r in rows
                   if r["role"] in ("controller", "service")][:5]
        text = f"Пакет `{p['fqn']}`: {len(rows)} класс(ов) ({dist})."
        if notable:
            text += " Ключевые: " + " | ".join(notable)
        repo.insert_summary(conn, "package", p["id"], text, model="deterministic", commit=False)
        stats["packages"] += 1

    for m in conn.execute("SELECT id, name FROM module"):
        rows = conn.execute(
            "SELECT simple_name, role, summary FROM class WHERE module_id = ? ORDER BY role, simple_name",
            (m["id"],),
        ).fetchall()
        if not rows:
            continue
        roles = {}
        for r in rows:
            roles[r["role"]] = roles.get(r["role"], 0) + 1
        dist = ", ".join(f"{n} {role}" for role, n in sorted(roles.items(), key=lambda t: -t[1]))
        notable = [f"{r['simple_name']}: {r['summary']}" for r in rows
                   if r["role"] in ("controller", "service")][:8]
        base = f"Модуль `{m['name']}`: {len(rows)} класс(ов) ({dist})."
        text = base + (" " + " | ".join(notable) if notable else "")
        if llm.enabled and notable:
            polished = llm.complete(
                system=_system_prompt(llm.config.lang),
                user="Сформулируй 2-3 предложения о назначении этого модуля по сводке "
                     "(верни обычный текст, без JSON):\n" + text,
            )
            if polished:
                text = polished.strip()
        repo.insert_summary(conn, "module", m["id"], text, model=model, commit=False)
        stats["modules"] += 1

    manifest = conn.execute("SELECT * FROM scan_manifest ORDER BY id DESC LIMIT 1").fetchone()
    totals = conn.execute("SELECT COUNT(*) n FROM class").fetchone()["n"]
    module_names = [r["name"] for r in conn.execute("SELECT name FROM module ORDER BY name")]
    proj = (
        f"Проект `{Path(manifest['repo_path']).name if manifest else 'repo'}`: "
        f"{totals} класс(ов), {len(module_names)} модул(ей) "
        f"({manifest['build_tool'] if manifest else '?'}). Модули: {', '.join(module_names[:20])}."
    )
    if llm.enabled:
        polished = llm.complete(
            system=_system_prompt(llm.config.lang),
            user="Кратко (3-5 предложений) опиши архитектуру и назначение проекта по сводке "
                 "(обычный текст, без JSON):\n" + proj,
        )
        if polished:
            proj = polished.strip()
    repo.insert_summary(conn, "project", None, proj, model=model, commit=False)
    stats["project"] = 1


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------

def describe_repo(
    conn: sqlite3.Connection,
    repo_path: str,
    *,
    force: bool = False,
    use_llm: bool = True,
    progress=None,
    progress_every: int = 200,
) -> dict:
    """Generate class/method/hierarchy descriptions over an already-built index."""
    echo = progress or (lambda _m: None)
    llm = LLMClient()
    if not use_llm:
        llm.config.base_url = None  # force deterministic fallback

    mode = f"LLM={llm.config.model}" if llm.enabled else "deterministic fallback (no LLM endpoint)"
    echo(f"Describing repository with {mode} ...")

    repo_root = Path(repo_path).resolve()
    cache = _open_cache(repo_path)
    stats = {
        "classes": 0, "methods": 0, "packages": 0, "modules": 0, "project": 0,
        "from_cache": 0, "from_llm": 0, "from_fallback": 0, "from_imported": 0,
        "stale_imported": 0,
        "llm_enabled": llm.enabled, "model": llm.describe(),
    }
    try:
        class_ids = [r["id"] for r in conn.execute("SELECT id FROM class ORDER BY fqn")]
        for i, class_id in enumerate(class_ids, 1):
            _describe_class(conn, cache, class_id, llm, force=force, stats=stats, repo_root=repo_root)
            if progress_every and i % progress_every == 0:
                conn.commit()
                cache.commit()
                echo(f"  ... {i}/{len(class_ids)} classes "
                     f"(llm={stats['from_llm']}, cache={stats['from_cache']}, fallback={stats['from_fallback']})")
        echo("Aggregating package/module/project descriptions ...")
        _describe_hierarchy(conn, llm, stats)
        conn.commit()
        cache.commit()

        echo("Rebuilding search index ...")
        stats["search_rows"] = build_search_index(conn)
    finally:
        cache.close()

    echo(
        f"Described {stats['classes']} class(es), {stats['methods']} method(s); "
        f"hierarchy: {stats['packages']} package(s), {stats['modules']} module(s). "
        f"Sources: imported={stats['from_imported']}, llm={stats['from_llm']}, "
        f"cache={stats['from_cache']}, fallback={stats['from_fallback']}"
        + (f"; stale imports ignored: {stats['stale_imported']}" if stats["stale_imported"] else "")
        + "."
    )
    return stats
