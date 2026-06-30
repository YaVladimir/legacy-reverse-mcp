"""Deterministic, template-based class summaries.

No LLM: summaries are rendered from structured index data so the pipeline runs
offline. The ``render_class_summary`` signature is the seam where an LLM-backed
implementation can be swapped in later behind ``summarize_class``.
"""

from __future__ import annotations

import sqlite3

from index import repository as repo


def render_class_summary(
    *,
    simple_name: str,
    role: str,
    kind: str,
    module: str | None,
    endpoints: list[dict],
    injected: list[str],
    method_count: int,
) -> str:
    where = f" in module `{module}`" if module else ""
    parts = [f"{simple_name} is a {role} ({kind}){where}."]

    if endpoints:
        sample = ", ".join(f"{e['http_method']} {e['full_path']}" for e in endpoints[:5])
        more = f" (+{len(endpoints) - 5} more)" if len(endpoints) > 5 else ""
        parts.append(f"Exposes {len(endpoints)} endpoint(s): {sample}{more}.")

    if injected:
        shown = ", ".join(injected[:8])
        more = f" (+{len(injected) - 8} more)" if len(injected) > 8 else ""
        parts.append(f"Depends on {len(injected)} injected component(s): {shown}{more}.")

    if method_count:
        parts.append(f"Defines {method_count} method(s).")

    return " ".join(parts)


def summarize_class(conn: sqlite3.Connection, class_id: int) -> str:
    """On-demand summary for a single class (exposed via the get_class_summary MCP tool).

    Prefers a stored description (the live seam: an LLM-generated description from
    the ``describe`` step, if present); otherwise renders deterministically.
    """
    from index.queries import class_detail

    cls = conn.execute("SELECT fqn, summary FROM class WHERE id = ?", (class_id,)).fetchone()
    if cls is None:
        return ""
    if cls["summary"]:
        return cls["summary"]
    d = class_detail(conn, cls["fqn"])
    if d is None:
        return ""
    return render_class_summary(
        simple_name=d["simple_name"],
        role=d["role"],
        kind=d["kind"],
        module=d["module"],
        endpoints=d["endpoints"],
        injected=[dep["name"] for dep in d["injected_dependencies"]],
        method_count=len(d["methods"]),
    )


def generate_class_summaries(conn: sqlite3.Connection) -> int:
    """Compute and persist class.summary for every class using bulk reads."""
    classes = {
        r["id"]: {
            "simple_name": r["simple_name"],
            "role": r["role"],
            "kind": r["kind"],
            "module": r["module_name"],
        }
        for r in conn.execute(
            "SELECT cl.id, cl.simple_name, cl.role, cl.kind, mo.name AS module_name "
            "FROM class cl LEFT JOIN module mo ON mo.id = cl.module_id"
        )
    }

    endpoints: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT controller_class_id AS cid, http_method, full_path FROM endpoint "
        "WHERE controller_class_id IS NOT NULL ORDER BY full_path"
    ):
        endpoints.setdefault(r["cid"], []).append(
            {"http_method": r["http_method"], "full_path": r["full_path"]}
        )

    injected: dict[int, list[str]] = {}
    for r in conn.execute("SELECT class_id, name FROM field WHERE is_injected = 1 ORDER BY name"):
        injected.setdefault(r["class_id"], []).append(r["name"])

    method_counts = {
        r["class_id"]: r["n"]
        for r in conn.execute("SELECT class_id, COUNT(*) n FROM method GROUP BY class_id")
    }

    updated = 0
    for cid, info in classes.items():
        summary = render_class_summary(
            simple_name=info["simple_name"],
            role=info["role"],
            kind=info["kind"],
            module=info["module"],
            endpoints=endpoints.get(cid, []),
            injected=injected.get(cid, []),
            method_count=method_counts.get(cid, 0),
        )
        repo.set_class_summary(conn, cid, summary, commit=False)
        updated += 1

    conn.commit()
    return updated
