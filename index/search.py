"""Full-text search over the search_index FTS5 virtual table.

Indexes classes, methods and endpoints. Used by find_code_areas and, indirectly,
by generate_context_pack.
"""

from __future__ import annotations

import re
import sqlite3

# \w is Unicode-aware: query terms may be Russian (descriptions are generated in
# ru by default), not just ASCII identifiers.
_TOKEN = re.compile(r"\w+")

# CamelCase / digit-boundary splitter for Java identifiers (ASCII only — Java
# names are ASCII; prose is already tokenized fine by unicode61).
_SUBWORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _subwords(token: str) -> list[str]:
    return _SUBWORD.findall(token)


def _with_subwords(name: str) -> str:
    """`DepositAccountService` -> `DepositAccountService Deposit Account Service`,
    so mid-name terms (`account`) match. FTS5 tokenizes the whole identifier as a
    single token, which only prefix queries can hit; consumers never display this
    column (they re-fetch by entity_id), so enriching it is invisible to users."""
    parts = _subwords(name)
    return f"{name} {' '.join(parts)}" if len(parts) > 1 else name


def build_search_index(conn: sqlite3.Connection) -> int:
    """Clear and repopulate search_index from class/method/endpoint rows."""
    conn.execute("DELETE FROM search_index")

    # classes: annotations joined, summary included
    rows = conn.execute(
        """
        SELECT cl.id, cl.simple_name, cl.fqn, cl.summary,
               COALESCE(GROUP_CONCAT(ca.name, ' '), '') AS anns
        FROM class cl
        LEFT JOIN class_annotation ca ON ca.class_id = cl.id
        GROUP BY cl.id
        """
    )
    conn.executemany(
        "INSERT INTO search_index (entity_type, entity_id, name, fqn, annotations, summary) "
        "VALUES ('class', ?, ?, ?, ?, ?)",
        [(r["id"], _with_subwords(r["simple_name"]), r["fqn"], r["anns"], r["summary"] or "") for r in rows],
    )

    # methods: fqn = ClassFqn#method; summary (when described) makes meaning searchable
    rows = conn.execute(
        """
        SELECT m.id, m.name, (cl.fqn || '#' || m.name) AS fqn, m.summary,
               COALESCE(GROUP_CONCAT(ma.name, ' '), '') AS anns
        FROM method m
        JOIN class cl ON cl.id = m.class_id
        LEFT JOIN method_annotation ma ON ma.method_id = m.id
        GROUP BY m.id
        """
    )
    conn.executemany(
        "INSERT INTO search_index (entity_type, entity_id, name, fqn, annotations, summary) "
        "VALUES ('method', ?, ?, ?, ?, ?)",
        [(r["id"], _with_subwords(r["name"]), r["fqn"], r["anns"], r["summary"] or "") for r in rows],
    )

    # endpoints: name = full_path, annotations = http method
    rows = conn.execute(
        "SELECT e.id, e.full_path, e.http_method, c.fqn AS controller "
        "FROM endpoint e LEFT JOIN class c ON c.id = e.controller_class_id"
    )
    conn.executemany(
        "INSERT INTO search_index (entity_type, entity_id, name, fqn, annotations, summary) "
        "VALUES ('endpoint', ?, ?, ?, ?, '')",
        [(r["id"], r["full_path"], r["controller"] or "", r["http_method"]) for r in rows],
    )

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
    return total


def _to_match_query(query: str) -> str:
    """Turn a free-text query into a forgiving FTS5 MATCH expression (prefix OR).
    CamelCase terms also match as a subword phrase, so `DepositAccount` finds the
    `Deposit Account Service` expansion written by :func:`_with_subwords`."""
    terms = _TOKEN.findall(query)
    if not terms:
        return '""'
    alts: list[str] = []
    for t in terms:
        alts.append(f'"{t}"*')
        parts = _subwords(t)
        if len(parts) > 1:
            alts.append('"' + " ".join(parts) + '"*')
    return " OR ".join(alts)


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    entity_type: str | None = None,
) -> list[dict]:
    match = _to_match_query(query)
    sql = (
        "SELECT entity_type, entity_id, name, fqn, annotations, summary, "
        "bm25(search_index) AS score "
        "FROM search_index WHERE search_index MATCH ?"
    )
    params: list = [match]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]
