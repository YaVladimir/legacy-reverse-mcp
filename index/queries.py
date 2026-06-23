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
            "SELECT http_method, full_path, controller_fqn, handler_name FROM v_endpoint_full WHERE id = ?",
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
# get_change_impact
# ------------------------------------------------------------

def change_impact(conn: sqlite3.Connection, symbol: str, limit: int = 60) -> dict | None:
    targets = conn.execute(
        "SELECT id, fqn, simple_name, role, kind FROM class WHERE fqn = ? OR simple_name = ?",
        (symbol, symbol),
    ).fetchall()
    if not targets:
        return None
    target_ids = [t["id"] for t in targets]
    ph = ",".join("?" * len(target_ids))

    # direct dependents (reverse class_dependency edges), with the via-kinds aggregated
    dep_map: dict[int, dict] = {}
    for r in conn.execute(
        f"SELECT c.id, c.fqn, c.simple_name, c.role, cd.kind AS via "
        f"FROM class_dependency cd JOIN class c ON c.id = cd.from_class_id "
        f"WHERE cd.to_class_id IN ({ph}) AND c.id NOT IN ({ph})",
        target_ids + target_ids,
    ):
        d = dep_map.setdefault(
            r["id"], {"fqn": r["fqn"], "simple_name": r["simple_name"], "role": r["role"], "via": set()}
        )
        d["via"].add(r["via"])
    dependents = sorted(dep_map.values(), key=lambda d: d["fqn"])[:limit]
    for d in dependents:
        d["via"] = sorted(d["via"])

    # affected endpoints: those exposed by the symbol or any dependent controller
    controller_ids = target_ids + list(dep_map.keys())
    ph2 = ",".join("?" * len(controller_ids))
    affected_endpoints = [
        dict(r)
        for r in conn.execute(
            f"SELECT http_method, full_path, controller_fqn FROM v_endpoint_full "
            f"WHERE id IN (SELECT id FROM endpoint WHERE controller_class_id IN ({ph2})) "
            f"ORDER BY full_path LIMIT ?",
            controller_ids + [limit],
        )
    ]

    # heuristic test candidates (test sources are not indexed): likely class names
    base_names = {t["simple_name"] for t in targets} | {d["simple_name"] for d in dependents}
    test_candidates = sorted(
        {f"{n}Test" for n in base_names} | {f"{n}IT" for n in base_names}
    )[: limit]

    return {
        "symbol": symbol,
        "resolved": [{"fqn": t["fqn"], "role": t["role"], "kind": t["kind"]} for t in targets],
        "direct_dependents": dependents,
        "dependent_count": len(dep_map),
        "affected_endpoints": affected_endpoints,
        "test_candidates": test_candidates,
        "note": "test_candidates are heuristic (test sources are not indexed); "
                "dependents derived from field/param/return/inheritance type references.",
    }


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
