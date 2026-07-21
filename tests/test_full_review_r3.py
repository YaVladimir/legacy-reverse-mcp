"""Regression tests for round 3 of the 2026-07-21 full-review fixes — index
completeness: nested types, the test-source filter, generic type arguments,
Maven inter-module edges and process-tree-safe subprocess execution. All
classes are invented."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from index.repository import init_db
from scanner.pipeline import build_index
from utils.proc import run_tree_captured

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


# --- M1: nested member types are indexed --------------------------------------

def test_nested_types_are_indexed_with_their_endpoints(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/ReportFacade.java": """
package com.example.app;
import org.springframework.web.bind.annotation.*;
public class ReportFacade {
    @RestController
    @RequestMapping("/admin/reports")
    public static class AdminReportApi {
        @GetMapping("/run")
        public String run() { return "ok"; }
    }
    public enum Status { OPEN, CLOSED }
}
""",
    })
    try:
        fqns = {r["fqn"]: r["kind"] for r in conn.execute("SELECT fqn, kind FROM class")}
        # the regression: only ReportFacade existed; the nested controller and
        # enum were invisible to search/trace/impact
        assert "com.example.app.ReportFacade.AdminReportApi" in fqns
        assert fqns["com.example.app.ReportFacade.Status"] == "enum"
        eps = [r["full_path"] for r in conn.execute("SELECT full_path FROM endpoint")]
        assert "/admin/reports/run" in eps
    finally:
        conn.close()


# --- M2: test filter matches the repo-RELATIVE path ---------------------------

def test_repo_cloned_under_src_test_is_still_scanned(tmp_path):
    # absolute prefix contains src/test — previously the whole repo scanned empty
    root = tmp_path / "src" / "test" / "myrepo"
    conn = _scan(root, {
        "pom.xml": _POM,
        f"{_SRC}/Widget.java": "package com.example.app;\npublic class Widget { }\n",
    })
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM class").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()


def test_nested_module_test_sources_are_skipped(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        "svc/pom.xml": _POM.replace("app", "svc"),
        "svc/src/main/java/com/example/svc/Widget.java":
            "package com.example.svc;\npublic class Widget { }\n",
        "svc/src/test/java/com/example/svc/WidgetTest.java":
            "package com.example.svc;\npublic class WidgetTest { }\n",
    })
    try:
        names = {r["simple_name"] for r in conn.execute("SELECT simple_name FROM class")}
        assert "Widget" in names
        assert "WidgetTest" not in names
    finally:
        conn.close()


# --- M3: generic arguments produce dependency edges ---------------------------

def test_collection_injection_creates_edge_on_element_type(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/CheckRunner.java": """
package com.example.app;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
@Service
public class CheckRunner {
    @Autowired
    private List<WidgetValidator> validators;
}
""",
        f"{_SRC}/WidgetValidator.java":
            "package com.example.app;\npublic class WidgetValidator { }\n",
    })
    try:
        # the written element type survives the import-FQN rewrite ...
        t = conn.execute(
            "SELECT type_fqn FROM field WHERE name = 'validators'"
        ).fetchone()["type_fqn"]
        assert t == "java.util.List<WidgetValidator>"
        # ... and yields a real dependency edge (change-impact relies on it)
        edge = conn.execute(
            "SELECT COUNT(*) AS n FROM class_dependency cd "
            "JOIN class f ON f.id = cd.from_class_id JOIN class t ON t.id = cd.to_class_id "
            "WHERE f.simple_name = 'CheckRunner' AND t.simple_name = 'WidgetValidator'"
        ).fetchone()["n"]
        assert edge >= 1
    finally:
        conn.close()


# --- M4: Maven sibling modules become module edges, not external deps ---------

def test_maven_inter_module_dependency_creates_edge(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": (
            "<project><groupId>com.example</groupId><artifactId>parent</artifactId>"
            "<version>1</version><packaging>pom</packaging>"
            "<modules><module>core</module><module>web</module></modules></project>"
        ),
        "core/pom.xml": (
            "<project><groupId>com.example</groupId><artifactId>app-core</artifactId>"
            "<version>1</version></project>"
        ),
        "web/pom.xml": (
            "<project><groupId>com.example</groupId><artifactId>app-web</artifactId>"
            "<version>1</version><dependencies><dependency>"
            "<groupId>com.example</groupId><artifactId>app-core</artifactId>"
            "<version>1</version></dependency></dependencies></project>"
        ),
        "core/src/main/java/com/example/core/CoreThing.java":
            "package com.example.core;\npublic class CoreThing { }\n",
        "web/src/main/java/com/example/web/WebThing.java":
            "package com.example.web;\npublic class WebThing { }\n",
    })
    try:
        edges = conn.execute(
            "SELECT COUNT(*) AS n FROM module_dependency md "
            "JOIN module m_to ON m_to.id = md.to_module_id "
            "WHERE m_to.path LIKE '%core%'"
        ).fetchone()["n"]
        assert edges >= 1  # the regression: always 0 on pure-Maven repos
        phantom = conn.execute(
            "SELECT COUNT(*) AS n FROM external_dependency WHERE artifact_id = 'app-core'"
        ).fetchone()["n"]
        assert phantom == 0
    finally:
        conn.close()


# --- M7: process-tree-safe subprocess helper ----------------------------------

def test_run_tree_captured_success_and_stdin():
    res = run_tree_captured(
        [sys.executable, "-c", "import sys; print('echo:' + sys.stdin.read())"],
        timeout=30, input_text="привет",
    )
    assert res.error is None and res.returncode == 0
    assert "echo:привет" in res.stdout


def test_run_tree_captured_timeout_kills_and_returns():
    start = time.monotonic()
    res = run_tree_captured(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=1.5,
    )
    assert res.returncode is None
    assert res.error and res.error.startswith("timed out")
    assert time.monotonic() - start < 30  # returned promptly, no hanging communicate()
