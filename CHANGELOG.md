# Changelog

## Unreleased — flat architecture JSON (import/export) + gigacode harness

Goal: interoperate with the GigaCode `architecture-generator` and let it be the
description source. Round-trip the same `project_architecture_flat.json` schema, and
run the skill via gigacode-cli.

### Added
- **Flat architecture export/import** (`analysis/flat_arch.py`):
  - `export_flat` renders the index as the reference schema (`{project, generated_at,
    total_classes, classes:[{id, pkg, name, description, type, kind, class_modifiers,
    extends, implements, fields, methods:[{sig, modifiers, description}]}]}`) — a drop-in
    for the GigaCode generator (reuses `class_detail`).
  - `import_flat` loads descriptions back in: classes match by `pkg.name` (fallback simple
    name), methods by name (+ parameter type simple-names for overloads). Writes
    `class.summary`/`method.summary` + rebuilds FTS.
- **Imported descriptions win** (`summarizer/describe.py`): new `imported_description`
  table in `.reverse/descriptions.sqlite3`; `describe` consults it first (imported > LLM >
  fallback) and it survives re-scans.
- **gigacode harness** (`summarizer/harness.py`): runs the `architecture-generator` skill
  via gigacode-cli (Gemini-CLI fork, headless `-p`; argv list, never `shell=True`; Windows
  `.cmd` wrapping; env inherited) → flat JSON (stdout or file) → `import_flat`. Fully
  configurable via `LEGACY_REVERSE_GIGACODE_*`; honest structured error + manual fallback
  when gigacode/skill is absent.
- **CLI**: `export-arch`, `import-arch`, `generate-arch`.
- **MCP tools**: `export_architecture`, `import_architecture`, `generate_architecture`
  (now 18 tools).
- **Tests** (`tests/test_flat_arch.py`): export parity, export→import round-trip,
  class/method matching, imported>fallback priority, harness (mocked subprocess), MCP
  wiring. Verify: 88 pytest green, golden 11/11, stdio `tools/list` = 18.

## Unreleased — Phase 2 meaning layer (descriptions + feature search)

Goal: close the gap with the reference architecture JSON — emit not just structure
but **meaning**. Every class/method gets a concise natural-language description of
what it does and why, and an agent can go from a topic/feature straight to the
relevant classes **with their methods and parameters**, no grep.

### Added
- **Pluggable LLM client** (`summarizer/llm.py`): zero-dependency, OpenAI-compatible
  (`/v1/chat/completions`) over stdlib `urllib`. Configured via `LEGACY_REVERSE_LLM_*`
  env vars; disabled (→ deterministic fallback) when no `BASE_URL` is set; never
  raises into the pipeline.
- **Describe pipeline** (`summarizer/describe.py`): offline, decoupled from `scan`.
  Builds a compact per-class skeleton + source snippet, asks the LLM for class +
  method descriptions (one JSON call per class), falls back to deterministic text,
  and aggregates package/module/project summaries. Denormalised into
  `class.summary`/`method.summary` (so cards + FTS use them) and the `summary` table.
- **Durable description cache** (`<repo>/.reverse/descriptions.sqlite3`): keyed by a
  stable content hash, **survives `scan --force`** so re-runs don't re-spend the LLM
  budget; `--force` ignores it.
- **Full structural surfacing** (`index/queries.py`): `class_detail` now returns
  `extends`, `implements`, per-method `parameters` + pretty `sig` (with param names),
  `class_modifiers`, `type` (alias of `role`) and `description`.
- **New MCP tools** (+ CLI): `generate_descriptions`, `find_feature` (topic → ranked
  class cards with bundled methods/params/descriptions), `get_class_card`
  (reference-parity object). `legacy-reverse describe --repo … [--force] [--no-llm]`.
  Now 15 MCP tools.
- **`get_class_summary` / `explain_class`** now surface the generated description.
- **FTS** indexes `method.summary`, so feature/business/Russian queries match meaning.
- **Tests** (`tests/test_describe.py`) + golden questions `find-feature-deposit`,
  `class-card-controller`. Verify: 80 pytest green, golden 11/11.

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
