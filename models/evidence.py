"""Provability primitives: Evidence, Confidence, ObservedFact, InferredFinding.

Design rules (enforced here so the rest of the codebase can rely on them):

* An **ObservedFact** is something read directly from source/config/structure
  (e.g. "class DepositController is annotated with @RestController"). It defaults
  to ``high`` confidence because it is not a guess.
* An **InferredFinding** is a heuristic conclusion drawn from observed facts
  (e.g. "DepositController is a Spring controller layer"). It **must** carry at
  least one piece of ``evidence`` — a finding with no evidence is a bug, so the
  model refuses to construct one. It also carries an explicit ``confidence`` and
  may list the ``limitations`` that bound the inference.

Everything is a pydantic ``BaseModel`` so it serialises to plain JSON for MCP
responses with ``model_dump(mode="json")`` / ``model_dump_json()``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ConfidenceLevel(str, Enum):
    """How much to trust a fact or finding.

    * ``high``    — a direct fact, or an inference over direct (unambiguous) links.
    * ``medium``  — a heuristic inference from several weak-ish signals.
    * ``low``     — a guess from naming / package / keyword similarity.
    * ``unknown`` — cannot be assessed.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    """One concrete reason a fact or finding is asserted.

    ``file_path`` is expected whenever the evidence comes from a source file;
    ``line_start`` / ``line_end`` are filled when the parser can locate the
    construct (they may be ``None`` when lines are hard to recover). ``symbol``
    names the construct the evidence is about, e.g. ``DepositController`` or
    ``DepositController#createDeposit``.
    """

    kind: str = Field(..., description="Category of evidence, e.g. 'annotation', 'mapping_annotation', 'field', 'import'.")
    description: str = Field(..., description="Human-readable reason, self-contained.")
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    source: str = Field("source", description="Where the evidence came from: 'source' | 'config' | 'build' | 'structure'.")


class Limitation(BaseModel):
    """A bounded caveat on a result. Use a stable ``code`` so consumers can match."""

    code: str
    description: str


class ObservedFact(BaseModel):
    """A fact extracted directly from source / config / project structure.

    Modelled as a (subject, predicate, object) triple so heterogeneous facts share
    one shape, e.g. ``("DepositController", "is_annotated_with", "@RestController")``
    or ``("DepositController#createDeposit", "maps_http", "POST /deposits/create")``.
    """

    fact_type: str = Field(..., description="e.g. 'class_declaration', 'class_annotation', 'mapping_annotation', 'field'.")
    subject: str
    predicate: str
    object: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH


class InferredFinding(BaseModel):
    """A heuristic conclusion. Must be backed by at least one Evidence item."""

    # re-validate on attribute assignment: without this, `f.evidence = []` after
    # construction silently bypasses the no-evidence invariant
    model_config = {"validate_assignment": True}

    finding_type: str = Field(..., description="e.g. 'spring_layer', 'endpoint_purpose', 'change_impact'.")
    subject: str
    summary: str
    evidence: list[Evidence] = Field(..., min_length=1)
    confidence: ConfidenceLevel
    limitations: list[Limitation] = Field(default_factory=list)

    @field_validator("evidence")
    @classmethod
    def _evidence_not_empty(cls, v: list[Evidence]) -> list[Evidence]:
        # Belt-and-suspenders next to min_length=1: a clear, intent-revealing error.
        if not v:
            raise ValueError("InferredFinding requires at least one Evidence item")
        return v


# ------------------------------------------------------------
# Reusable limitation catalogue
# ------------------------------------------------------------
# Stable codes so tools and consumers can attach/match the same caveat. New
# codes can be added freely; existing codes should not change meaning.
LIMITATIONS: dict[str, Limitation] = {
    lim.code: lim
    for lim in [
        Limitation(
            code="syntactic_calls",
            description="Method calls are extracted syntactically and may miss polymorphic or reflective calls.",
        ),
        Limitation(
            code="no_call_graph",
            description="No call-site graph is built; relations are derived from the dependency-injection graph and type references.",
        ),
        Limitation(
            code="spring_proxies",
            description="Spring runtime proxies, AOP advice and dynamic bean wiring are not resolved.",
        ),
        Limitation(
            code="interface_impl_unresolved",
            description="Interface implementations are resolved by naming convention only (no JDT/bytecode analysis).",
        ),
        Limitation(
            code="ambiguous_simple_name",
            description="Types are matched by simple name; ambiguous names over-approximate (link all candidates).",
        ),
        Limitation(
            code="repo_table_naming",
            description="Repository-to-table mapping is inferred from naming conventions, not verified against schema.",
        ),
        Limitation(
            code="dynamic_endpoints",
            description="Dynamic or programmatically registered endpoint mappings are not supported (annotation-only).",
        ),
        Limitation(
            code="ctor_injection_without_lombok",
            description="Constructor injection is detected via Lombok @RequiredArgsConstructor/@AllArgsConstructor or explicit field-injection annotations; a hand-written constructor over a final field may be missed.",
        ),
        Limitation(
            code="tests_not_indexed",
            description="Test sources are not indexed; test references are heuristic name guesses.",
        ),
        Limitation(
            code="external_types_unresolved",
            description="Types outside the scanned project (framework/library classes) are not resolved to a definition.",
        ),
        Limitation(
            code="config_not_resolved",
            description="Config is read statically: ${...} placeholders are not resolved, and profile activation / import / override precedence is not computed.",
        ),
        Limitation(
            code="generated_code_build_dependent",
            description="Generated Java sources under build/generated (Gradle) and target/generated-sources (Maven) are indexed, but only if the project was built — their presence and freshness depend on the last build.",
        ),
    ]
}


def limitation(code: str) -> Limitation:
    """Look up a reusable limitation by code (raises KeyError on an unknown code)."""
    return LIMITATIONS[code]
