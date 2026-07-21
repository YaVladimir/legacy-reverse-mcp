"""FastMCP server exposing legacy-reverse-mcp tools.

DB resolution order for read tools:
  1. the repo most recently passed to scan_repository in this process
  2. the LEGACY_REVERSE_REPO environment variable
The index lives at ``<repo>/.reverse/index.sqlite3``.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from fastmcp import FastMCP

from index import queries
from index.repository import get_conn, init_db, list_inferred_findings
from analysis.common import meta
from analysis.explain import explain_class as _explain_class
from scanner.pipeline import build_index

mcp = FastMCP("legacy-reverse-mcp")

_DB_RELATIVE = Path(".reverse") / "index.sqlite3"
_active_repo: Path | None = None


class _IndexUnavailable(RuntimeError):
    """Raised by the repo/db resolution helpers; carries the structured payload a
    tool must return instead of letting a bare exception hit the MCP transport
    (docs/mcp-api.md: errors are structured dicts with suggestions, never raises)."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("message", "index unavailable"))
        self.payload = payload


def _structured_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _IndexUnavailable as exc:
            return exc.payload
    return wrapper


def _resolve_repo() -> Path:
    global _active_repo
    if _active_repo is not None:
        return _active_repo
    env = os.environ.get("LEGACY_REVERSE_REPO")
    if env:
        return Path(env).resolve()
    raise _IndexUnavailable({
        "error": "no_repository",
        "kind": "repository",
        "message": "No repository indexed in this process and LEGACY_REVERSE_REPO is not set.",
        "suggestions": [
            "Call scan_repository(repo_path=...) first",
            "Or set the LEGACY_REVERSE_REPO environment variable",
        ],
    })


def _db_path(repo: Path | None = None) -> Path:
    repo = repo or _resolve_repo()
    return repo / _DB_RELATIVE


def _require_db(db: Path) -> None:
    if not db.exists():
        raise _IndexUnavailable({
            "error": "no_index",
            "kind": "index",
            "message": f"Index not found at {db}.",
            "suggestions": ["Run scan_repository(repo_path=...) to build the index"],
        })


def _read_conn():
    db = _db_path()
    _require_db(db)
    return get_conn(db)


@mcp.tool()
@_structured_errors
def scan_repository(repo_path: str, force: bool = False) -> dict:
    """Scan a Java/Spring repo and (re)build its .reverse index."""
    global _active_repo
    repo = Path(repo_path).resolve()
    db = repo / _DB_RELATIVE
    if db.exists() and not force:
        _active_repo = repo
        return {"status": "exists", "db_path": str(db), "hint": "pass force=true to rebuild"}
    if db.exists():
        db.unlink()
        # a WAL-mode db leaves sidecars next to it; a stale pair from a previous
        # process would be re-attached to the freshly created file
        for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if sidecar.exists():
                sidecar.unlink()

    conn = init_db(db)
    try:
        summary = build_index(conn, str(repo))
    finally:
        # without this, a failed build leaves the WAL connection open and (on
        # Windows) the half-built index file locked until the server restarts
        conn.close()
    _active_repo = repo
    return {"status": "scanned", "db_path": str(db), **summary}


@mcp.tool()
@_structured_errors
def list_endpoints(http_method: str | None = None, path_contains: str | None = None, limit: int = 200) -> dict:
    """List REST endpoints (JAX-RS + Spring), optionally filtered by verb or path substring."""
    conn = _read_conn()
    try:
        rows = queries.list_endpoints(conn, http_method, path_contains, limit)
    finally:
        conn.close()
    return meta(
        {"count": len(rows), "endpoints": rows},
        confidence="high",  # endpoints are read directly from mapping annotations
        limitation_codes=["dynamic_endpoints"],
    )


@mcp.tool()
@_structured_errors
def explain_class(fqn: str) -> dict:
    """Explain a class as observed facts + inferred findings (each with evidence,
    confidence) + related symbols + limitations. Accepts FQN or simple name."""
    conn = _read_conn()
    try:
        return _explain_class(conn, fqn)
    finally:
        conn.close()


@mcp.tool()
@_structured_errors
def get_class_summary(fqn: str) -> dict:
    """Deterministic one-line summary of a class (role, module, endpoints, injected
    dependencies, method count). Accepts FQN or simple name. The summarize_class
    seam is where an LLM-backed summary can later be swapped in."""
    from analysis.common import not_found
    from summarizer.class_summary import summarize_class

    conn = _read_conn()
    try:
        row = conn.execute(
            "SELECT id, fqn, simple_name FROM class WHERE fqn = ? OR simple_name = ? "
            "ORDER BY fqn LIMIT 1",
            (fqn, fqn),
        ).fetchone()
        if row is None:
            suggestions = [
                {"fqn": s["fqn"], "name": s["simple_name"]}
                for s in conn.execute(
                    "SELECT fqn, simple_name FROM class WHERE simple_name LIKE ? OR fqn LIKE ? "
                    "ORDER BY simple_name LIMIT 5",
                    (f"%{fqn}%", f"%{fqn}%"),
                )
            ]
            return not_found("class", fqn, suggestions)
        summary = summarize_class(conn, row["id"])
    finally:
        conn.close()
    return meta(
        {"fqn": row["fqn"], "name": row["simple_name"], "summary": summary},
        confidence="medium",  # deterministic rendering over a heuristic role
        limitation_codes=["spring_proxies"],
    )


