"""Stage 2: schema + repository round-trip for facts / findings / evidence / limitations."""

from __future__ import annotations

from index import repository as repo
from models import (
    ConfidenceLevel,
    Evidence,
    InferredFinding,
    Limitation,
    ObservedFact,
    limitation,
)


def _new_db(tmp_path):
    return repo.init_db(tmp_path / ".reverse" / "index.sqlite3")


def test_new_tables_exist(tmp_path):
    conn = _new_db(tmp_path)
    try:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"observed_facts", "inferred_findings", "evidence", "limitations"} <= names
        # existing tables are untouched
        assert {"class", "endpoint", "finding"} <= names
    finally:
        conn.close()


def test_observed_fact_round_trip(tmp_path):
    conn = _new_db(tmp_path)
    try:
        fact = ObservedFact(
            fact_type="class_annotation",
            subject="ru.bank.deposit.DepositController",
            predicate="is_annotated_with",
            object="@RestController",
            evidence=[
                Evidence(
                    kind="annotation",
                    description="Class DepositController is annotated with @RestController",
                    file_path="src/main/java/ru/bank/deposit/DepositController.java",
                    line_start=18,
                    line_end=18,
                    symbol="DepositController",
                )
            ],
        )
        fact_id = repo.insert_observed_fact(conn, fact)
        assert fact_id > 0

        rows = repo.list_observed_facts(conn, subject="ru.bank.deposit.DepositController")
        assert len(rows) == 1
        got = rows[0]
        assert got["fact_type"] == "class_annotation"
        assert got["object"] == "@RestController"
        assert got["confidence"] == "high"
        assert len(got["evidence"]) == 1
        assert got["evidence"][0]["line_start"] == 18
        assert got["evidence"][0]["symbol"] == "DepositController"
        assert repo.count_observed_facts(conn) == 1
    finally:
        conn.close()


def test_inferred_finding_round_trip_with_evidence_and_limitations(tmp_path):
    conn = _new_db(tmp_path)
    try:
        finding = InferredFinding(
            finding_type="spring_layer",
            subject="ru.bank.deposit.DepositController",
            summary="Belongs to the controller layer",
            evidence=[
                Evidence(
                    kind="annotation",
                    description="annotated with @RestController",
                    symbol="DepositController",
                )
            ],
            confidence=ConfidenceLevel.HIGH,
            limitations=[limitation("spring_proxies"), Limitation(code="custom", description="bespoke caveat")],
        )
        fid = repo.insert_inferred_finding(conn, finding)
        assert fid > 0

        rows = repo.list_inferred_findings(conn)
        assert len(rows) == 1
        got = rows[0]
        assert got["confidence"] == "high"
        assert len(got["evidence"]) == 1
        codes = {limit["code"] for limit in got["limitations"]}
        assert codes == {"spring_proxies", "custom"}
    finally:
        conn.close()


def test_clear_isolates_owners(tmp_path):
    conn = _new_db(tmp_path)
    try:
        repo.insert_observed_fact(
            conn,
            ObservedFact(
                fact_type="class_declaration",
                subject="A",
                predicate="declared",
                evidence=[Evidence(kind="class_declaration", description="class A", file_path="A.java")],
            ),
        )
        repo.insert_inferred_finding(
            conn,
            InferredFinding(
                finding_type="spring_layer",
                subject="A",
                summary="service",
                evidence=[Evidence(kind="annotation", description="@Service")],
                confidence=ConfidenceLevel.MEDIUM,
            ),
        )
        # clearing facts must leave findings (and their evidence) intact
        repo.clear_observed_facts(conn)
        assert repo.count_observed_facts(conn) == 0
        findings = repo.list_inferred_findings(conn)
        assert len(findings) == 1
        assert len(findings[0]["evidence"]) == 1
        # no orphaned observed-fact evidence remains
        leftover = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE owner_type = ?", (repo.OWNER_OBSERVED_FACT,)
        ).fetchone()[0]
        assert leftover == 0
    finally:
        conn.close()
