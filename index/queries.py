"""Read models used by the MCP tools. SQL lives here; the server stays thin."""

from __future__ import annotations

import json
import re
import sqlite3

_GENERIC = re.compile(r"<.*>")


def _simple_type(type_fqn: str | None) -> str | None:
    if not type_fqn:
        return None
    t = _GENERIC.sub("", type_fqn).strip()
    t = t.rstrip("[]").strip()
    if "." in t:
        t = t.rsplit(".", 1)[-1]
    return t or None


# ------------------------------------------------------------
# list_endpoints
# ------------------------------------------------------------

def list_endpoints(
    conn: sqlite3.Connection,
    http_method: str | None = None,
    path_contains: str | None = None,
    limit: int = 200,
) -> list[dict]:
    query = "SELECT * FROM v_endpoint_full WHERE 1=1"
    params: list = []
    if http_method:
        query += " AND http_method = ?"
        params.append(http_method.upper())
    if path_contains:
        query += " AND full_path LIKE ?"
        params.append(f"%{path_contains}%")
    query += " ORDER BY full_path, http_method LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params)]


# ------------------------------------------------------------
# explain_class
# ------------------------------------------------------------

def class_detail(conn: sqlite3.Connection, fqn: str) -> dict | None:
    cls = conn.execute("SELECT * FROM v_class_full WHERE fqn = ?", (fqn,)).fetchone()
    if cls is None:
        # fall back to simple-name match
        cls = conn.execute(
            "SELECT * FROM v_class_full WHERE simple_name = ? LIMIT 1", (fqn,)
        ).fetchone()
    if cls is None:
        return None

    class_id = conn.execute("SELECT id FROM class WHERE fqn = ?", (cls["fqn"],)).fetchone()["id"]

    annotations = [
        dict(r)
        for r in conn.execute(
            "SELECT name, attributes FROM class_annotation WHERE class_id = ?", (class_id,)
        )
    ]
    interfaces = [
        r["interface_fqn"]
        for r in conn.execute(
            "SELECT interface_fqn FROM class_interface WHERE class_id = ?", (class_id,)
        )
    ]
    fields = [
        dict(r)
        for r in conn.execute(
            "SELECT name, type_fqn, visibility, is_static, is_injected, annotation_names "
            "FROM field WHERE class_id = ? ORDER BY name",
            (class_id,),
        )
    ]
    injected = [
        {"name": f["name"], "type": f["type_fqn"]} for f in fields if f["is_injected"]
    ]
    methods = []
    for m in conn.execute(
        "SELECT id, name, signature, return_type, visibility, line_start, line_end "
        "FROM method WHERE class_id = ? ORDER BY line_start",
        (class_id,),
    ):
        m_anns = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM method_annotation WHERE method_id = ?", (m["id"],)
            )
        ]
        md = dict(m)
        md["annotations"] = m_anns
        methods.append(md)

    endpoints = [
        dict(r)
        for r in conn.execute(
            "SELECT http_method, full_path, handler_name FROM v_endpoint_full "
            "WHERE controller_fqn = ? ORDER BY full_path",
            (cls["fqn"],),
        )
    ]

    return {
        "fqn": cls["fqn"],
        "simple_name": cls["simple_name"],
        "role": cls["role"],
        "kind": cls["kind"],
        "module": cls["module_name"],
        "package": cls["package_fqn"],
        "file_path": cls["file_path"],
        "line_start": cls["line_start"],
        "summary": cls["summary"],
        "annotations": annotations,
        "interfaces": interfaces,
        "injected_dependencies": injected,
        "fields": fields,
        "methods": methods,
        "endpoints": endpoints,
    }


# ------------------------------------------------------------
# trace_endpoint (heuristic controller -> service -> repository -> entity)
# ------------------------------------------------------------

def _resolve_type(conn: sqlite3.Connection, type_fqn: str | None) -> tuple[sqlite3.Row | None, str]:
    """Resolve a (usually simple) type name to a class row.

    Returns (row|None, confidence). confidence: high (unique fqn/simple),
    medium (one of several), low/none (unresolved).
    """
    simple = _simple_type(type_fqn)
    if not simple:
        return None, "low"
    rows = conn.execute("SELECT * FROM class WHERE simple_name = ?", (simple,)).fetchall()
    if not rows:
        return None, "low"
    if len(rows) == 1:
        return rows[0], "high"
    # prefer a non-interface/concrete impl if obvious; otherwise first, medium conf
    return rows[0], "medium"