@mcp.tool()
@_structured_errors
def generate_descriptions(force: bool = False, no_llm: bool = False) -> dict:
    """Generate meaningful natural-language descriptions for every class and method
    (and the package/module/project hierarchy) over the already-built index, so the
    other tools can return *what code does and why*, not just its structure.

    Uses a pluggable LLM configured via LEGACY_REVERSE_LLM_* env vars; with no
    endpoint configured (or no_llm=true) it writes deterministic fallback text.
    Results are cached by content hash in .reverse/descriptions.sqlite3 so re-runs
    are cheap. Run this once after scan_repository (it can take a while on big repos)."""
    from summarizer.describe import describe_repo

    repo = _resolve_repo()
    db = _db_path(repo)
    _require_db(db)
    conn = get_conn(db)
    try:
        stats = describe_repo(conn, str(repo), force=force, use_llm=not no_llm)
    finally:
        conn.close()
    return meta(
        {"status": "described", **stats},
        confidence="medium",  # descriptions are model/heuristic text over high-confidence structure
        limitation_codes=["spring_proxies"],
    )


@mcp.tool()
@_structured_errors
def find_feature(topic: str, limit: int = 20, methods_per_class: int = 12) -> dict:
    """Find the classes that implement a feature/topic (e.g. "банкротство / bankruptcy")
    and return each as a compact card with its methods, parameters and descriptions —
    so an agent can act without grep or reading files. Searches class and method names,
    annotations AND generated descriptions (run generate_descriptions first for the best
    recall, especially for business/Russian queries)."""
    conn = _read_conn()
    try:
        result = queries.find_feature(conn, topic, limit=limit, methods_per_class=methods_per_class)
    finally:
        conn.close()
    return meta(
        result,
        confidence="medium",
        limitation_codes=["ambiguous_simple_name", "no_call_graph"],
    )


@mcp.tool()
@_structured_errors
def get_class_card(fqn: str) -> dict:
    """Full structured card for one class (reference-architecture parity): id, pkg,
    name, description, type, kind, class_modifiers, extends, implements, fields and
    methods (each with sig incl. parameter names, modifiers, annotations, description).
    Accepts FQN or simple name. Structured not-found with suggestions otherwise."""
    from analysis.common import not_found

    conn = _read_conn()
    try:
        card = queries.class_card(conn, fqn)
        if card is None:
            suggestions = [
                {"fqn": s["fqn"], "name": s["simple_name"]}
                for s in conn.execute(
                    "SELECT fqn, simple_name FROM class WHERE simple_name LIKE ? OR fqn LIKE ? "
                    "ORDER BY simple_name LIMIT 5",
                    (f"%{fqn}%", f"%{fqn}%"),
                )
            ]
            return not_found("class", fqn, suggestions)
    finally:
        conn.close()
    return meta(card, confidence="medium", limitation_codes=["spring_proxies"])


