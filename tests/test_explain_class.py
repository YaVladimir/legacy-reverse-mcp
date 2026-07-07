"""Stage 4: explain_class is evidence-based for each Spring layer."""

from __future__ import annotations

from analysis.explain import explain_class


def _layer_finding(result):
    return next(f for f in result["inferred_findings"] if f["finding_type"] == "spring_layer")


def test_controller_is_high_confidence_with_evidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = explain_class(conn, "DepositController")

    assert res["class"]["fqn"] == "ru.bank.deposit.DepositController"
    assert res["observed_facts"], "must include observed facts"
    finding = _layer_finding(res)
    assert finding["confidence"] == "high"
    assert finding["evidence"], "inferred finding must carry evidence"
    assert "@RestController" in {
        f["object"] for f in res["observed_facts"] if f["fact_type"] == "class_annotation"
    }
    # related symbols: injected service, syntactic calls, endpoints. The service is
    # in the controller's own package, so its type resolves to the FQN (M7).
    assert any(
        d["type"] == "ru.bank.deposit.DepositService"
        for d in res["related_symbols"]["injected_dependencies"]
    )
    assert any(c["symbol"] == "DepositService#create" for c in res["related_symbols"]["called_methods"])
    assert len(res["related_symbols"]["endpoints"]) == 2
    assert res["limitations"]


def test_service_high_confidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    finding = _layer_finding(explain_class(conn, "DepositService"))
    assert finding["layer"] == "service"
    assert finding["confidence"] == "high"
    assert finding["evidence"]


def test_repository_high_confidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    finding = _layer_finding(explain_class(conn, "DepositRepository"))
    assert finding["layer"] == "repository"
    assert finding["confidence"] == "high"


def test_entity_high_confidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = explain_class(conn, "Deposit")
    finding = _layer_finding(res)
    assert finding["layer"] == "entity"
    assert finding["confidence"] == "high"
    assert finding["evidence"]


def test_every_inferred_finding_has_evidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    for name in ("DepositController", "DepositService", "DepositRepository", "Deposit", "DepositRequest"):
        res = explain_class(conn, name)
        for finding in res["inferred_findings"]:
            assert finding["evidence"], f"{name}: finding without evidence"
        assert res["limitations"]


def test_name_and_package_only_is_lower_confidence():
    # a service-named class in a service package, but NO stereotype annotation
    from index.repository import init_db
    from scanner.pipeline import build_index
    import tempfile, pathlib

    root = pathlib.Path(tempfile.mkdtemp()) / "repo"
    src = root / "src/main/java/com/acme/service"
    src.mkdir(parents=True)
    (src / "PricingService.java").write_text(
        "package com.acme.service;\npublic class PricingService { public int price() { return 1; } }\n",
        encoding="utf-8",
    )
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    try:
        finding = _layer_finding(explain_class(conn, "PricingService"))
        assert finding["layer"] == "service"
        # name + package agree, but no annotation -> medium (not high)
        assert finding["confidence"] == "medium"
        assert finding["evidence"]
    finally:
        conn.close()


def test_unknown_class_returns_structured_error(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = explain_class(conn, "NoSuchThing")
    assert res["error"] == "not_found"
    assert res["kind"] == "class"
    assert "suggestions" in res
