# Changelog

## Unreleased — provability & evidence layer

Goal: keep the source-first MVP, but make every heuristic answer **honest** —
separate observed facts from inferred findings, attach evidence (file + line),
report confidence, and list limitations. No bytecode/JDT/Neo4j; all heuristics
deterministic; no LLM calls in tests.

### Added
- **Provability models** (`models/evidence.py`): `Evidence`, `ConfidenceLevel`
  (`high|medium|low|unknown`), `Limitation`, `ObservedFact`, `InferredFinding`
  (rejects empty evidence) + a reusable `LIMITATIONS` catalogue.
- **Schema**: `observed_facts`, `inferred_findings`, `evidence`, `limitations`,
  and `method_call` (intra-class syntactic calls). Existing tables untouched.
- **Observed-fact extraction** during scan (`scanner/fact_emitter.py`): package /
  class / method / field declarations, annotations, and one `mapping_annotation`
  fact per endpoint — each with evidence. `FactConfig` gates high-volume facts.
- **Syntactic call extraction**: the parser now records method invocations on
  class fields, enabling real controller→service→repository traces.
- **`analysis/` package** — evidence-based tools:
  - `explain_class`: observed facts + inferred findings (layer, with evidence +
    confidence) + related symbols (injected deps, calls, endpoints).
  - `trace_endpoint`: per-step + overall confidence, evidence, limitations; lookup
    by id or method/path; structured not-found with suggestions.
  - `get_change_impact`: split `direct_impacts` vs `candidate_impacts`, each with
    reason/evidence/confidence; `suggested_files_for_context`.
  - `generate_context_pack`: `selected_items` (with reason/evidence/confidence) +
    `excluded_items` + `context_markdown`.
  - `report`: baseline report (markdown + json).
- **Baseline report**: `legacy-reverse scan --report` and `legacy-reverse report
  --repo …` → `<repo>/.reverse/reports/baseline.{md,json}`.
- **Golden questions** (`eval/`): 9 structural-quality checks over a committed
  Java/Spring fixture; `py eval/run_golden_questions.py` (exit 0/1).
- **MCP envelope**: every heuristic tool returns `confidence` + `limitations` +
  `warnings`; structured errors.
- **Docs**: `docs/mcp-api.md`, `confidence-model.md`, `evidence-model.md`,
  `limitations.md`, `golden-questions.md`, `evidence-layer.md`,
  `current-state.md`; README rewritten.
- **Tests**: first real pytest suite (`tests/`), pytest added as a `dev` extra.

### Changed
- `explain_class` / `trace_endpoint` / `get_change_impact` /
  `generate_context_pack` now return the evidence-based contracts above (their
  old flat shapes are replaced).
- `trace_endpoint` accepts `http_method` / `path_contains` in addition to
  `endpoint_id`.

### Fixed
- Root module path `.` is normalised to the catch-all, so classes at the repo
  root attribute to their module (single-module repos previously got
  `module_id = NULL`).

### Notes
- Reports live under `.reverse/reports/` (alongside the existing `.reverse` index)
  rather than a second `.legacy-reverse/` directory; the path is a parameter.
- Verified on Apache Fineract (~5.3k classes): ~48k observed facts + evidence,
  scan ≈ 20 s; full pytest suite and golden questions green.