@mcp.tool()
@_structured_errors
def export_architecture(out_path: str | None = None) -> dict:
    """Render the whole index as a flat architecture JSON — reference-schema parity
    with the GigaCode architecture-generator (per class: id, pkg, name, description,
    type, kind, class_modifiers, extends, implements, fields, methods[{sig, modifiers,
    description}]). With out_path, writes the file and returns a summary; otherwise
    returns the full architecture dict. Run generate_descriptions first to fill descriptions."""
    from analysis.flat_arch import export_flat

    repo = _resolve_repo()
    db = _db_path(repo)
    _require_db(db)
    conn = get_conn(db)
    try:
        data = export_flat(conn, str(repo))
    finally:
        conn.close()
    if out_path:
        Path(out_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta(
            {"status": "written", "out_path": out_path,
             "project": data["project"], "total_classes": data["total_classes"]},
            confidence="high", limitation_codes=["spring_proxies"],
        )
    return meta({"status": "ok", **data}, confidence="high", limitation_codes=["spring_proxies"])


@mcp.tool()
@_structured_errors
def import_architecture(in_path: str) -> dict:
    """Load descriptions from a flat architecture JSON (e.g. produced by the GigaCode
    architecture-generator skill) into the index, so find_feature / get_class_card /
    explain_class serve them and a later describe keeps them (imported wins). Matches
    classes by pkg.name and methods by name (+ parameter types for overloads)."""
    from analysis.flat_arch import import_flat
    from analysis.common import not_found

    repo = _resolve_repo()
    db = _db_path(repo)
    _require_db(db)
    p = Path(in_path)
    if not p.exists():
        return not_found("file", in_path, [])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": "bad_input", "kind": "file", "query": in_path, "message": str(exc), "suggestions": []}
    conn = get_conn(db)
    try:
        stats = import_flat(conn, str(repo), data)
    finally:
        conn.close()
    return meta({"status": "imported", **stats}, confidence="medium", limitation_codes=["spring_proxies"])


@mcp.tool()
@_structured_errors
def generate_architecture() -> dict:
    """Run gigacode-cli's architecture-generator skill and import its flat JSON,
    configured via LEGACY_REVERSE_GIGACODE_* env vars. On failure returns a structured
    error with a hint to run the skill manually and call import_architecture."""
    from summarizer.harness import generate_architecture as _gen

    repo = _resolve_repo()
    db = _db_path(repo)
    _require_db(db)
    conn = get_conn(db)
    try:
        stats = _gen(conn, str(repo))
    finally:
        conn.close()
    conf = "medium" if stats.get("status") == "imported" else "low"
    return meta(stats, confidence=conf, limitation_codes=["spring_proxies"])


@mcp.tool()
@_structured_errors
def trace_endpoint(
    endpoint_id: int | None = None,
    http_method: str | None = None,
    path_contains: str | None = None,
) -> dict:
    """Honest controller -> service -> repository/persistence trace with per-step
    + overall confidence, evidence and limitations. Look up by id, or by
    http_method/path_contains. Structured error (with suggestions) if not found."""
    from analysis.trace import trace_endpoint as _trace

    conn = _read_conn()
    try:
        return _trace(conn, endpoint_id, http_method, path_contains)
    finally:
        conn.close()


@mcp.tool()
@_structured_errors
def get_project_overview() -> dict:
    """High-level overview: stack, totals, role distribution, top modules, findings."""
    conn = _read_conn()
    try:
        result = queries.project_overview(conn)
    finally:
        conn.close()
    return meta(result, confidence="medium", limitation_codes=["spring_proxies", "no_call_graph"])


@mcp.tool()
@_structured_errors
def find_code_areas(query: str, limit: int = 20) -> dict:
    """Keyword search over classes, methods and endpoints, grouped by kind."""
    conn = _read_conn()
    try:
        result = queries.find_code_areas(conn, query, limit)
    finally:
        conn.close()
    return meta(result, confidence="medium", limitation_codes=["ambiguous_simple_name"])


@mcp.tool()
@_structured_errors
def get_findings(subject: str | None = None, finding_type: str | None = None, limit: int = 200) -> dict:
    """Inferred findings persisted during scan (e.g. low-confidence layer guesses
    for classes with no stereotype annotation), each with evidence + confidence."""
    conn = _read_conn()
    try:
        findings = list_inferred_findings(conn, subject=subject, finding_type=finding_type, limit=limit)
    finally:
        conn.close()
    return meta(
        {"count": len(findings), "findings": findings},
        confidence="low",
        limitation_codes=["spring_proxies", "no_call_graph"],
    )


@mcp.tool()
@_structured_errors
def get_config(key_contains: str | None = None, profile: str | None = None, limit: int = 200) -> dict:
    """Spring externalized configuration indexed from application*.{yml,properties}
    and bootstrap*.* — config files (with profile) plus individual properties,
    optionally filtered by key substring or profile. Secret-bearing values
    (password/secret/token/...) are masked. Static read: ${...} placeholders are
    not resolved."""
    from index.repository import list_config_files, list_config_properties

    conn = _read_conn()
    try:
        files = [dict(r) for r in list_config_files(conn)]
        props = list_config_properties(conn, key_contains=key_contains, profile=profile, limit=limit)
    finally:
        conn.close()
    return meta(
        {
            "config_file_count": len(files),
            "files": files,
            "property_count": len(props),
            "properties": props,
        },
        confidence="high",  # values are read verbatim from the config files
        limitation_codes=["config_not_resolved"],
    )


@mcp.tool()
@_structured_errors
def get_module_map() -> dict:
    """Module graph: modules with inter-module deps, external coordinates, endpoint counts."""
    conn = _read_conn()
    try:
        result = queries.module_map(conn)
    finally:
        conn.close()
    return meta(result, confidence="high", limitation_codes=["external_types_unresolved"])


@mcp.tool()
@_structured_errors
def get_change_impact(symbol: str) -> dict:
    """Candidate impact of changing a class, split into direct_impacts (direct
    references) and candidate_impacts (heuristic: endpoints, test names). Each
    impact has reason + evidence + confidence; also returns suggested files."""
    from analysis.impact import change_impact as _impact

    conn = _read_conn()
    try:
        return _impact(conn, symbol)
    finally:
        conn.close()


@mcp.tool()
@_structured_errors
def generate_context_pack(task: str, max_tokens: int = 8000, max_items: int = 20) -> dict:
    """Explainable, task-scoped context pack: selected_items (each with reason,
    confidence, evidence), excluded_items, a context_markdown and limitations."""
    from analysis.context_pack import generate_context_pack as _gen

    conn = _read_conn()
    try:
        return _gen(conn, task, max_tokens, max_items)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
