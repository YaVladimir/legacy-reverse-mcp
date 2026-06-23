"""Deterministic package summaries: aggregate a package's classes by role."""

from __future__ import annotations

import sqlite3
from collections import Counter

from index import repository as repo

_ROLE_ORDER = ["controller", "service", "repository", "entity", "dto", "config", "component", "util", "unknown"]


def _render(fqn: str, module: str | None, roles: Counter, notable: list[str]) -> str:
    total = sum(roles.values())
    where = f" ({module})" if module else ""
    dist = ", ".join(f"{roles[r]} {r}" for r in _ROLE_ORDER if roles.get(r))
    parts = [f"Package `{fqn}`{where} contains {total} class(es): {dist}."]
    if notable:
        parts.append(f"Notable: {', '.join(notable[:6])}.")
    return " ".join(parts)


def summarize_package(conn: sqlite3.Connection, package_id: int) -> str:
    pkg = conn.execute(
        "SELECT p.fqn, mo.name AS module FROM package p "
        "LEFT JOIN module mo ON mo.id = p.module_id WHERE p.id = ?",
        (package_id,),
    ).fetchone()
    if pkg is None:
        return ""
    rows = conn.execute(
        "SELECT simple_name, role FROM class WHERE package_id = ?", (package_id,)
    ).fetchall()
    roles = Counter(r["role"] for r in rows)
    notable = [r["simple_name"] for r in rows if r["role"] in ("controller", "service")]
    return _render(pkg["fqn"], pkg["module"], roles, notable)


def generate_package_summaries(conn: sqlite3.Connection) -> int:
    repo.clear_summaries(conn, kind="package", commit=False)
    package_ids = [r["id"] for r in conn.execute("SELECT id FROM package")]
    count = 0
    for pid in package_ids:
        content = summarize_package(conn, pid)
        if content:
            repo.insert_summary(conn, "package", pid, content, model="deterministic", commit=False)
            count += 1
    conn.commit()
    return count
