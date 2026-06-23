"""Stage 10: every heuristic MCP tool returns a structured envelope (confidence/
limitations/warnings where applicable) and structured errors."""

from __future__ import annotations

import pytest

import mcp_server
from tests.conftest import write_fixture_repo


@pytest.fixture(scope="module")
def scanned_repo(tmp_path_factory):
    repo = write_fixture_repo(tmp_path_factory.mktemp("mcp") / "repo")
    res = mcp_server.scan_repository(str(repo))
    assert res["status"] in {"scanned", "exists"}
    return repo


def test_list_endpoints_envelope(scanned_repo):
    res = mcp_server.list_endpoints()
    assert res["count"] == 2
    assert res["confidence"] == "high"
    assert res["limitations"] and res["warnings"] == []


def test_module_map_and_overview_carry_limitations(scanned_repo):
    assert mcp_server.get_module_map()["limitations"]
    ov = mcp_server.get_project_overview()
    assert ov["limitations"] and "confidence" in ov


def test_find_code_areas_envelope(scanned_repo):
    res = mcp_server.find_code_areas("deposit")
    assert res["limitations"]
    assert res["confidence"] == "medium"


def test_explain_class_structured(scanned_repo):
    res = mcp_server.explain_class("DepositController")
    for key in ("observed_facts", "inferred_findings", "related_symbols", "confidence", "limitations"):
        assert key in res
    assert all(f["evidence"] for f in res["inferred_findings"])


def test_trace_and_impact_and_pack(scanned_repo):
    tr = mcp_server.trace_endpoint(http_method="POST", path_contains="create")
    assert tr["trace"] and tr["confidence"] and tr["limitations"]

    im = mcp_server.get_change_impact("DepositService")
    assert "direct_impacts" in im and "candidate_impacts" in im and im["limitations"]

    pack = mcp_server.generate_context_pack("deposit")
    assert pack["selected_items"] and pack["limitations"]


def test_structured_errors(scanned_repo):
    assert mcp_server.explain_class("NoSuchClass")["error"] == "not_found"
    assert mcp_server.get_change_impact("NoSuchClass")["error"] == "not_found"
    assert mcp_server.trace_endpoint(http_method="TRACE", path_contains="zzz")["error"] == "not_found"
