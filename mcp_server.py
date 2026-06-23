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
from index.repository import get_conn, init_db
from index.queries import class_detail
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
    return {"count": len(rows), "endpoints": rows}


@mcp.tool()
def explain_class(fqn: str) -> dict:
    """Explain a class: role, annotations, injected deps, methods, endpoints. Accepts FQN or simple name."""
    conn = _read_conn()
    try:
        detail = class_detail(conn, fqn)
    finally:
        conn.close()
    if detail is None:
        return {"error": f"class not found: {fqn}"}
    return detail


@mcp.tool()
def trace_endpoint(endpoint_id: int) -> dict:
    """Heuristic controller -> service -> repository/persistence trace for an endpoint id."""
    conn = _read_conn()
    try:
        trace = queries.trace_endpoint(conn, endpoint_id)
    finally:
        conn.close()
    if trace is None:
        return {"error": f"endpoint not found: {endpoint_id}"}
    return trace


@mcp.tool()
def get_project_overview() -> dict:
    raise NotImplementedError


@mcp.tool()
def find_code_areas(query: str) -> dict:
    raise NotImplementedError


@mcp.tool()
def get_module_map() -> dict:
    """Module graph: modules with inter-module deps, external coordinates, endpoint counts."""
    conn = _read_conn()
    try:
        return queries.module_map(conn)
    finally:
        conn.close()


@mcp.tool()
def get_change_impact(symbol: str) -> dict:
    raise NotImplementedError


@mcp.tool()
def generate_context_pack(task: str, max_tokens: int = 4000) -> dict:
    raise NotImplementedError


if __name__ == "__main__":
    mcp.run()
