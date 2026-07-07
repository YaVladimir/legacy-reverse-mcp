"""Phase 2 description layer: pluggable LLM client, describe pipeline (fallback +
LLM + durable cache), full structural surfacing and topic->code find_feature."""

from __future__ import annotations

import json

import pytest

import mcp_server
from index import queries
from index.repository import get_conn
from summarizer.describe import describe_repo
from summarizer.llm import LLMClient, LLMConfig
from tests.conftest import write_fixture_repo


# ------------------------------------------------------------
# LLM client
# ------------------------------------------------------------

def test_llm_disabled_without_base_url(monkeypatch):
    for var in ("LEGACY_REVERSE_LLM_BASE_URL",):
        monkeypatch.delenv(var, raising=False)
    client = LLMClient(LLMConfig.from_env())
    assert client.enabled is False
    assert client.complete(system="s", user="u") is None
    assert client.describe() == "deterministic"


def test_llm_calls_openai_compatible_endpoint(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "  hello  "}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("summarizer.llm.urllib.request.urlopen", fake_urlopen)
    client = LLMClient(LLMConfig(base_url="http://localhost:1234/v1", model="m"))
    out = client.complete(system="sys", user="usr")
    assert out == "hello"
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "m"
    assert captured["body"]["messages"][0]["role"] == "system"


# ------------------------------------------------------------
# describe pipeline
# ------------------------------------------------------------

def _repo(tmp_path):
    repo = write_fixture_repo(tmp_path / "repo")
    db = repo / ".reverse" / "index.sqlite3"
    from index.repository import init_db
    from scanner.pipeline import build_index

    conn = init_db(db)
    build_index(conn, str(repo))
    conn.close()
    return repo, db


def test_describe_fallback_populates_class_and_method_summaries(tmp_path, monkeypatch):
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    repo, db = _repo(tmp_path)
    conn = get_conn(db)
    stats = describe_repo(conn, str(repo), use_llm=False)
    assert stats["from_llm"] == 0
    assert stats["from_fallback"] == stats["classes"] > 0
    # every method now has a description, not just classes
    missing = conn.execute("SELECT COUNT(*) FROM method WHERE summary IS NULL OR summary = ''").fetchone()[0]
    assert missing == 0
    conn.close()


def test_describe_uses_llm_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGACY_REVERSE_LLM_BASE_URL", "http://localhost:9/v1")

    def fake_complete(self, *, system, user):
        # the per-class call returns the JSON contract; the hierarchy polish calls
        # send "...без JSON" and should get plain text.
        if "без JSON" in user:
            return "Модуль про депозиты."
        return json.dumps({"class": "Класс про депозиты.", "methods": {}})

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    repo, db = _repo(tmp_path)
    conn = get_conn(db)
    stats = describe_repo(conn, str(repo), use_llm=True)
    assert stats["llm_enabled"] is True
    assert stats["from_llm"] == stats["classes"] > 0
    row = conn.execute(
        "SELECT summary FROM class WHERE simple_name = 'DepositController'"
    ).fetchone()
    assert row["summary"] == "Класс про депозиты."
    conn.close()


def test_description_cache_survives_rescan(tmp_path, monkeypatch):
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    repo, db = _repo(tmp_path)

    conn = get_conn(db)
    first = describe_repo(conn, str(repo), use_llm=False)
    conn.close()
    assert first["from_cache"] == 0 and first["from_fallback"] > 0

    # simulate a full re-scan: rebuild index.sqlite3 from scratch (cache is a
    # separate file under .reverse and must NOT be wiped)
    from index.repository import init_db
    from scanner.pipeline import build_index

    db.unlink()
    conn = init_db(db)
    build_index(conn, str(repo))
    second = describe_repo(conn, str(repo), use_llm=False)
    conn.close()
    assert second["from_cache"] == second["classes"] > 0
    assert second["from_fallback"] == 0


# ------------------------------------------------------------
# structural surfacing: class_detail / class_card
# ------------------------------------------------------------

def test_class_card_is_reference_parity(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    card = queries.class_card(conn, "DepositController")
    # reference-architecture field names
    for key in ("id", "pkg", "name", "description", "type", "kind",
                "class_modifiers", "extends", "implements", "fields", "methods"):
        assert key in card, key
    assert card["type"] == "controller"
    assert card["class_modifiers"] == ["public"]
    # method sig carries the PARAMETER NAME (not just the type), like the reference
    create = next(m for m in card["methods"] if m["sig"].startswith("createDeposit"))
    assert "DepositRequest req" in create["sig"]


def test_class_detail_surfaces_extends_and_params(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    d = queries.class_detail(conn, "DepositController")
    assert "extends" in d and "implements" in d and d["type"] == d["role"]
    m = next(m for m in d["methods"] if m["name"] == "createDeposit")
    assert m["parameters"] == [{"name": "req", "type": "DepositRequest"}]


# ------------------------------------------------------------
# find_feature: topic -> classes with bundled methods
# ------------------------------------------------------------

def test_find_feature_bundles_methods(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    res = queries.find_feature(conn, "deposit", limit=10)
    assert res["count"] >= 3
    names = {c["name"] for c in res["classes"]}
    assert {"DepositController", "DepositService"} <= names
    svc = next(c for c in res["classes"] if c["name"] == "DepositService")
    # methods are bundled with their signature so an agent needs no grep
    assert svc["methods"] and any("create(" in m["sig"] for m in svc["methods"])
    assert all("sig" in m for m in svc["methods"])


# ------------------------------------------------------------
# MCP wiring
# ------------------------------------------------------------

@pytest.fixture(scope="module")
def scanned_repo(tmp_path_factory, request):
    repo = write_fixture_repo(tmp_path_factory.mktemp("mcp_desc") / "repo")
    mcp_server.scan_repository(str(repo))
    return repo


def test_mcp_generate_descriptions_and_cards(scanned_repo, monkeypatch):
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    res = mcp_server.generate_descriptions(no_llm=True)
    assert res["status"] == "described" and res["classes"] > 0
    assert res["limitations"] and "confidence" in res

    card = mcp_server.get_class_card("DepositController")
    assert card["type"] == "controller" and card["methods"]
    assert "confidence" in card

    feat = mcp_server.find_feature("deposit")
    assert feat["count"] >= 1 and feat["limitations"]

    assert mcp_server.get_class_card("NoSuchClass")["error"] == "not_found"
