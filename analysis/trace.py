"""Stage 5: evidence-based, honest endpoint trace.

Follows controller -> service -> repository/persistence using, in order of
strength: (1) syntactic method calls recorded during scan (high), (2) the
dependency-injection graph + naming (medium), (3) name/package similarity (low).
Never claims a precise call graph; always returns per-step + overall confidence,
evidence, and limitations.
"""

from __future__ import annotations

import sqlite3

from analysis.common import conf_str, ev, limitations, min_confidence, not_found
from index.queries import _PERSISTENCE_TYPES, _simple_type


# ------------------------------------------------------------
# resolution helpers
# ------------------------------------------------------------

def _resolve_endpoint(conn, endpoint_id, http_method, path_contains):
    if endpoint_id is not None:
        return conn.execute("SELECT * FROM endpoint WHERE id = ?", (endpoint_id,)).fetchone()
    # exclude superseded interface rows: they're duplicated by (and less accurate
    # than) the reattributed controller rows
    query = "SELECT * FROM endpoint WHERE superseded = 0"
    params: list = []
    if http_method:
        query += " AND http_method = ?"
        params.append(http_method.upper())
    if path_contains:
        query += " AND full_path LIKE ?"
        params.append(f"%{path_contains}%")
    query += " ORDER BY full_path LIMIT 1"
    return conn.execute(query, params).fetchone()


def _resolve_type_to_class(conn, type_fqn):
    if not type_fqn:
        return None
    # import-resolved types are stored as FQN: match exactly before simple-name
    row = conn.execute("SELECT * FROM class WHERE fqn = ? LIMIT 1", (type_fqn,)).fetchone()
    if row is not None:
        return row
    simple = _simple_type(type_fqn)
    if not simple:
        return None
    return conn.execute("SELECT * FROM class WHERE simple_name = ? LIMIT 1", (simple,)).fetchone()


def _find_impl(conn, row):
    """If row is an interface, prefer its *Impl implementation.

    Returns ``(resolved_row, ambiguous)``. ``ambiguous`` is True when several
    implementations exist and none follows the canonical ``<Name>Impl``
    convention: the returned one is then an arbitrary pick — which impl Spring
    actually wires (@Primary/@Qualifier) is not indexed — so the caller must
    degrade the confidence of everything derived from it (weakest link wins),
    not just attach a limitation."""
    if row is None or row["kind"] != "interface":
        return row, False
    impls = conn.execute(
        "SELECT cl.* FROM class cl JOIN class_interface ci ON ci.class_id = cl.id "
        "WHERE ci.interface_fqn = ?",
        (row["simple_name"],),
    ).fetchall()
    if not impls:
        return row, False
    for impl in impls:
        if impl["simple_name"] == row["simple_name"] + "Impl":
            return impl, False
    return impls[0], len(impls) > 1


def _looks_like(role_target: str, row) -> bool:
    name = row["simple_name"]
    if role_target == "service":
        return row["role"] == "service" or name.endswith(("Service", "PlatformService", "Manager", "Facade"))
    if role_target == "repository":
        return row["role"] == "repository" or name.endswith(("Repository", "Dao"))
    return False


def _calls_of_method(conn, method_id):
    return conn.execute(
        "SELECT receiver_field, callee_name, receiver_type_fqn, line FROM method_call "
        "WHERE caller_method_id = ? ORDER BY line",
        (method_id,),
    ).fetchall()


def _injected_of(conn, class_id):
    return conn.execute(
        "SELECT name, type_fqn FROM field WHERE class_id = ? AND is_injected = 1", (class_id,)
    ).fetchall()


def _method_in_class(conn, class_id, name):
    return conn.execute(
        "SELECT id, name FROM method WHERE class_id = ? AND name = ? ORDER BY line_start LIMIT 1",
        (class_id, name),
    ).fetchone()


# ------------------------------------------------------------
# trace
# ------------------------------------------------------------

