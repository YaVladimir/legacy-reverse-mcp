"""Stage 8: baseline report (markdown + json), incl. graceful empty-project case."""

from __future__ import annotations

import json

from analysis.report import collect_baseline, render_markdown, write_baseline
from index.repository import init_db
from scanner.pipeline import build_index


def test_report_written_in_both_formats(scan_summary_and_conn, tmp_path):
    _, conn = scan_summary_and_conn
    out = write_baseline(conn, tmp_path, report_dir=tmp_path / "reports")

    md = (tmp_path / "reports" / "baseline.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "reports" / "baseline.json").read_text(encoding="utf-8"))

    assert md.startswith("# Legacy Reverse Baseline")
    assert "## Inventory" in md
    assert "## Limitations" in md
    assert "## Low-confidence findings" in md

    inv = js["inventory"]
    assert inv["controllers"] >= 1
    assert inv["services"] >= 1
    assert inv["repositories"] >= 1
    assert inv["entities"] >= 1
    assert inv["endpoints"] == 2
    assert js["limitations"], "report must list limitations explicitly"
    assert "low_confidence_findings" in js


def test_scheduled_jobs_counted(scan_summary_and_conn):
    _, conn = scan_summary_and_conn
    data = collect_baseline(conn)
    # DepositService.sweep() is @Scheduled
    assert data["inventory"]["scheduled_jobs"] >= 1


def test_report_does_not_crash_on_empty_project(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo))
    try:
        data = collect_baseline(conn)
        md = render_markdown(data)
        assert data["inventory"]["classes"] == 0
        assert data["inventory"]["endpoints"] == 0
        assert data["limitations"]
        assert "## Limitations" in md
        assert "(none)" in md  # no low-confidence findings
    finally:
        conn.close()
