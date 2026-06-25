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

#### Accuracy & coverage passes (phases A–E)
- **Type resolution via imports** (A): type references are resolved through a
  file's imports to FQNs, so `class_dependency` edges are matched on FQN and the
  count of false `ambiguous_simple_name` edges drops.
- **Hand-written constructor injection** (B): a constructor that assigns its
  parameters to `final` fields now marks those fields injected, even without
  Lombok.
- **CI + persisted inferences** (C): GitHub Actions workflow; low-confidence
  layer findings are persisted to `inferred_findings` during scan.
- **Config indexing** (D): `scanner/config_scanner.py` indexes
  `application*.{yml,yaml,properties}` and `bootstrap*.*` into `config_file` /
  `config_property` — nested YAML flattened to dotted keys, per-file profile from
  the filename, secret-bearing keys flagged. Scan summary gains config counts; the
  baseline report gains a **Config / profiles** section and a config-derived
  `external_service_urls` signal (kept separate from the code-based
  `external_clients` count; infra URLs filtered, embedded creds masked). New MCP
  **`get_config`** (secret values masked on read; the index stays raw).
- **Richer explain + same-class trace hop** (E): `explain_class` adds inferred
  findings (each with evidence) for `@Transactional` boundaries, per-endpoint
  purpose (verb + handler), and reuse of structural smells
  (`god_class`/`large_controller`). The parser also records same-class self-calls,
  and `trace_endpoint` steps one level into a delegating helper
  (`controller_helper`) to reach the service. New MCP **`get_class_summary`** over
  the deterministic `summarize_class` seam.
- **Limitation codes**: added `config_not_resolved`.

### Changed
- `explain_class` / `trace_endpoint` / `get_change_impact` /
  `generate_context_pack` now return the evidence-based contracts above (their
  old flat shapes are replaced).
- `trace_endpoint` accepts `http_method` / `path_contains` in addition to
  `endpoint_id`.

### Removed (dead/legacy)
- `summarizer/context_pack.py` — superseded by `analysis/context_pack.py`; no
  importers remained.
- `index/queries.py`: legacy `trace_endpoint` and `change_impact` (+ private
  helpers `_resolve_type`, `_find_impl`, `_injected_of`, `_looks_like`) —
  superseded by `analysis/trace.py` and `analysis/impact.py`; no callers
  remained. `_PERSISTENCE_TYPES`, `_simple_type` and `class_detail` are kept
  (still used).

### Fixed
- Root module path `.` is normalised to the catch-all, so classes at the repo
  root attribute to their module (single-module repos previously got
  `module_id = NULL`).

### Notes
- Reports live under `.reverse/reports/` (alongside the existing `.reverse` index)
  rather than a second `.legacy-reverse/` directory; the path is a parameter.
- Verified on Apache Fineract (~5.3k classes): ~51k observed facts + evidence,
  scan ≈ 23 s; full pytest suite and golden questions green.
- D/E on Fineract: 11 config files / 911 properties / 2 profiles indexed (no
  secrets leak into the report); intra-class calls 15.4k → 23.2k; trace reaches a
  service/repository on 939/974 endpoints, using the same-class helper hop on 85.