def trace_endpoint(
    conn: sqlite3.Connection,
    endpoint_id: int | None = None,
    http_method: str | None = None,
    path_contains: str | None = None,
) -> dict:
    ep = _resolve_endpoint(conn, endpoint_id, http_method, path_contains)
    if ep is None:
        sample = [
            {"id": r["id"], "endpoint": f"{r['http_method']} {r['full_path']}"}
            for r in conn.execute(
                "SELECT id, http_method, full_path FROM endpoint WHERE superseded = 0 "
                "ORDER BY full_path LIMIT 8"
            )
        ]
        return not_found(
            "endpoint",
            {"endpoint_id": endpoint_id, "http_method": http_method, "path_contains": path_contains},
            sample,
        )

    full = conn.execute("SELECT * FROM v_endpoint_full WHERE id = ?", (ep["id"],)).fetchone()
    if full is None or "annotation_inherited" not in full.keys():
        # the endpoint is superseded (hidden from the view), or the index predates
        # the current endpoint view and wasn't migrated on open — either way, give a
        # structured, actionable error instead of raising mid-trace.
        return not_found(
            "endpoint",
            {"endpoint_id": endpoint_id, "http_method": http_method, "path_contains": path_contains},
            [{"suggestion": "re-run scan to rebuild the endpoint index"}],
        )
    controller_id = ep["controller_class_id"]
    handler_id = ep["handler_method_id"]
    controller_fqn = full["controller_fqn"]
    controller_name = _simple_type(controller_fqn) or controller_fqn
    handler_name = full["handler_name"]
    file_path = full["controller_file"]

    endpoint_evidence = [
        ev(
            "mapping_annotation",
            f"{ep['http_method']} {ep['full_path']} handled by {controller_name}#{handler_name}",
            file_path=file_path,
            line_start=full["handler_line"],
            symbol=f"{controller_name}#{handler_name}",
        )
    ]
    if full["annotation_inherited"]:
        # controller/handler above is the concrete @RestController the DI-trace starts
        # from, but the mapping annotation itself lives on an ancestor interface —
        # cite that truthfully instead of implying it's on the controller line.
        annotation_symbol = f"{_simple_type(full['annotation_fqn']) or full['annotation_fqn']}#{full['annotation_method_name']}"
        endpoint_evidence.append(
            ev(
                "inherited_mapping_annotation",
                f"Annotation inherited from {annotation_symbol}, implemented by {controller_name}#{handler_name}",
                file_path=full["annotation_file"],
                line_start=full["annotation_line"],
                symbol=annotation_symbol,
            )
        )

    endpoint_block = {
        "http_method": ep["http_method"],
        "path": ep["full_path"],
        "controller_class": controller_name,
        "controller_method": handler_name,
        "evidence": endpoint_evidence,
    }

    steps: list[dict] = [
        {
            "step": 1,
            "kind": "controller_method",
            "symbol": f"{controller_name}#{handler_name}",
            "confidence": "high",
            "evidence": endpoint_block["evidence"],
        }
    ]

    # ---- step 2: service ------------------------------------------------
    service_class = None
    service_method_name = None
    impl_ambiguous = False
    warnings: list[str] = []
    step_no = 2
    handler_calls = _calls_of_method(conn, handler_id) if handler_id else []

    # Collect EVERY service-like call before choosing: a handler often calls an
    # audit/notification service before the business one, and silently tracing
    # the first call with high confidence would confidently show the wrong chain.
    service_calls = [
        (call, target)
        for call in handler_calls
        if (target := _resolve_type_to_class(conn, call["receiver_type_fqn"])) is not None
        and _looks_like("service", target)
    ]
    if service_calls:
        call, target = service_calls[0]
        not_followed = sorted({
            f"{t['simple_name']}#{c['callee_name']}" for c, t in service_calls[1:]
            if t["id"] != target["id"]
        })
        service_class, impl_ambiguous = _find_impl(conn, target)
        service_method_name = call["callee_name"]
        steps.append(
            {
                "step": step_no,
                "kind": "service_call",
                "symbol": f"{target['simple_name']}#{call['callee_name']}",
                # a syntactic call is high — unless other service calls exist and
                # "first by line" is just a guess at which one is the business flow
                "confidence": "medium" if not_followed else "high",
                "evidence": [
                    ev(
                        "method_call",
                        f"{controller_name}#{handler_name} calls {call['receiver_field']}.{call['callee_name']}()",
                        file_path=file_path,
                        line_start=call["line"],
                        symbol=f"{controller_name}#{handler_name}",
                    )
                ],
            }
        )
        step_no += 1
        if not_followed:
            warnings.append(
                f"{controller_name}#{handler_name} calls {len(service_calls)} service-like beans; "
                f"the trace follows {target['simple_name']}#{call['callee_name']} (first by line). "
                f"Not followed: {', '.join(not_followed)}."
            )

    if service_class is None and controller_id is not None and handler_id is not None:
        # same-class hop: the handler delegates to a helper in this controller that
        # makes the service call. Step one level into the helper's own field-calls.
        for call in handler_calls:
            if call["receiver_field"] is not None or call["receiver_type_fqn"] != controller_fqn:
                continue  # not a same-class self-call
            helper = _method_in_class(conn, controller_id, call["callee_name"])
            if helper is None:
                continue
            for inner in _calls_of_method(conn, helper["id"]):
                target = _resolve_type_to_class(conn, inner["receiver_type_fqn"])
                if target is None or not _looks_like("service", target):
                    continue
                steps.append(
                    {
                        "step": step_no,
                        "kind": "controller_helper",
                        "symbol": f"{controller_name}#{call['callee_name']}",
                        "confidence": "high",  # syntactic same-class call
                        "evidence": [
                            ev(
                                "method_call",
                                f"{controller_name}#{handler_name} delegates to same-class {call['callee_name']}()",
                                file_path=file_path,
                                line_start=call["line"],
                                symbol=f"{controller_name}#{handler_name}",
                            )
                        ],
                    }
                )
                step_no += 1
                service_class, impl_ambiguous = _find_impl(conn, target)
                service_method_name = inner["callee_name"]
                steps.append(
                    {
                        "step": step_no,
                        "kind": "service_call",
                        "symbol": f"{target['simple_name']}#{inner['callee_name']}",
                        "confidence": "high",  # call found syntactically in the helper body
                        "evidence": [
                            ev(
                                "method_call",
                                f"{controller_name}#{call['callee_name']} calls {inner['receiver_field']}.{inner['callee_name']}()",
                                file_path=file_path,
                                line_start=inner["line"],
                                symbol=f"{controller_name}#{call['callee_name']}",
                            )
                        ],
                    }
                )
                step_no += 1
                break
            if service_class is not None:
                break

    if service_class is None and controller_id is not None:
        # fallback: injection + naming
        for fld in _injected_of(conn, controller_id):
            target = _resolve_type_to_class(conn, fld["type_fqn"])
            if target is not None and _looks_like("service", target):
                service_class, impl_ambiguous = _find_impl(conn, target)
                steps.append(
                    {
                        "step": step_no,
                        "kind": "likely_service",
                        "symbol": target["simple_name"],
                        "confidence": "medium",  # injection + naming
                        "evidence": [
                            ev(
                                "field_injection",
                                f"{controller_name} injects {fld['name']} : {_simple_type(fld['type_fqn'])}",
                                file_path=file_path,
                                symbol=f"{controller_name}.{fld['name']}",
                            )
                        ],
                    }
                )
                step_no += 1
                break

    # ---- step 3: repository / persistence -------------------------------
    if service_class is not None:
        svc_file = service_class["file_path"]
        svc_name = service_class["simple_name"]
        if impl_ambiguous:
            warnings.append(
                f"{svc_name} is one of several implementations of the called interface; "
                "which one Spring actually wires (@Primary/@Qualifier) is not indexed — "
                "downstream steps are derived from an arbitrary candidate."
            )
        # weakest link: steps derived from an arbitrarily-picked impl cannot be high
        impl_conf = "medium" if impl_ambiguous else "high"
        svc_method = (
            _method_in_class(conn, service_class["id"], service_method_name)
            if service_method_name
            else None
        )

        added = False
        if svc_method is not None:
            for call in _calls_of_method(conn, svc_method["id"]):
                simple = _simple_type(call["receiver_type_fqn"])
                if simple in _PERSISTENCE_TYPES:
                    steps.append(_persistence_step(
                        step_no, simple, svc_name, svc_method["name"], call, svc_file,
                        confidence=impl_conf,
                    ))
                    step_no += 1
                    added = True
                    break
                target = _resolve_type_to_class(conn, call["receiver_type_fqn"])
                if target is not None and _looks_like("repository", target):
                    steps.append(
                        {
                            "step": step_no,
                            "kind": "repository_call",
                            "symbol": f"{target['simple_name']}#{call['callee_name']}",
                            "confidence": impl_conf,
                            "evidence": [
                                ev(
                                    "method_call",
                                    f"{svc_name}#{svc_method['name']} calls {call['receiver_field']}.{call['callee_name']}()",
                                    file_path=svc_file,
                                    line_start=call["line"],
                                    symbol=f"{svc_name}#{svc_method['name']}",
                                )
                            ],
                        }
                    )
                    step_no += 1
                    added = True
                    break

        if not added:
            # fallback: repository/persistence injected into the service
            for fld in _injected_of(conn, service_class["id"]):
                simple = _simple_type(fld["type_fqn"])
                if simple in _PERSISTENCE_TYPES:
                    steps.append(
                        {
                            "step": step_no,
                            "kind": "persistence",
                            "symbol": simple,
                            "confidence": "medium",
                            "evidence": [
                                ev("field_injection", f"{svc_name} injects {fld['name']} : {simple}",
                                   file_path=svc_file, symbol=f"{svc_name}.{fld['name']}")
                            ],
                        }
                    )
                    step_no += 1
                    break
                target = _resolve_type_to_class(conn, fld["type_fqn"])
                if target is not None and _looks_like("repository", target):
                    steps.append(
                        {
                            "step": step_no,
                            "kind": "likely_repository",
                            "symbol": target["simple_name"],
                            "confidence": "medium",
                            "evidence": [
                                ev("field_injection", f"{svc_name} injects {fld['name']} : {target['simple_name']}",
                                   file_path=svc_file, symbol=f"{svc_name}.{fld['name']}")
                            ],
                        }
                    )
                    step_no += 1
                    break

    # a trace that found a service but no repository/persistence step is
    # indistinguishable from a complete two-layer flow unless we say so —
    # unresolved hand-written ctor injection is a known cause of exactly this
    if service_class is not None and steps[-1]["kind"] in ("service_call", "likely_service"):
        warnings.append(
            f"No repository/persistence call could be resolved from {service_class['simple_name']} — "
            "the trace may be incomplete, not necessarily two-layered."
        )

    # ---- overall confidence --------------------------------------------
    non_ctrl = steps[1:]
    if not non_ctrl:
        overall = "low"
        warnings.append("No downstream service/repository call could be resolved from this controller method.")
    else:
        overall = conf_str(min_confidence([s["confidence"] for s in steps]))

    return {
        "query": f"{ep['http_method']} {ep['full_path']}",
        "endpoint": endpoint_block,
        "trace": steps,
        "confidence": overall,
        "limitations": limitations(
            "syntactic_calls", "spring_proxies", "interface_impl_unresolved",
            "no_call_graph", "ctor_injection_without_lombok",
        ),
        "warnings": warnings,
    }


def _persistence_step(step_no, simple, svc_name, svc_method, call, svc_file, *, confidence="high") -> dict:
    return {
        "step": step_no,
        "kind": "persistence",
        "symbol": f"{simple}#{call['callee_name']}",
        "confidence": confidence,
        "evidence": [
            ev(
                "method_call",
                f"{svc_name}#{svc_method} calls {call['receiver_field']}.{call['callee_name']}() on {simple}",
                file_path=svc_file,
                line_start=call["line"],
                symbol=f"{svc_name}#{svc_method}",
            )
        ],
    }
