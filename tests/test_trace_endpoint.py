"""Stage 5: trace_endpoint over a Controller -> Service -> Repository mini-project."""

from __future__ import annotations

from analysis.trace import trace_endpoint


def _post_id(conn):
    return conn.execute("SELECT id FROM endpoint WHERE http_method = 'POST'").fetchone()["id"]


def test_full_chain_traced_with_evidence(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = trace_endpoint(conn, endpoint_id=_post_id(conn))

    assert res["endpoint"]["controller_class"] == "DepositController"
    assert res["endpoint"]["controller_method"] == "createDeposit"

    kinds = [s["kind"] for s in res["trace"]]
    assert kinds[0] == "controller_method"
    # service + repository reached via syntactic calls
    assert "service_call" in kinds
    assert "repository_call" in kinds

    symbols = [s["symbol"] for s in res["trace"]]
    assert "DepositService#create" in symbols
    assert "DepositRepository#save" in symbols

    # contract guarantees
    for step in res["trace"]:
        assert step["confidence"] in {"high", "medium", "low", "unknown"}
        assert step["evidence"], f"step {step['step']} has no evidence"
    assert res["confidence"] == "high"  # whole chain found syntactically
    assert res["limitations"]
    codes = {limit["code"] for limit in res["limitations"]}
    assert "syntactic_calls" in codes


def test_lookup_by_method_and_path(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = trace_endpoint(conn, http_method="POST", path_contains="create")
    assert res["endpoint"]["controller_method"] == "createDeposit"
    assert res["query"] == "POST /deposits/create"


def test_overall_confidence_present_and_each_step(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = trace_endpoint(conn, http_method="GET", path_contains="deposits")
    assert "confidence" in res
    assert res["trace"][0]["kind"] == "controller_method"
    assert all("confidence" in s and "evidence" in s for s in res["trace"])


def test_not_found_returns_structured_error_with_suggestions(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    res = trace_endpoint(conn, http_method="DELETE", path_contains="nonexistent")
    assert res["error"] == "not_found"
    assert res["kind"] == "endpoint"
    assert isinstance(res["suggestions"], list) and res["suggestions"]


def test_injection_fallback_when_no_call(tmp_path):
    """Controller injects a service but its handler makes no resolved call ->
    service step via injection (medium), not via call (high)."""
    from index.repository import init_db
    from scanner.pipeline import build_index

    src = tmp_path / "src/main/java/com/acme"
    src.mkdir(parents=True)
    (src / "ThingController.java").write_text(
        """
package com.acme;
import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;
@RestController
@RequiredArgsConstructor
public class ThingController {
    private final ThingService thingService;
    @GetMapping("/things")
    public String list() { return "ok"; }
}
""",
        encoding="utf-8",
    )
    (src / "ThingService.java").write_text(
        "package com.acme;\nimport org.springframework.stereotype.Service;\n@Service\npublic class ThingService { public String all() { return \"x\"; } }\n",
        encoding="utf-8",
    )
    conn = init_db(tmp_path / ".reverse" / "index.sqlite3")
    build_index(conn, str(tmp_path))
    try:
        res = trace_endpoint(conn, http_method="GET", path_contains="things")
        service_step = next((s for s in res["trace"] if s["kind"] in {"service_call", "likely_service"}), None)
        assert service_step is not None
        assert service_step["kind"] == "likely_service"
        assert service_step["confidence"] == "medium"
        assert service_step["evidence"]
    finally:
        conn.close()
