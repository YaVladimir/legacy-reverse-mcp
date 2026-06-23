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
# get_project_overview
# ------------------------------------------------------------

def project_overview(conn: sqlite3.Connection) -> dict:
    manifest = conn.execute(
        "SELECT * FROM scan_manifest ORDER BY id DESC LIMIT 1"
    ).fetchone()

    roles = {r["role"]: r["n"] for r in conn.execute("SELECT role, COUNT(*) n FROM class GROUP BY role")}
    endpoints_by_verb = {
        r["http_method"]: r["n"]
        for r in conn.execute("SELECT http_method, COUNT(*) n FROM endpoint GROUP BY http_method")
    }

    top_modules = [
        {"name": r["name"], "classes": r["n"], "endpoints": r["ep"]}
        for r in conn.execute(
            "SELECT mo.name, COUNT(DISTINCT cl.id) n, "
            "       COUNT(DISTINCT e.id) ep "
            "FROM module mo "
            "LEFT JOIN class cl ON cl.module_id = mo.id "
            "LEFT JOIN endpoint e ON e.controller_class_id = cl.id "
            "GROUP BY mo.id ORDER BY n DESC LIMIT 8"
        )
    ]

    top_external = [
        {"artifact": f"{r['group_id']}:{r['artifact_id']}", "used_by_modules": r["n"]}
        for r in conn.execute(
            "SELECT group_id, artifact_id, COUNT(DISTINCT module_id) n "
            "FROM external_dependency GROUP BY group_id, artifact_id ORDER BY n DESC LIMIT 10"
        )
    ]

    findings = {
        r["kind"]: r["n"]
        for r in conn.execute("SELECT kind, COUNT(*) n FROM finding GROUP BY kind")
    }
    sample_findings = [
        {"kind": r["kind"], "severity": r["severity"], "description": r["description"]}
        for r in conn.execute(
            "SELECT kind, severity, description FROM finding "
            "ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END LIMIT 15"
        )
    ]

    return {
        "repo_path": manifest["repo_path"] if manifest else None,
        "build_tool": manifest["build_tool"] if manifest else None,
        "scanned_at": manifest["scanned_at"] if manifest else None,
        "totals": {
            "modules": conn.execute("SELECT COUNT(*) FROM module").fetchone()[0],
            "classes": conn.execute("SELECT COUNT(*) FROM class").fetchone()[0],
            "methods": conn.execute("SELECT COUNT(*) FROM method").fetchone()[0],
            "endpoints": conn.execute("SELECT COUNT(*) FROM endpoint").fetchone()[0],
        },
        "roles": roles,
        "endpoints_by_verb": endpoints_by_verb,
        "top_modules": top_modules,
        "top_external_dependencies": top_external,
        "findings": findings,
        "sample_findings": sample_findings,
    }


# ------------------------------------------------------------
# find_code_areas
# ------------------------------------------------------------

def find_code_areas(conn: sqlite3.Connection, query: str, limit: int = 20) -> dict:
    """Keyword search over classes/methods/endpoints, grouped and enriched."""
    from index import search as search_mod

    # search each entity type separately so the 21k methods don't crowd out
    # the (far fewer) classes and endpoints in the ranked window.
    classes: list[dict] = []
    for h in search_mod.search(conn, query, limit=limit, entity_type="class"):
        row = conn.execute(
            "SELECT fqn, simple_name, role, kind, file_path, line_start FROM class WHERE id = ?",
            (h["entity_id"],),
        ).fetchone()
        if row:
            d = dict(row)
            d["module"] = _module_name(conn, h["entity_id"])
            classes.append(d)

    endpoints: list[dict] = []
    for h in search_mod.search(conn, query, limit=limit, entity_type="endpoint"):
        row = conn.execute(
            "SELECT id, http_method, full_path, controller_fqn, handler_name FROM v_endpoint_full WHERE id = ?",
            (h["entity_id"],),
        ).fetchone()
        if row:
            endpoints.append(dict(row))

    methods: list[dict] = []
    for h in search_mod.search(conn, query, limit=limit, entity_type="method"):
        row = conn.execute(
            "SELECT m.name, m.signature, c.fqn AS class_fqn, m.line_start "
            "FROM method m JOIN class c ON c.id = m.class_id WHERE m.id = ?",
            (h["entity_id"],),
        ).fetchone()
        if row:
            methods.append(dict(row))

    return {
        "query": query,
        "counts": {"classes": len(classes), "endpoints": len(endpoints), "methods": len(methods)},
        "classes": classes,
        "endpoints": endpoints,
        "methods": methods,
    }


def _module_name(conn: sqlite3.Connection, class_id: int) -> str | None:
    row = conn.execute(
        "SELECT mo.name FROM class cl LEFT JOIN module mo ON mo.id = cl.module_id WHERE cl.id = ?",
        (class_id,),
    ).fetchone()
    return row["name"] if row else None


# ------------------------------------------------------------
# get_module_map
# ------------------------------------------------------------

def module_map(conn: sqlite3.Connection) -> dict:
    """Modules with inter-module deps, external dep coordinates and endpoint counts."""
    modules = conn.execute("SELECT id, name, path, build_file, packaging FROM module ORDER BY name").fetchall()

    class_counts = {
        r["module_id"]: r["n"]
        for r in conn.execute("SELECT module_id, COUNT(*) n FROM class GROUP BY module_id")
    }
    endpoint_counts = {
        r["module_id"]: r["n"]
        for r in conn.execute(
            "SELECT cl.module_id AS module_id, COUNT(*) n FROM endpoint e "
            "JOIN class cl ON cl.id = e.controller_class_id GROUP BY cl.module_id"
        )
    }

    depends_on: dict[int, list[str]] = {}
    edges: list[list[str]] = []
    for r in conn.execute(
        "SELECT m1.id AS fid, m1.name AS fname, m2.name AS tname "
        "FROM module_dependency md "
        "JOIN module m1 ON m1.id = md.from_module_id "
        "JOIN module m2 ON m2.id = md.to_module_id"
    ):
        depends_on.setdefault(r["fid"], []).append(r["tname"])
        edges.append([r["fname"], r["tname"]])

    external: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT module_id, group_id, artifact_id FROM external_dependency"
    ):
        external.setdefault(r["module_id"], []).append(f"{r['group_id']}:{r['artifact_id']}")

    out_modules = []
    for m in modules:
        out_modules.append(
            {
                "name": m["name"],
                "path": m["path"],
                "build_file": m["build_file"],
                "packaging": m["packaging"],
                "classes": class_counts.get(m["id"], 0),
                "endpoints": endpoint_counts.get(m["id"], 0),
                "depends_on": sorted(depends_on.get(m["id"], [])),
                "external_deps": sorted(set(external.get(m["id"], []))),
            }
        )

    return {"module_count": len(out_modules), "modules": out_modules, "edges": edges}


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
# persistence primitives — kept for analysis.trace (raw DB access detection)
# ------------------------------------------------------------

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


