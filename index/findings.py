"""Lightweight heuristic findings written to the `finding` table during scan.

Cheap structural smells only (no LLM, no deep analysis): circular module
dependencies, god classes, oversized controllers. Surfaced by get_project_overview.
"""

from __future__ import annotations

import sqlite3

from index import repository as repo

GOD_CLASS_METHODS = 40
LARGE_CONTROLLER_ENDPOINTS = 20


def _module_cycles(conn: sqlite3.Connection) -> list[list[str]]:
    """Return simple cycles in the module dependency graph (by module name)."""
    adj: dict[int, list[int]] = {}
    for r in conn.execute("SELECT from_module_id, to_module_id FROM module_dependency"):
        adj.setdefault(r["from_module_id"], []).append(r["to_module_id"])
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM module")}

    cycles: list[list[str]] = []
    seen_pairs: set[frozenset[int]] = set()
    color: dict[int, int] = {}  # 0=unvisited,1=in-stack,2=done
    stack: list[int] = []

    def dfs(u: int) -> None:
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, 0) == 1:
                # back-edge u->v: cycle from v..u
                if u in adj.get(v, []):  # report 2-cycles and longer once
                    pair = frozenset((u, v))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        cycles.append([names.get(v, str(v)), names.get(u, str(u))])
                elif v in stack:
                    idx = stack.index(v)
                    cyc = [names.get(n, str(n)) for n in stack[idx:]]
                    key = frozenset(stack[idx:])
                    if key not in seen_pairs:
                        seen_pairs.add(key)
                        cycles.append(cyc)
            elif color.get(v, 0) == 0:
                dfs(v)
        stack.pop()
        color[u] = 2

    for node in list(adj.keys()):
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def detect_findings(conn: sqlite3.Connection) -> dict:
    repo.clear_findings(conn, commit=False)
    counts: dict[str, int] = {}

    def bump(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1

    # circular module dependencies
    for cycle in _module_cycles(conn):
        mod_row = conn.execute("SELECT id FROM module WHERE name = ?", (cycle[0],)).fetchone()
        repo.insert_finding(
            conn, "circular_dependency",
            "Circular module dependency: " + " -> ".join(cycle + [cycle[0]]),
            severity="warning",
            module_id=mod_row["id"] if mod_row else None,
            commit=False,
        )
        bump("circular_dependency")

    # god classes (many methods)
    for r in conn.execute(
        "SELECT c.id, c.fqn, COUNT(m.id) n FROM class c JOIN method m ON m.class_id = c.id "
        "GROUP BY c.id HAVING n > ?",
        (GOD_CLASS_METHODS,),
    ):
        repo.insert_finding(
            conn, "god_class", f"{r['fqn']} has {r['n']} methods",
            severity="warning", class_id=r["id"], commit=False,
        )
        bump("god_class")

    # large controllers (many endpoints)
    for r in conn.execute(
        "SELECT c.id, c.fqn, COUNT(e.id) n FROM class c JOIN endpoint e ON e.controller_class_id = c.id "
        "GROUP BY c.id HAVING n > ?",
        (LARGE_CONTROLLER_ENDPOINTS,),
    ):
        repo.insert_finding(
            conn, "large_controller", f"{r['fqn']} exposes {r['n']} endpoints",
            severity="info", class_id=r["id"], commit=False,
        )
        bump("large_controller")

    conn.commit()
    return counts