def _injected_of(conn: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, type_fqn FROM field WHERE class_id = ? AND is_injected = 1", (class_id,)
    ).fetchall()


# Raw persistence primitives that mark "reaches the database" without a @Repository.
_PERSISTENCE_TYPES = {
    "JdbcTemplate",
    "NamedParameterJdbcTemplate",
    "JdbcOperations",
    "EntityManager",
    "SqlSession",
    "DataSource",
    "RoutingDataSource",
}


def _find_impl(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """If ``row`` is an interface, return its implementing class (prefer *Impl).

    Falls back to ``row`` itself when no implementation is indexed.
    """
    if row["kind"] != "interface":
        return row
    impls = conn.execute(
        "SELECT cl.* FROM class cl JOIN class_interface ci ON ci.class_id = cl.id "
        "WHERE ci.interface_fqn = ?",
        (row["simple_name"],),
    ).fetchall()
    if not impls:
        return row
    for impl in impls:
        if impl["simple_name"] == row["simple_name"] + "Impl":
            return impl
    return impls[0]


def _looks_like(role_target: str, row: sqlite3.Row) -> bool:
    name = row["simple_name"]
    if role_target == "service":
        return row["role"] == "service" or name.endswith(("Service", "PlatformService"))
    if role_target == "repository":
        return row["role"] == "repository" or name.endswith(("Repository", "Dao"))
    return False


def trace_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> dict | None:
    ep = conn.execute("SELECT * FROM v_endpoint_full WHERE id = ?", (endpoint_id,)).fetchone()
    if ep is None:
        return None
    ep_row = conn.execute("SELECT * FROM endpoint WHERE id = ?", (endpoint_id,)).fetchone()

    steps: list[dict] = []
    controller_id = ep_row["controller_class_id"]

    # step 0: controller + handler
    steps.append(
        {
            "step": 0,
            "role": "controller",
            "fqn": ep["controller_fqn"],
            "method": ep["handler_name"],
            "confidence": "high",
        }
    )

    # step 1: services injected into the controller. Fineract injects service
    # *interfaces*; resolve each to its impl so step 2 can follow real deps.
    impl_ids: list[int] = []
    found_service = False
    if controller_id is not None:
        for fld in _injected_of(conn, controller_id):
            row, conf = _resolve_type(conn, fld["type_fqn"])
            if row is not None and _looks_like("service", row):
                found_service = True
                impl = _find_impl(conn, row)
                impl_ids.append(impl["id"])
                steps.append(
                    {
                        "step": 1,
                        "role": "service",
                        "fqn": row["fqn"],
                        "impl_fqn": impl["fqn"] if impl["id"] != row["id"] else None,
                        "via_field": fld["name"],
                        "confidence": conf,
                    }
                )

    # step 2: repositories / persistence primitives injected into those impls
    reached_data = False
    seen: set[int] = set()
    for sid in impl_ids:
        if sid in seen:
            continue
        seen.add(sid)
        for fld in _injected_of(conn, sid):
            simple = _simple_type(fld["type_fqn"])
            if simple in _PERSISTENCE_TYPES:
                reached_data = True
                steps.append(
                    {
                        "step": 2,
                        "role": "persistence",
                        "fqn": simple,
                        "via_field": fld["name"],
                        "confidence": "high",
                    }
                )
                continue
            row, conf = _resolve_type(conn, fld["type_fqn"])
            if row is not None and _looks_like("repository", row):
                reached_data = True
                steps.append(
                    {
                        "step": 2,
                        "role": "repository",
                        "fqn": row["fqn"],
                        "via_field": fld["name"],
                        "confidence": "medium" if conf == "high" else "low",
                    }
                )

    overall = "high" if (found_service and reached_data) else "medium" if found_service else "low"
    return {
        "endpoint": {
            "id": endpoint_id,
            "http_method": ep["http_method"],
            "full_path": ep["full_path"],
            "controller": ep["controller_fqn"],
            "handler": ep["handler_signature"],
        },
        "steps": steps,
        "confidence": overall,
    }
