"""Stage 4: evidence-based explain_class.

Returns observed facts (read straight from the index), inferred findings (each
with its own evidence + confidence), related symbols (injected deps, syntactic
calls, endpoints) and the limitations that bound the answer. No free-text-only
output, and no inference without evidence.
"""

from __future__ import annotations

import sqlite3

from analysis.common import conf_str, ev, limitations
from analysis.layers import infer_spring_layer
from index import repository as repo
from index.queries import _simple_type

# HTTP verb -> what the endpoint most likely does (heuristic, but the verb itself
# is a direct fact). Used to describe each endpoint's purpose.
_VERB_PURPOSE = {
    "GET": "reads/queries",
    "POST": "creates/submits",
    "PUT": "creates or replaces",
    "PATCH": "partially updates",
    "DELETE": "deletes",
    "HEAD": "checks existence",
    "OPTIONS": "describes options",
}
_MAX_ENDPOINT_FINDINGS = 25


def _finding(finding_type, subject, summary, confidence, evidence, limitation_codes=()):
    """Build a JSON-able inferred finding (same shape as infer_spring_layer)."""
    assert evidence, "an inferred finding must carry at least one evidence item"
    return {
        "finding_type": finding_type,
        "subject": subject,
        "summary": summary,
        "confidence": confidence,
        "evidence": evidence,
        "limitations": limitations(*limitation_codes),
    }


def _endpoint_purpose(http_method: str | None, handler_name: str | None) -> str:
    action = _VERB_PURPOSE.get((http_method or "").upper(), "handles")
    return f"{action} (handler {handler_name})" if handler_name else action


def _transaction_finding(conn, class_id, fqn, simple_name, file_path) -> dict | None:
    """Class defines transaction boundaries if it (or its methods) carry @Transactional."""
    methods = conn.execute(
        "SELECT m.name, m.line_start FROM method m JOIN method_annotation ma ON ma.method_id = m.id "
        "WHERE m.class_id = ? AND (ma.name = '@Transactional' OR ma.name LIKE '%.Transactional') "
        "ORDER BY m.line_start",
        (class_id,),
    ).fetchall()
    class_level = conn.execute(
        "SELECT 1 FROM class_annotation WHERE class_id = ? "
        "AND (name = '@Transactional' OR name LIKE '%.Transactional')",
        (class_id,),
    ).fetchone()
    if not methods and not class_level:
        return None

    evidence: list[dict] = []
    if class_level:
        evidence.append(
            ev("annotation", f"{simple_name} is annotated @Transactional", file_path=file_path, symbol=simple_name)
        )
    for m in methods[:5]:
        evidence.append(
            ev("annotation", f"{simple_name}#{m['name']} is @Transactional",
               file_path=file_path, line_start=m["line_start"], symbol=f"{simple_name}#{m['name']}")
        )
    scope = "class-level" if class_level else f"{len(methods)} method(s)"
    return _finding(
        "transaction_boundary", fqn,
        f"Manages transaction boundaries ({scope})", "high", evidence,
        limitation_codes=["spring_proxies"],
    )


def _structural_findings(conn, class_id, fqn, simple_name, file_path) -> list[dict]:
    """Surface structural smells already detected for this class (god_class, etc.)."""
    out: list[dict] = []
    for r in conn.execute(
        "SELECT kind, description FROM finding WHERE class_id = ? ORDER BY kind", (class_id,)
    ):
        out.append(
            _finding(
                r["kind"], fqn, r["description"], "medium",
                [ev("structure", r["description"], file_path=file_path, symbol=simple_name, source="structure")],
                limitation_codes=["no_call_graph"],
            )
        )
    return out


def _resolve_class(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM v_class_full WHERE fqn = ?", (name,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM v_class_full WHERE simple_name = ? ORDER BY fqn LIMIT 1", (name,)
        ).fetchone()
    return row


def _suggest_classes(conn: sqlite3.Connection, name: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT fqn, simple_name, role FROM class WHERE simple_name LIKE ? "
        "OR fqn LIKE ? ORDER BY simple_name LIMIT ?",
        (f"%{name}%", f"%{name}%", limit),
    ).fetchall()
    return [{"fqn": r["fqn"], "name": r["simple_name"], "role": r["role"]} for r in rows]


def _fact_evidence(facts: list[dict], fact_type: str, subject: str, obj: str | None = None) -> list[dict]:
    for f in facts:
        if f["fact_type"] == fact_type and f["subject"] == subject and (obj is None or f["object"] == obj):
            return f["evidence"]
    return []


