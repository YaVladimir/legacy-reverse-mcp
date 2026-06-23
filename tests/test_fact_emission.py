"""Stage 3: scanning a repo records observed facts with evidence, and the
existing tools keep working unchanged."""

from __future__ import annotations

from index import queries, repository as repo
from scanner.fact_emitter import FactConfig, class_observed_facts
from scanner.java_parser import parse_source


# ------------------------------------------------------------
# pure unit: facts straight off a ParsedClass
# ------------------------------------------------------------

_CONTROLLER = """
package ru.bank.deposit;

import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/deposits")
@RequiredArgsConstructor
public class DepositController {
    private final DepositService depositService;

    @PostMapping("/create")
    public Deposit createDeposit(DepositRequest req) {
        return depositService.create(req);
    }
}
"""


def _facts_by_type(facts):
    out: dict[str, list] = {}
    for f in facts:
        out.setdefault(f.fact_type, []).append(f)
    return out


def test_class_observed_facts_cover_the_listed_categories():
    parsed = parse_source(_CONTROLLER.encode(), "DepositController.java")
    pc = parsed.classes[0]
    facts = class_observed_facts(pc, FactConfig())
    by_type = _facts_by_type(facts)

    assert "package_declaration" in by_type
    assert "class_declaration" in by_type
    # both class annotations recorded
    class_anns = {f.object for f in by_type["class_annotation"]}
    assert "@RestController" in class_anns and "@RequestMapping" in class_anns
    # the REST mapping fact resolves verb + full path
    mappings = by_type["mapping_annotation"]
    assert len(mappings) == 1
    mp = mappings[0]
    assert mp.object == "POST /deposits/create"
    assert mp.confidence == "high"
    # injected field recorded
    field_subjects = {f.subject for f in by_type.get("field", [])}
    assert "ru.bank.deposit.DepositController.depositService" in field_subjects


def test_every_observed_fact_has_evidence_with_file_path():
    parsed = parse_source(_CONTROLLER.encode(), "DepositController.java")
    facts = class_observed_facts(parsed.classes[0], FactConfig())
    assert facts
    for f in facts:
        assert f.evidence, f"fact {f.fact_type} has no evidence"
        for ev in f.evidence:
            assert ev.file_path, f"evidence for {f.fact_type} missing file_path"


def test_mapping_evidence_has_file_and_lines():
    parsed = parse_source(_CONTROLLER.encode(), "DepositController.java")
    facts = class_observed_facts(parsed.classes[0], FactConfig())
    mapping = next(f for f in facts if f.fact_type == "mapping_annotation")
    ev = mapping.evidence[0]
    assert ev.file_path == "DepositController.java"
    assert ev.line_start is not None  # @PostMapping line is recoverable
    assert ev.symbol == "DepositController#createDeposit"


def test_uninteresting_methods_and_fields_are_skipped_by_default():
    src = """
package x;
public class Plain {
    private int a;
    public void noop() {}
}
"""
    facts = class_observed_facts(parse_source(src.encode(), "Plain.java").classes[0])
    types = {f.fact_type for f in facts}
    # only declaration-level facts; no method/field facts for unannotated members
    assert "method_declaration" not in types
    assert "field" not in types
    assert {"package_declaration", "class_declaration"} <= types


def test_record_all_flags_emit_plain_members():
    src = """
package x;
public class Plain {
    private int a;
    public void noop() {}
}
"""
    cfg = FactConfig(record_all_methods=True, record_all_fields=True)
    facts = class_observed_facts(parse_source(src.encode(), "Plain.java").classes[0], cfg)
    types = {f.fact_type for f in facts}
    assert "method_declaration" in types
    assert "field" in types


# ------------------------------------------------------------
# end-to-end: scan the fixture repo
# ------------------------------------------------------------

def test_scan_populates_observed_facts(scan_summary_and_conn):
    summary, conn = scan_summary_and_conn
    assert summary["observed_facts"] > 0
    assert repo.count_observed_facts(conn) == summary["observed_facts"]

    # endpoints have mapping facts with file_path + lines
    mappings = repo.list_observed_facts(conn, fact_type="mapping_annotation")
    assert len(mappings) == 2  # POST /create + GET /{id}
    objs = {m["object"] for m in mappings}
    assert objs == {"POST /deposits/create", "GET /deposits/{id}"}
    for m in mappings:
        ev = m["evidence"][0]
        assert ev["file_path"] and ev["file_path"].endswith("DepositController.java")
        assert ev["line_start"] is not None

    # the @Scheduled method annotation is captured
    sched = repo.list_observed_facts(conn, fact_type="method_annotation")
    assert any(s["object"] == "@Scheduled" for s in sched)

    # stereotype annotations recorded for the service/repository
    svc = repo.list_observed_facts(
        conn, subject="ru.bank.deposit.DepositService", fact_type="class_annotation"
    )
    assert any(s["object"] == "@Service" for s in svc)


def test_existing_tools_still_work_after_fact_emission(scan_summary_and_conn):
    summary, conn = scan_summary_and_conn
    # baseline contract: counts unchanged by the new layer
    assert summary["classes"] == 5
    assert summary["endpoints"] == 2

    eps = queries.list_endpoints(conn)
    assert len(eps) == 2
    paths = {e["full_path"] for e in eps}
    assert paths == {"/deposits/create", "/deposits/{id}"}

    detail = queries.class_detail(conn, "ru.bank.deposit.DepositController")
    assert detail["role"] == "controller"
    assert len(detail["endpoints"]) == 2
