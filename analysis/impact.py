"""Stage 6: honest change-impact analysis.

Splits results into ``direct_impacts`` (someone references the symbol directly:
a field of its type, a syntactic call, inheritance, a param/return type) and
``candidate_impacts`` (heuristic: endpoints of dependent controllers, test-name
matches). Every impact carries reason + evidence + confidence. Never claims a
change "will" break something — only what is a candidate to review.
"""

from __future__ import annotations

import sqlite3

from analysis.common import ev, limitations
from index.queries import _simple_type

_HIGH_VIA = {"call", "field", "field_injection", "inheritance"}


def _resolve_targets(conn, symbol):
    return conn.execute(
        "SELECT id, fqn, simple_name, role, kind, file_path FROM class "
        "WHERE fqn = ? OR simple_name = ?",
        (symbol, symbol),
    ).fetchall()


def _suggest(conn, symbol, limit=5):
    rows = conn.execute(
        "SELECT fqn, simple_name FROM class WHERE simple_name LIKE ? OR fqn LIKE ? "
        "ORDER BY simple_name LIMIT ?",
        (f"%{symbol}%", f"%{symbol}%", limit),
    ).fetchall()
    return [{"fqn": r["fqn"], "name": r["simple_name"]} for r in rows]


def change_impact(conn: sqlite3.Connection, symbol: str, limit: int = 60) -> dict:
    targets = _resolve_targets(conn, symbol)
    if not targets:
        from analysis.common import not_found

        return not_found("symbol", symbol, _suggest(conn, symbol))

    target_ids = [t["id"] for t in targets]
    target_simples = {t["simple_name"] for t in targets}
    ph = ",".join("?" * len(target_ids))

    # dependent[cid] -> aggregated record
    dependents: dict[int, dict] = {}

    def touch(cid, fqn, name, role, file_path):
        return dependents.setdefault(
            cid,
            {"fqn": fqn, "simple_name": name, "role": role, "file_path": file_path,
             "via": set(), "evidence": []},
        )

    # 1) class_dependency reverse edges (injected field / param / return / inheritance)
    for r in conn.execute(
        f"SELECT c.id, c.fqn, c.simple_name, c.role, c.file_path, cd.kind AS via "
        f"FROM class_dependency cd JOIN class c ON c.id = cd.from_class_id "
        f"WHERE cd.to_class_id IN ({ph}) AND c.id NOT IN ({ph})",
        target_ids + target_ids,
    ):
        touch(r["id"], r["fqn"], r["simple_name"], r["role"], r["file_path"])["via"].add(r["via"])

    # 2) direct syntactic calls onto the symbol (evidence: file + line)
    for r in conn.execute(
        "SELECT mc.caller_class_id AS cid, c.fqn, c.simple_name, c.role, c.file_path, "
        "       m.name AS caller, mc.receiver_field, mc.callee_name, mc.receiver_type_fqn, mc.line "
        "FROM method_call mc JOIN class c ON c.id = mc.caller_class_id "
        "JOIN method m ON m.id = mc.caller_method_id"
    ):
        if _simple_type(r["receiver_type_fqn"]) not in target_simples or r["cid"] in target_ids:
            continue
        rec = touch(r["cid"], r["fqn"], r["simple_name"], r["role"], r["file_path"])
        rec["via"].add("call")
        rec["evidence"].append(
            ev("method_call",
               f"{r['simple_name']}#{r['caller']} calls {r['receiver_field']}.{r['callee_name']}()",
               file_path=r["file_path"], line_start=r["line"], symbol=f"{r['simple_name']}#{r['caller']}")
        )

    # 3) fields typed as the symbol (injected or plain)
    for r in conn.execute(
        "SELECT f.class_id AS cid, c.fqn, c.simple_name, c.role, c.file_path, f.name, f.type_fqn "
        "FROM field f JOIN class c ON c.id = f.class_id"
    ):
        if _simple_type(r["type_fqn"]) not in target_simples or r["cid"] in target_ids:
            continue
        rec = touch(r["cid"], r["fqn"], r["simple_name"], r["role"], r["file_path"])
        rec["via"].add("field")
        rec["evidence"].append(
            ev("field", f"{r['simple_name']} declares field {r['name']} : {_simple_type(r['type_fqn'])}",
               file_path=r["file_path"], symbol=f"{r['simple_name']}.{r['name']}")
        )

    direct_impacts = []
    for rec in sorted(dependents.values(), key=lambda d: d["fqn"])[:limit]:
        via = sorted(rec["via"])
        confidence = "high" if (rec["via"] & _HIGH_VIA) else "medium"
        evidence = rec["evidence"] or [
            ev("type_reference", f"{rec['simple_name']} references {symbol} via {', '.join(via)}",
               file_path=rec["file_path"], symbol=rec["simple_name"])
        ]
        direct_impacts.append(
            {
                "kind": "class",
                "target": rec["fqn"],
                "reason": f"{rec['simple_name']} directly references {symbol} (via {', '.join(via)})",
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    # ---- candidate impacts ---------------------------------------------
    candidate_impacts = []
    controller_ids = list(target_ids) + list(dependents.keys())
    ph2 = ",".join("?" * len(controller_ids))
    seen_ep = set()
    for r in conn.execute(
        f"SELECT e.id, e.http_method, e.full_path, e.controller_class_id, c.simple_name AS ctrl "
        f"FROM endpoint e JOIN class c ON c.id = e.controller_class_id "
        f"WHERE e.controller_class_id IN ({ph2}) ORDER BY e.full_path LIMIT ?",
        controller_ids + [limit],
    ):
        key = (r["http_method"], r["full_path"])
        if key in seen_ep:
            continue
        seen_ep.add(key)
        is_target_ctrl = r["controller_class_id"] in target_ids
        reason = (
            f"Endpoint is exposed by the changed class {r['ctrl']}"
            if is_target_ctrl
            else f"Endpoint controller {r['ctrl']} depends on {symbol}"
        )
        candidate_impacts.append(
            {
                "kind": "endpoint",
                "target": f"{r['http_method']} {r['full_path']}",
                "reason": reason,
                "confidence": "medium",
                "evidence": [
                    ev("mapping_annotation", f"{r['http_method']} {r['full_path']} -> {r['ctrl']}",
                       symbol=r["ctrl"])
                ],
            }
        )

    # test-name candidates (test sources are not indexed)
    base_names = sorted(target_simples | {d["simple_name"] for d in dependents.values()})
    for base in base_names[:limit]:
        for suffix in ("Test", "IT"):
            candidate_impacts.append(
                {
                    "kind": "test_candidate",
                    "target": f"{base}{suffix}",
                    "reason": f"Test name matches affected class {base}",
                    "confidence": "low",
                    "evidence": [ev("naming", f"Convention: {base} is usually tested by {base}{suffix}",
                                    source="structure")],
                }
            )

    # ---- suggested files ------------------------------------------------
    suggested = []
    for t in targets:
        suggested.append(t["file_path"])
    for d in sorted(dependents.values(), key=lambda d: d["fqn"])[:10]:
        suggested.append(d["file_path"])
    suggested = list(dict.fromkeys(p for p in suggested if p))[:15]

    return {
        "symbol": symbol,
        "resolved": [{"fqn": t["fqn"], "role": t["role"], "kind": t["kind"]} for t in targets],
        "direct_impacts": direct_impacts,
        "candidate_impacts": candidate_impacts,
        "suggested_files_for_context": suggested,
        "confidence": "high" if any(i["confidence"] == "high" for i in direct_impacts) else "medium",
        "limitations": limitations(
            "ambiguous_simple_name", "syntactic_calls", "no_call_graph", "tests_not_indexed"
        ),
        "warnings": [],
    }
