"""FastMCP server exposing legacy-reverse-mcp tools.

DB resolution order for read tools:
  1. the repo most recently passed to scan_repository in this process
  2. the LEGACY_REVERSE_REPO environment variable
The index lives at ``<repo>/.reverse/index.sqlite3``.
"""

from __future__ import annotations

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


def _resolve_repo() -> Path:
    global _active_repo
    if _active_repo is not None:
        return _active_repo
    env = os.environ.get("LEGACY_REVERSE_REPO")
    if env:
        return Path(env).resolve()
    raise RuntimeError(
        "No repository indexed. Call scan_repository(repo_path=...) first "
        "or set LEGACY_REVERSE_REPO."
    )


def _db_path(repo: Path | None = None) -> Path:
    repo = repo or _resolve_repo()
    return repo / _DB_RELATIVE


def _read_conn():
    db = _db_path()
    if not db.exists():
        raise RuntimeError(f"Index not found at {db}. Run scan_repository first.")
    return get_conn(db)


@mcp.tool()
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

    conn = init_db(db)
    summary = build_index(conn, str(repo))
    conn.close()
    _active_repo = repo
    return {"status": "scanned", "db_path": str(db), **summary}


@mcp.tool()
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
def explain_class(fqn: str) -> dict:
    """Explain a class as observed facts + inferred findings (each with evidence,
    confidence) + related symbols + limitations. Accepts FQN or simple name."""
    conn = _read_conn()
    try:
        return _explain_class(conn, fqn)
    finally:
        conn.close()


@mcp.tool()
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
def get_project_overview() -> dict:
    """High-level overview: stack, totals, role distribution, top modules, findings."""
    conn = _read_conn()
    try:
        result = queries.project_overview(conn)
    finally:
        conn.close()
    return meta(result, confidence="medium", limitation_codes=["spring_proxies", "no_call_graph"])


@mcp.tool()
def find_code_areas(query: str, limit: int = 20) -> dict:
    """Keyword search over classes, methods and endpoints, grouped by kind."""
    conn = _read_conn()
    try:
        result = queries.find_code_areas(conn, query, limit)
    finally:
        conn.close()
    return meta(result, confidence="medium", limitation_codes=["ambiguous_simple_name"])


@mcp.tool()
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
def get_module_map() -> dict:
    """Module graph: modules with inter-module deps, external coordinates, endpoint counts."""
    conn = _read_conn()
    try:
        result = queries.module_map(conn)
    finally:
        conn.close()
    return meta(result, confidence="high", limitation_codes=["external_types_unresolved"])


@mcp.tool()
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
