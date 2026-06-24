"""Phase C: scan persists low-confidence inferred findings; report reads them."""

from __future__ import annotations

from analysis.report import collect_baseline
from index import repository as repo
from index.repository import init_db
from scanner.pipeline import build_index


def _build(tmp_path):
    src = tmp_path / "src/main/java/bank/service"
    src.mkdir(parents=True)
    # no stereotype annotation -> role 'unknown'; name 'Manager' + package 'service'
    # are a low-confidence layer hint
    (src / "PaymentManager.java").write_text(
        "package bank.service;\npublic class PaymentManager { public void pay() {} }\n",
        encoding="utf-8",
    )
    conn = init_db(tmp_path / ".reverse" / "index.sqlite3")
    summary = build_index(conn, str(tmp_path))
    return summary, conn


def test_scan_persists_inferred_findings(tmp_path):
    summary, conn = _build(tmp_path)
    try:
        assert summary["inferred_findings"] >= 1
        rows = repo.list_inferred_findings(conn, finding_type="spring_layer")
        assert any(f["subject"] == "bank.service.PaymentManager" for f in rows)
        f = next(f for f in rows if f["subject"] == "bank.service.PaymentManager")
        assert f["confidence"] == "low"
        assert f["evidence"], "persisted finding must keep its evidence"
    finally:
        conn.close()


def test_report_reads_persisted_findings(tmp_path):
    _, conn = _build(tmp_path)
    try:
        data = collect_baseline(conn)
        subjects = {f["subject"] for f in data["low_confidence_findings"]}
        assert "bank.service.PaymentManager" in subjects
    finally:
        conn.close()


def test_rescan_does_not_duplicate(tmp_path):
    _, conn = _build(tmp_path)
    try:
        before = len(repo.list_inferred_findings(conn))
        # re-run the indexing pipeline on the same connection
        build_index(conn, str(tmp_path))
        after = len(repo.list_inferred_findings(conn))
        assert after == before  # clear_inferred_findings runs each scan
    finally:
        conn.close()
