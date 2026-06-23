"""Stage 1: contract tests for the provability models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from models import (
    LIMITATIONS,
    ConfidenceLevel,
    Evidence,
    InferredFinding,
    Limitation,
    ObservedFact,
    limitation,
)


def test_confidence_levels():
    assert {c.value for c in ConfidenceLevel} == {"high", "medium", "low", "unknown"}
    # str-enum: compares/serialises as the plain string
    assert ConfidenceLevel.HIGH == "high"


def test_observed_fact_defaults_high_confidence():
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
    assert fact.confidence is ConfidenceLevel.HIGH


def test_models_serialise_to_plain_json():
    fact = ObservedFact(
        fact_type="mapping_annotation",
        subject="DepositController#createDeposit",
        predicate="maps_http",
        object="POST /deposits/create",
        evidence=[
            Evidence(
                kind="mapping_annotation",
                description="Method createDeposit has @PostMapping('/create')",
                file_path="DepositController.java",
                line_start=42,
                symbol="DepositController#createDeposit",
            )
        ],
    )
    blob = fact.model_dump_json()
    parsed = json.loads(blob)
    assert parsed["confidence"] == "high"
    assert parsed["object"] == "POST /deposits/create"
    assert parsed["evidence"][0]["line_start"] == 42
    assert parsed["evidence"][0]["line_end"] is None
    # mode="json" round-trips the enum to a string too
    assert fact.model_dump(mode="json")["confidence"] == "high"


def test_inferred_finding_requires_evidence():
    with pytest.raises(ValidationError):
        InferredFinding(
            finding_type="spring_layer",
            subject="DepositController",
            summary="Looks like a controller",
            evidence=[],
            confidence=ConfidenceLevel.MEDIUM,
        )


def test_inferred_finding_valid_with_evidence_and_limitations():
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
        limitations=[limitation("spring_proxies")],
    )
    assert finding.confidence is ConfidenceLevel.HIGH
    assert finding.limitations[0].code == "spring_proxies"
    assert json.loads(finding.model_dump_json())["evidence"][0]["kind"] == "annotation"


def test_limitation_catalogue_is_self_consistent():
    # every entry is keyed by its own code, and lookup helper agrees
    for code, lim in LIMITATIONS.items():
        assert isinstance(lim, Limitation)
        assert lim.code == code
        assert limitation(code) is lim
    with pytest.raises(KeyError):
        limitation("does_not_exist")