def explain_class(conn: sqlite3.Connection, name: str) -> dict:
    cls = _resolve_class(conn, name)
    if cls is None:
        from analysis.common import not_found

        return not_found("class", name, _suggest_classes(conn, name))

    fqn = cls["fqn"]
    class_id = conn.execute("SELECT id FROM class WHERE fqn = ?", (fqn,)).fetchone()["id"]
    facts = repo.observed_facts_for_class(conn, fqn)

    # --- inferred findings ------------------------------------------------
    layer_finding = infer_spring_layer(
        fqn=fqn,
        simple_name=cls["simple_name"],
        kind=cls["kind"],
        package=cls["package_fqn"],
        file_path=cls["file_path"],
        class_facts=facts,
    )
    inferred_findings = [layer_finding]

    tx = _transaction_finding(conn, class_id, fqn, cls["simple_name"], cls["file_path"])
    if tx is not None:
        inferred_findings.append(tx)
    inferred_findings.extend(_structural_findings(conn, class_id, fqn, cls["simple_name"], cls["file_path"]))

    # --- related symbols --------------------------------------------------
    injected = []
    for r in conn.execute(
        "SELECT name, type_fqn FROM field WHERE class_id = ? AND is_injected = 1 ORDER BY name",
        (class_id,),
    ):
        subject = f"{fqn}.{r['name']}"
        evidence = _fact_evidence(facts, "field", subject) or [
            ev("field", f"Injected field {r['name']} of type {r['type_fqn']}",
               file_path=cls["file_path"], symbol=f"{cls['simple_name']}.{r['name']}")
        ]
        injected.append({"name": r["name"], "type": r["type_fqn"], "evidence": evidence})

    called = []
    for r in conn.execute(
        "SELECT m.name AS caller, mc.receiver_field, mc.callee_name, mc.receiver_type_fqn, mc.line "
        "FROM method_call mc JOIN method m ON m.id = mc.caller_method_id "
        "WHERE mc.caller_class_id = ? ORDER BY mc.line",
        (class_id,),
    ):
        if r["receiver_field"] is None:
            # same-class self-call (helper delegation): receiver is this class
            target_type = _simple_type(r["receiver_type_fqn"]) or cls["simple_name"]
            desc = f"{cls['simple_name']}#{r['caller']} calls {r['callee_name']}() (same class)"
        else:
            target_type = _simple_type(r["receiver_type_fqn"]) or r["receiver_field"]
            desc = f"{cls['simple_name']}#{r['caller']} calls {r['receiver_field']}.{r['callee_name']}()"
        called.append(
            {
                "symbol": f"{target_type}#{r['callee_name']}",
                "via_field": r["receiver_field"],
                "from_method": r["caller"],
                "confidence": "high",
                "evidence": [
                    ev(
                        "method_call",
                        desc,
                        file_path=cls["file_path"],
                        line_start=r["line"],
                        symbol=f"{cls['simple_name']}#{r['caller']}",
                    )
                ],
            }
        )

    endpoints = []
    endpoint_purpose_findings: list[dict] = []
    for r in conn.execute(
        "SELECT id, http_method, full_path, handler_name FROM v_endpoint_full "
        "WHERE controller_fqn = ? ORDER BY full_path",
        (fqn,),
    ):
        handler_subject = f"{fqn}#{r['handler_name']}" if r["handler_name"] else None
        evidence = (
            _fact_evidence(facts, "mapping_annotation", handler_subject) if handler_subject else []
        )
        purpose = _endpoint_purpose(r["http_method"], r["handler_name"])
        endpoints.append(
            {
                "http_method": r["http_method"],
                "path": r["full_path"],
                "handler": r["handler_name"],
                "purpose": purpose,
                "evidence": evidence,
            }
        )
        if len(endpoint_purpose_findings) < _MAX_ENDPOINT_FINDINGS:
            ep_evidence = evidence or [
                ev("mapping_annotation", f"{r['http_method']} {r['full_path']}",
                   file_path=cls["file_path"], symbol=handler_subject or fqn)
            ]
            endpoint_purpose_findings.append(
                _finding(
                    "endpoint_purpose", handler_subject or fqn,
                    f"{r['http_method']} {r['full_path']} — {purpose}", "medium",
                    ep_evidence, limitation_codes=["dynamic_endpoints"],
                )
            )
    inferred_findings.extend(endpoint_purpose_findings)

    return {
        "class": {
            "name": cls["simple_name"],
            "fqn": fqn,
            "file_path": cls["file_path"],
            "package": cls["package_fqn"],
            "module": cls["module_name"],
            "kind": cls["kind"],
            "role": cls["role"],
            "description": cls["summary"],  # meaningful description from the describe step
        },
        "observed_facts": facts,
        "inferred_findings": inferred_findings,
        "related_symbols": {
            "injected_dependencies": injected,
            "called_methods": called,
            "endpoints": endpoints,
        },
        "confidence": conf_str(layer_finding["confidence"]),
        "limitations": limitations(
            "spring_proxies", "interface_impl_unresolved", "syntactic_calls", "no_call_graph"
        ),
        "warnings": [],
    }
