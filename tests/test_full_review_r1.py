"""Regression tests for round 1 of the 2026-07-21 full-review fixes: the command
-injection window in the gigacode harness, silently-poisoned describe cache,
stale work-dir generations, structured MCP errors, weakest-link confidences and
the endpoint cleanup on class re-index. All fixtures are invented classes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server
from analysis import flat_arch
from analysis.report import collect_baseline
from index.repository import clear_class_members, init_db
from scanner.pipeline import build_index
from summarizer import batch_generate as bg
from summarizer import describe
from summarizer.harness import HarnessConfig, _build_argv
from tests.conftest import write_fixture_repo

_POM = "<project><groupId>com.example</groupId><artifactId>app</artifactId><version>1</version></project>"


def _scan(root: Path, files: dict[str, str]):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    return conn


# --- B1: cmd /c shim refuses argv prompt, stdin mode keeps argv trusted -------

def test_build_argv_refuses_cmd_shim_without_stdin(monkeypatch):
    """cmd.exe re-parses argv content (quotes/&/newlines) — an argv prompt built
    from repo source would be command injection, so a shim must fail closed."""
    import summarizer.harness as h
    monkeypatch.setattr(h.os, "name", "nt", raising=False)
    monkeypatch.setattr(h.shutil, "which", lambda _c: r"C:\tools\gigacode.CMD")
    argv, stdin_text, err = _build_argv(HarnessConfig(prompt="p"))
    assert argv is None and stdin_text is None
    assert "stdin" in err and "shim" in err.lower()


def test_build_argv_shim_with_stdin_keeps_prompt_out_of_argv(monkeypatch):
    import summarizer.harness as h
    monkeypatch.setattr(h.os, "name", "nt", raising=False)
    monkeypatch.setattr(h.shutil, "which", lambda _c: r"C:\tools\gigacode.cmd")
    evil = 'опиши "x\\" & calc & rem "\nвторая строка'
    argv, stdin_text, err = _build_argv(HarnessConfig(prompt=evil, prompt_stdin=True))
    assert err is None
    assert argv[:2] == ["cmd", "/c"]
    assert evil not in argv          # untrusted content never reaches cmd's parser
    assert stdin_text == evil


def test_build_argv_direct_exe_keeps_prompt_in_argv(monkeypatch):
    import summarizer.harness as h
    monkeypatch.setattr(h.shutil, "which", lambda _c: str(Path("gigacode.exe").resolve()))
    argv, stdin_text, err = _build_argv(HarnessConfig(prompt="просто промпт"))
    assert err is None and stdin_text is None
    assert argv[-1] == "просто промпт"


# --- M6: LLM failure must not poison the describe cache -----------------------

class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)   # None = transient failure
        self.enabled = True
        self.config = SimpleNamespace(lang="ru", model="fake")
        self.calls = 0

    def describe(self):
        return "fake-model"

    def complete(self, system, user):
        self.calls += 1
        return self.replies.pop(0)


def test_llm_failure_fallback_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    repo = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo))
    cache = describe._open_cache(str(repo))
    try:
        cid = conn.execute("SELECT id FROM class ORDER BY id LIMIT 1").fetchone()["id"]
        good = json.dumps({"class": "Настоящее описание от LLM.", "methods": {}})

        failing = _FakeLLM([None])
        stats = {k: 0 for k in ("classes", "methods", "from_cache", "from_llm",
                                "from_fallback", "from_imported", "stale_imported")}
        describe._describe_class(conn, cache, cid, failing, force=False,
                                 stats=stats, repo_root=repo)
        assert stats["from_fallback"] == 1

        working = _FakeLLM([good])
        stats2 = dict.fromkeys(stats, 0)
        describe._describe_class(conn, cache, cid, working, force=False,
                                 stats=stats2, repo_root=repo)
        # the poisoned-cache bug: this used to be from_cache=1 / calls=0 forever
        assert working.calls == 1
        assert stats2["from_llm"] == 1 and stats2["from_cache"] == 0
    finally:
        cache.close()
        conn.close()


# --- M8: fresh batch run clears leftovers of a previous generation ------------

def test_fresh_batch_run_clears_stale_workdir(tmp_path, monkeypatch):
    repo = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo))
    arch = flat_arch.export_flat(conn, str(repo))
    conn.close()
    arch_path = tmp_path / "arch.json"
    arch_path.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")

    work_dir = repo / ".reverse" / "batch"
    work_dir.mkdir(parents=True)
    stale = [work_dir / "chunk-9999.json", work_dir / "out-chunk-9999.json",
             work_dir / "chunk-9999-stdout.txt"]
    for f in stale:
        f.write_text("{}", encoding="utf-8")

    # stop before any generator runs: unavailable gigacode ends the fresh path early
    monkeypatch.setattr(bg, "gigacode_available", lambda _c: False)
    bg.main([str(arch_path), "--repo", str(repo)])

    for f in stale:
        assert not f.exists(), f"stale {f.name} survived a fresh run"


# --- M15/M16: mcp_server structured errors + no leaked scan connection --------

def test_read_tool_returns_structured_error_without_repo(monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_repo", None)
    monkeypatch.delenv("LEGACY_REVERSE_REPO", raising=False)
    result = mcp_server.list_endpoints()
    assert result["error"] == "no_repository"
    assert result["suggestions"]


def test_read_tool_returns_structured_error_without_index(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_repo", None)
    monkeypatch.setenv("LEGACY_REVERSE_REPO", str(tmp_path))
    result = mcp_server.get_findings()
    assert result["error"] == "no_index"
    assert result["suggestions"]


def test_scan_repository_failure_leaves_db_unlockable(tmp_path, monkeypatch):
    repo = write_fixture_repo(tmp_path / "repo")

    def boom(conn, path):
        raise ValueError("mid-scan failure")

    monkeypatch.setattr(mcp_server, "build_index", boom)
    monkeypatch.setattr(mcp_server, "_active_repo", None)
    with pytest.raises(ValueError):
        mcp_server.scan_repository(str(repo))
    db = repo / ".reverse" / "index.sqlite3"
    # the regression: the WAL connection stayed open and this unlink failed on Windows
    db.unlink()


# --- M13: context pack confidence is the weakest selected link ----------------

def test_context_pack_confidence_is_weakest_link(tmp_path):
    from analysis.common import min_confidence, conf_str
    from analysis.context_pack import generate_context_pack

    repo = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo))
    try:
        pack = generate_context_pack(conn, "депозит deposit", 8000, 20)
        assert pack["selected_items"]
        expected = conf_str(min_confidence(s["confidence"] for s in pack["selected_items"]))
        assert pack["confidence"] == expected
    finally:
        conn.close()


# --- M14: flat import refuses an ambiguous simple name ------------------------

def test_import_refuses_ambiguous_simple_name(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        "src/main/java/com/example/a/Client.java":
            "package com.example.a;\npublic class Client { }\n",
        "src/main/java/com/example/b/Client.java":
            "package com.example.b;\npublic class Client { }\n",
    })
    try:
        stats = flat_arch.import_flat(conn, str(tmp_path / "repo"), {
            "classes": [{"name": "Client", "description": "Чьё это описание?"}],
        })
        assert stats["classes_matched"] == 0
        assert stats["unmatched_classes"] == ["Client"]
        # the scan's deterministic pass fills class.summary — the check is that the
        # *imported* text landed on neither of the two candidates
        rows = conn.execute("SELECT summary FROM class WHERE simple_name='Client'").fetchall()
        assert all("Чьё это описание?" not in (r["summary"] or "") for r in rows)
    finally:
        conn.close()


# --- M5: clearing a class removes its endpoints too ---------------------------

def test_clear_class_members_removes_endpoints(tmp_path):
    repo = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo))
    try:
        row = conn.execute(
            "SELECT controller_class_id AS cid FROM endpoint "
            "WHERE controller_class_id IS NOT NULL LIMIT 1"
        ).fetchone()
        assert row is not None
        clear_class_members(conn, row["cid"])
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM endpoint WHERE controller_class_id = ?", (row["cid"],)
        ).fetchone()["n"]
        assert left == 0
    finally:
        conn.close()


# --- M12: FQN-typed RestTemplate fields count as external clients -------------

def test_external_clients_counts_fqn_rest_template(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        "src/main/java/com/example/app/RateClient.java":
            "package com.example.app;\n"
            "import org.springframework.web.client.RestTemplate;\n"
            "public class RateClient {\n"
            "    private final RestTemplate rest = new RestTemplate();\n"
            "}\n",
    })
    try:
        baseline = collect_baseline(conn)
        assert baseline["inventory"]["external_clients"] >= 1
    finally:
        conn.close()
