"""Stage 6: change-impact separates direct from candidate impacts, all with evidence."""

from __future__ import annotations

from analysis.impact import change_impact


def test_direct_and_candidate_separation(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = change_impact(conn, "DepositService")

    assert "direct_impacts" in res and "candidate_impacts" in res
    # DepositController references DepositService directly (field + call)
    direct_targets = {d["target"] for d in res["direct_impacts"]}
    assert "ru.bank.deposit.DepositController" in direct_targets

    controller = next(d for d in res["direct_impacts"] if d["target"].endswith("DepositController"))
    assert controller["confidence"] == "high"
    assert controller["evidence"]
    assert "reason" in controller


def test_every_impact_has_reason_evidence_confidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = change_impact(conn, "DepositService")
    for impact in res["direct_impacts"] + res["candidate_impacts"]:
        assert impact["reason"]
        assert impact["confidence"] in {"high", "medium", "low", "unknown"}
        assert "evidence" in impact


def test_candidate_endpoints_and_tests(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = change_impact(conn, "DepositService")
    kinds = {c["kind"] for c in res["candidate_impacts"]}
    assert "endpoint" in kinds  # endpoints of the controller that injects the service
    assert "test_candidate" in kinds
    # the affected endpoints should include the deposit endpoints
    ep_targets = {c["target"] for c in res["candidate_impacts"] if c["kind"] == "endpoint"}
    assert any("/deposits" in t for t in ep_targets)


def test_suggested_files_present(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = change_impact(conn, "DepositService")
    assert res["suggested_files_for_context"]
    assert any(p.endswith("DepositService.java") for p in res["suggested_files_for_context"])
    assert res["limitations"]


def test_unknown_symbol_structured_error(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = change_impact(conn, "TotallyMissing")
    assert res["error"] == "not_found"
    assert res["kind"] == "symbol"
    assert "suggestions" in res
