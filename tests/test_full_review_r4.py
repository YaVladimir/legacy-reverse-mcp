"""Regression tests for round 4 of the 2026-07-21 full-review fixes: the minor
findings (package dirs named build/, dependencyManagement, record components,
*Impl name signal, ambiguous explain, sidecar union, CBMC argv hygiene) and the
test debt — CLI commands and mcp_server wrappers had zero coverage. All classes
are invented."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import mcp_server
import utils.cbmc_config as cc
from analysis.explain import explain_class
from analysis.layers import compute_low_confidence_findings
from cli import cli as cli_group
from index.repository import init_db
from scanner.pipeline import build_index
from summarizer import batch_generate as bg
from tests.conftest import write_fixture_repo

_POM = "<project><groupId>com.example</groupId><artifactId>app</artifactId><version>1</version></project>"
_SRC = "src/main/java/com/example/app"


def _scan(root: Path, files: dict[str, str]):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    return conn


# --- CLI: the whole scan/report/export/import surface actually runs ----------

def test_cli_scan_report_export_import_roundtrip(tmp_path):
    repo = write_fixture_repo(tmp_path / "repo")
    out = tmp_path / "arch.json"
    runner = CliRunner()

    res = runner.invoke(cli_group, ["scan", "--repo", str(repo), "--report"])
    assert res.exit_code == 0, res.output
    assert (repo / ".reverse" / "index.sqlite3").exists()
    assert list((repo / ".reverse" / "reports").glob("*.md"))

    # second scan without --force: keeps the index, still exits 0
    res = runner.invoke(cli_group, ["scan", "--repo", str(repo), "--report"])
    assert res.exit_code == 0 and "already exists" in res.output

    res = runner.invoke(cli_group, ["export-arch", "--repo", str(repo), "--out", str(out)])
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total_classes"] > 0

    data["classes"][0]["description"] = "CLI: описание из импорта."
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = runner.invoke(cli_group, ["import-arch", "--repo", str(repo), "--in", str(out)])
    assert res.exit_code == 0 and "Imported" in res.output

    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    res = runner.invoke(cli_group, ["import-arch", "--repo", str(repo), "--in", str(bad)])
    assert res.exit_code != 0


def test_cli_report_without_index_fails_structured(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = CliRunner().invoke(cli_group, ["report", "--repo", str(empty)])
    assert res.exit_code != 0 and "No index" in res.output


def test_cli_generate_arch_without_gigacode_fails_with_hint(tmp_path, monkeypatch):
    import summarizer.harness as h
    repo = write_fixture_repo(tmp_path / "repo")
    assert CliRunner().invoke(cli_group, ["scan", "--repo", str(repo)]).exit_code == 0
    monkeypatch.setattr(h.shutil, "which", lambda _c: None)
    monkeypatch.delenv("GIGACODE", raising=False)
    monkeypatch.delenv("GIGACODE_CLI", raising=False)
    res = CliRunner().invoke(cli_group, ["generate-arch", "--repo", str(repo)])
    assert res.exit_code != 0 and "not found" in res.output


# --- mcp_server wrappers: force rescan + the uncovered read tools -------------

def test_mcp_scan_force_and_wrapper_envelopes(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_repo", None)
    repo = write_fixture_repo(tmp_path / "repo")

    assert mcp_server.scan_repository(str(repo))["status"] == "scanned"
    assert mcp_server.scan_repository(str(repo))["status"] == "exists"
    assert mcp_server.scan_repository(str(repo), force=True)["status"] == "scanned"

    summary = mcp_server.get_class_summary("DepositController")
    assert summary["summary"] and summary["confidence"] == "medium" and summary["limitations"]

    findings = mcp_server.get_findings()
    assert "findings" in findings and findings["limitations"]

    config = mcp_server.get_config()
    assert "properties" in config and config["confidence"] == "high"


# --- package directories named like build output ------------------------------

def test_package_named_build_is_scanned(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        "src/main/java/com/example/build/PipelineBuilder.java":
            "package com.example.build;\npublic class PipelineBuilder { }\n",
    })
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM class WHERE simple_name = 'PipelineBuilder'"
        ).fetchone()
        assert row["n"] == 1  # the regression: silently dropped as build output
    finally:
        conn.close()


# --- dependencyManagement is not a dependency ---------------------------------

def test_dependency_management_entries_are_not_counted(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": (
            "<project><groupId>com.example</groupId><artifactId>app</artifactId><version>1</version>"
            "<dependencyManagement><dependencies>"
            "<dependency><groupId>g</groupId><artifactId>managed-a</artifactId><version>1</version></dependency>"
            "<dependency><groupId>g</groupId><artifactId>managed-b</artifactId><version>1</version></dependency>"
            "</dependencies></dependencyManagement>"
            "<dependencies>"
            "<dependency><groupId>org.example</groupId><artifactId>real-dep</artifactId><version>6</version></dependency>"
            "</dependencies></project>"
        ),
        f"{_SRC}/Widget.java": "package com.example.app;\npublic class Widget { }\n",
    })
    try:
        arts = {r["artifact_id"] for r in conn.execute("SELECT artifact_id FROM external_dependency")}
        assert arts == {"real-dep"}
    finally:
        conn.close()


# --- record components become fields + dependency edges -----------------------

def test_record_components_are_fields_with_edges(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/DealDto.java":
            "package com.example.app;\n"
            "public record DealDto(CustomerRef customer, long amount) {}\n",
        f"{_SRC}/CustomerRef.java":
            "package com.example.app;\npublic class CustomerRef { }\n",
    })
    try:
        fields = {r["name"]: r["type_fqn"] for r in conn.execute(
            "SELECT f.name, f.type_fqn FROM field f JOIN class c ON c.id = f.class_id "
            "WHERE c.simple_name = 'DealDto'"
        )}
        # the same-package pass resolves the component type to its FQN
        assert fields == {"customer": "com.example.app.CustomerRef", "amount": "long"}
        # the point of the fix: change-impact now sees the record via its field
        from analysis.impact import change_impact
        impacted = {d["target"] for d in change_impact(conn, "CustomerRef")["direct_impacts"]}
        assert "com.example.app.DealDto" in impacted
    finally:
        conn.close()


# --- *Impl carries the same name signal ---------------------------------------

def test_service_impl_suffix_counts_as_name_signal(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        # XML-wired legacy: no annotations at all
        "src/main/java/com/example/app/service/WidgetServiceImpl.java":
            "package com.example.app.service;\n"
            "public class WidgetServiceImpl { public void run() { } }\n",
    })
    try:
        findings = compute_low_confidence_findings(conn)
        subjects = [f.subject for f in findings if "Possibly a service" in f.summary]
        assert "com.example.app.service.WidgetServiceImpl" in subjects
    finally:
        conn.close()


# --- ambiguous simple name in explain is said out loud ------------------------

def test_explain_ambiguous_simple_name_warns(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        "src/main/java/com/example/a/Client.java":
            "package com.example.a;\npublic class Client { }\n",
        "src/main/java/com/example/b/Client.java":
            "package com.example.b;\npublic class Client { }\n",
    })
    try:
        result = explain_class(conn, "Client")
        assert any("matches 2" in w for w in result["warnings"])
        codes = {lim["code"] for lim in result["limitations"]}
        assert "ambiguous_simple_name" in codes
    finally:
        conn.close()


# --- sidecar union on complementary retry -------------------------------------

def test_partial_retry_sidecar_is_unioned_not_replaced(tmp_path, monkeypatch):
    chunk_path = tmp_path / "chunk-0000.json"
    chunk_path.write_text('{"classes": []}', encoding="utf-8")
    sidecar = tmp_path / "chunk-0000-stdout.txt"
    sidecar.write_text(
        '{"classes": [{"id": "a/OldOne", "description": "OLD"}]}', encoding="utf-8"
    )

    class _Proc:
        returncode = 0
        stdout = '{"classes": [{"id": "b/NewOne", "description": "NEW"}]}'
        stderr = ""
        error = None

    monkeypatch.setattr(bg, "_build_argv", lambda cfg: (["gigacode", "-p", cfg.prompt], None, None))
    monkeypatch.setattr(bg, "run_tree_captured", lambda *a, **k: _Proc())

    _idx, data, _info = bg._run_single_chunk(chunk_path, 0, 1, "gigacode", ["-p"], 10.0, None)
    ids = {c["id"] for c in data["classes"]}
    assert ids == {"a/OldOne", "b/NewOne"}
    on_disk = json.loads(sidecar.read_text(encoding="utf-8"))
    assert {c["id"] for c in on_disk["classes"]} == {"a/OldOne", "b/NewOne"}


# --- CBMC argv hygiene + cross-filesystem root_path ---------------------------

def test_cbmc_call_rejects_leading_dash_value(tmp_path):
    fake_bin = tmp_path / "cbmc-bin"
    fake_bin.write_text("", encoding="utf-8")
    result, info = cc.cbmc_call(
        "search_graph", {"name_pattern": "--project=other-local"}, binary=str(fake_bin)
    )
    assert result is None and "unsafe argument" in info["error"]


@pytest.mark.skipif(os.name != "nt", reason="drive-grafting only happens on Windows")
def test_resolved_path_key_refuses_posix_absolute_on_windows():
    # a WSL-built index reports /home/... — resolve() would graft the current
    # drive and manufacture a false root_path match
    assert bg._resolved_path_key("/home/user/proj") is None
    assert bg._resolved_path_key(r"C:\home\user\proj") is not None


def test_verified_snippet_rejects_relocated_copy():
    result = {
        "qualified_name": "shaded.com.example.app.OrderService",
        "file_path": "/repo/target/shaded/com/example/app/OrderService.java",
        "source": "class OrderService {}",
        "caller_names": [], "callee_names": [],
    }
    assert bg._verified_snippet(result, "com.example.app.OrderService") is None
