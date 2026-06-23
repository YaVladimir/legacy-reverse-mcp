"""Stage 7: context pack explains why each file is included and respects budgets."""

from __future__ import annotations

from analysis.context_pack import generate_context_pack


def test_selected_items_have_reason_evidence_confidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = generate_context_pack(conn, "create deposit", max_tokens=8000)

    assert res["selected_items"], "should select something for a matching task"
    for item in res["selected_items"]:
        assert item["reason"]
        assert item["confidence"] in {"high", "medium", "low", "unknown"}
        assert item["evidence"]
        assert item["file_path"]
    assert res["context_markdown"].startswith("# Context pack")
    assert res["limitations"]


def test_controller_selected_first_for_endpoint_task(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = generate_context_pack(conn, "deposit", max_tokens=8000)
    symbols = [i["symbol"] for i in res["selected_items"]]
    assert "DepositController" in symbols


def test_max_items_budget_moves_rest_to_excluded(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = generate_context_pack(conn, "deposit", max_tokens=8000, max_items=1)
    assert len(res["selected_items"]) == 1
    assert res["excluded_items"], "remaining candidates must be reported as excluded"
    for ex in res["excluded_items"]:
        assert ex["reason"]


def test_max_tokens_budget_excludes(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    tiny = generate_context_pack(conn, "deposit", max_tokens=30, max_items=20)
    # almost nothing fits in a 30-token budget
    assert tiny["estimated_tokens"] <= 60
    assert tiny["excluded_items"]


def test_no_match_returns_empty_but_structured(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = generate_context_pack(conn, "zzzqqq_nothing_matches", max_tokens=8000)
    assert res["selected_items"] == []
    assert res["confidence"] == "unknown"
    assert res["warnings"]
    assert "context_markdown" in res
