# Evidence / confidence / limitations layer (Stages 1–3)

This layer lets the tool **show its work**: separate what was *observed* from what
was *inferred*, attach *evidence* to each, carry a *confidence* level, and list the
*limitations* that bound a result. The MVP can still be wrong — but it is now
honest about *why* it says what it says.

## Contract (`models/evidence.py`)

| Type | Meaning |
|------|---------|
| `Evidence` | one concrete reason (`kind`, `description`, optional `file_path`/`line_start`/`line_end`/`symbol`, `source`) |
| `ConfidenceLevel` | `high` \| `medium` \| `low` \| `unknown` |
| `Limitation` | a bounded caveat with a stable `code` + `description` |
| `ObservedFact` | a `(subject, predicate, object)` triple read directly from source; defaults to `high` confidence |
| `InferredFinding` | a heuristic conclusion; **must** have ≥1 `Evidence`, an explicit `confidence`, and optional `limitations` |

All are pydantic models → `model_dump(mode="json")` / `model_dump_json()` for MCP.
A shared `LIMITATIONS` catalogue (keyed by `code`) provides reusable caveats such
as `syntactic_calls`, `spring_proxies`, `interface_impl_unresolved`,
`ctor_injection_without_lombok`, `dynamic_endpoints`, `tests_not_indexed`.

**Invariant:** `InferredFinding(evidence=[])` raises `ValidationError`. A finding
with no evidence is treated as a bug, not a permissive default.

## Storage (`index/schema.sql`)

Four additive tables (no existing table dropped/renamed):

- `observed_facts(fact_type, subject, predicate, object, confidence, created_at)`
- `inferred_findings(finding_type, subject, summary, confidence, created_at)`
- `evidence(owner_type, owner_id, kind, description, file_path, line_start, line_end, symbol, source)`
- `limitations(owner_type, owner_id, code, description)`

`evidence` / `limitations` are polymorphic via `(owner_type, owner_id)` where
`owner_type ∈ {observed_fact, inferred_finding}`. The pre-existing `finding`
table (structural smells) is **kept separate** and untouched.

Repository helpers (`index/repository.py`): `insert_observed_fact`,
`insert_inferred_finding`, `clear_observed_facts`, `clear_inferred_findings`,
`list_observed_facts`, `list_inferred_findings`, `count_observed_facts`. The
`clear_*` helpers only remove evidence/limitations for their own `owner_type`.

## Emission (`scanner/fact_emitter.py`)

`class_observed_facts(parsed_class, FactConfig)` turns one parsed class into
`ObservedFact`s, all at `high` confidence:

- `package_declaration`, `class_declaration`
- `class_annotation` (every class annotation — stereotypes etc.)
- `method_declaration`, `method_annotation` (`@Scheduled`, `@KafkaListener`, …)
- `mapping_annotation` — one per REST endpoint, `object = "<VERB> <full path>"`,
  evidence pointing at the actual mapping annotation (JAX-RS `@GET`/`@Path` *and*
  Spring `@*Mapping`/`@RequestMapping`), reusing the same pure functions the
  indexer uses so facts never drift from the `endpoint` table.
- `field` + `field_annotation` (injection detection mirrors the indexer, incl.
  Lombok `@RequiredArgsConstructor` over `final` fields)

Wired into the scan via `java_indexer.index_repo(..., emit_facts=True)` (default
on). The scan summary now includes `observed_facts`.

### Volume control (`FactConfig`)

Legacy repos have tens of thousands of members; by default only *signal-bearing*
methods/fields are recorded (annotated members, REST handlers, injected fields).
Flags `record_all_methods`, `record_all_fields`, `record_imports` opt into an
exhaustive fact base.

Measured on Apache Fineract (5 286 classes, 21 814 methods): **48 052 facts +
48 052 evidence rows**, scan time ≈ 20 s (≈ +3 s over the member-only index).
`mapping_annotation` facts (974) match the endpoint count exactly.

## What is intentionally *not* in this layer yet

Per the task's non-goals and staged scope, this PR ships **observed facts only**.
Still to come (separate stages): emitting `InferredFinding`s for role/layer,
endpoint purpose, trace and change-impact (the machinery — models, tables, repo
helpers — is already in place); exposing facts/findings through MCP responses;
a post-scan baseline report; and a golden-questions regression layer. No
bytecode/JDT/Neo4j/call-graph work is planned (explicit non-goals).
