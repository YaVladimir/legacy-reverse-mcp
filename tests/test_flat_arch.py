"""Flat architecture JSON: export parity, export→import round-trip, class/method
matching, imported>fallback priority, gigacode harness, and the MCP wiring."""

from __future__ import annotations

import json
import types

import pytest

import mcp_server
from analysis import flat_arch
from index.repository import get_conn, init_db
from scanner.pipeline import build_index
from summarizer import harness
from summarizer.describe import describe_repo
from tests.conftest import write_fixture_repo


def _scan_describe(tmp_path, monkeypatch):
    """Scan + describe (deterministic, no LLM) the fixture; return (repo, db, open conn)."""
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    repo = write_fixture_repo(tmp_path / "repo")
    db = repo / ".reverse" / "index.sqlite3"
    conn = init_db(db)
    build_index(conn, str(repo))
    conn.close()
    conn = get_conn(db)
    describe_repo(conn, str(repo), use_llm=False)
    conn.commit()
    return repo, db, conn


# ------------------------------------------------------------
# export
# ------------------------------------------------------------

def test_export_flat_reference_shape(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    data = flat_arch.export_flat(conn, str(repo))
    assert {"project", "generated_at", "total_classes", "classes"} <= set(data)
    assert data["total_classes"] == len(data["classes"]) == 5

    c = next(c for c in data["classes"] if c["name"] == "DepositController")
    for key in ("id", "pkg", "name", "description", "type", "kind",
                "class_modifiers", "extends", "methods", "fields", "implements"):
        assert key in c, key
    assert c["type"] == "controller" and c["description"]
    m = next(m for m in c["methods"] if m["sig"].startswith("createDeposit"))
    assert set(m) == {"sig", "modifiers", "description"}
    assert "DepositRequest req" in m["sig"] and m["description"]
    conn.close()


# ------------------------------------------------------------
# round-trip + matching
# ------------------------------------------------------------

def test_round_trip_export_import(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    data = flat_arch.export_flat(conn, str(repo))

    conn.execute("UPDATE class SET summary = NULL")
    conn.execute("UPDATE method SET summary = NULL")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM class WHERE summary IS NOT NULL").fetchone()[0] == 0

    stats = flat_arch.import_flat(conn, str(repo), data)
    assert stats["classes_matched"] == 5
    assert stats["methods_matched"] == 7 and stats["methods_unmatched"] == 0

    row = conn.execute("SELECT summary FROM class WHERE simple_name = 'DepositController'").fetchone()
    assert row["summary"]
    mrow = conn.execute("SELECT summary FROM method WHERE name = 'createDeposit'").fetchone()
    assert mrow["summary"]
    conn.close()


def test_import_then_describe_keeps_imported(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    custom = {
        "classes": [
            {
                "pkg": "ru.bank.deposit", "name": "DepositController",
                "kind": "class", "type": "controller",
                "description": "КАСТОМ: фасад API депозитов.",
                "class_modifiers": ["public"], "extends": None, "implements": None,
                "fields": [],
                "methods": [
                    {"sig": "createDeposit(DepositRequest req)", "modifiers": "public",
                     "description": "КАСТОМ: открывает депозит."}
                ],
            }
        ]
    }
    stats = flat_arch.import_flat(conn, str(repo), custom)
    assert stats["classes_matched"] == 1 and stats["methods_matched"] == 1
    conn.commit()

    # a later describe (fallback) must NOT clobber imported descriptions
    describe_repo(conn, str(repo), use_llm=False)
    conn.commit()
    cls = conn.execute("SELECT summary FROM class WHERE simple_name = 'DepositController'").fetchone()
    assert cls["summary"] == "КАСТОМ: фасад API депозитов."
    m = conn.execute("SELECT summary FROM method WHERE name = 'createDeposit'").fetchone()
    assert m["summary"] == "КАСТОМ: открывает депозит."
    conn.close()


def test_import_goes_stale_when_class_changes(tmp_path, monkeypatch):
    """An imported description must stop winning once the class structurally
    changes — a stale import confidently describing old behaviour is worse
    than the bland fallback."""
    repo, db, conn = _scan_describe(tmp_path, monkeypatch)
    custom = {
        "classes": [
            {
                "pkg": "ru.bank.deposit", "name": "DepositController",
                "kind": "class", "type": "controller",
                "description": "КАСТОМ: фасад API депозитов.",
                "class_modifiers": ["public"], "extends": None, "implements": None,
                "fields": [], "methods": [],
            }
        ]
    }
    assert flat_arch.import_flat(conn, str(repo), custom)["classes_matched"] == 1
    conn.commit()

    # unchanged class: the import must still win
    describe_repo(conn, str(repo), use_llm=False)
    conn.commit()
    row = conn.execute("SELECT summary FROM class WHERE simple_name = 'DepositController'").fetchone()
    assert row["summary"] == "КАСТОМ: фасад API депозитов."
    conn.close()

    # change the class structure (new method) and rebuild the index (scan --force)
    src = repo / "src/main/java/ru/bank/deposit/DepositController.java"
    text = src.read_text(encoding="utf-8")
    src.write_text(
        text.replace(
            "public class DepositController {",
            "public class DepositController {\n    public void closeDeposit(String id) { }\n",
        ),
        encoding="utf-8",
    )
    db.unlink()
    conn = init_db(db)
    build_index(conn, str(repo))

    stats = describe_repo(conn, str(repo), use_llm=False)
    conn.commit()
    assert stats["stale_imported"] >= 1
    row = conn.execute("SELECT summary FROM class WHERE simple_name = 'DepositController'").fetchone()
    assert row["summary"] != "КАСТОМ: фасад API депозитов."  # fallback, not the stale import
    conn.close()


def test_import_reports_unmatched(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    data = {"classes": [{"pkg": "ru.bank.deposit", "name": "GhostClass", "description": "x"}]}
    stats = flat_arch.import_flat(conn, str(repo), data)
    assert stats["classes_matched"] == 0
    assert "ru.bank.deposit.GhostClass" in stats["unmatched_classes"]
    conn.close()


# ------------------------------------------------------------
# gigacode harness
# ------------------------------------------------------------

def test_harness_imports_fake_gigacode_output(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    flat = {
        "project": "x", "generated_at": "2026-06-29", "total_classes": 1,
        "classes": [
            {
                "pkg": "ru.bank.deposit", "name": "DepositService",
                "kind": "class", "type": "service",
                "description": "ГИГА: сервис депозитов.",
                "class_modifiers": ["public"], "extends": None, "implements": None, "fields": [],
                "methods": [
                    {"sig": "create(DepositRequest req)", "modifiers": "public",
                     "description": "ГИГА: создаёт депозит."}
                ],
            }
        ],
    }
    monkeypatch.setattr(harness.shutil, "which", lambda c: r"C:\fake\gigacode.exe")
    monkeypatch.setattr(
        harness, "run_tree_captured",
        lambda argv, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(flat), stderr="", error=None
        ),
    )
    stats = harness.generate_architecture(conn, str(repo))
    assert stats["status"] == "imported"
    assert stats["classes_matched"] == 1 and stats["methods_matched"] == 1
    row = conn.execute("SELECT summary FROM class WHERE simple_name = 'DepositService'").fetchone()
    assert row["summary"] == "ГИГА: сервис депозитов."
    conn.close()


def test_harness_missing_gigacode_is_structured(tmp_path, monkeypatch):
    repo, _db, conn = _scan_describe(tmp_path, monkeypatch)
    monkeypatch.setattr(harness.shutil, "which", lambda c: None)
    monkeypatch.delenv("GIGACODE", raising=False)
    monkeypatch.delenv("GIGACODE_CLI", raising=False)
    stats = harness.generate_architecture(conn, str(repo))
    assert stats["status"] == "error" and stats.get("hint")
    conn.close()


# ------------------------------------------------------------
# MCP wiring
# ------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_repo(tmp_path_factory):
    repo = write_fixture_repo(tmp_path_factory.mktemp("mcp_arch") / "repo")
    mcp_server.scan_repository(str(repo))
    mcp_server.generate_descriptions(no_llm=True)
    return repo


def test_mcp_export_then_import(mcp_repo, tmp_path):
    out = tmp_path / "arch.json"
    res = mcp_server.export_architecture(out_path=str(out))
    assert res["status"] == "written" and out.exists()
    assert res["total_classes"] == 5

    imp = mcp_server.import_architecture(str(out))
    assert imp["status"] == "imported" and imp["classes_matched"] >= 1
    assert mcp_server.import_architecture("C:/nope/none.json")["error"] == "not_found"


def test_mcp_generate_architecture_without_gigacode(mcp_repo, monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda c: None)
    monkeypatch.delenv("GIGACODE", raising=False)
    monkeypatch.delenv("GIGACODE_CLI", raising=False)
    res = mcp_server.generate_architecture()
    assert res["status"] == "error" and res["confidence"] == "low"
    assert res["limitations"]
