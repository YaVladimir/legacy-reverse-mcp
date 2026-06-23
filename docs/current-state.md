# legacy-reverse-mcp — current state (Stage 0 inventory)

Snapshot taken before the "evidence / confidence / limitations" hardening work
(Stages 1–3). Purpose: record what exists today so later stages can extend it
without breaking the source-first MVP.

## What the tool is

A **source-first** reverse-engineering helper for legacy Java/Spring backends.
It parses Java source with tree-sitter, stores a static model in SQLite, and
exposes read tools over MCP (and a CLI) so a developer or an LLM agent can
understand an unfamiliar repo quickly: modules, classes, endpoints, Spring/JAX-RS
layers, approximate relations, code areas, and task-scoped context packs.

It is explicitly **not** an exhaustive RE suite and does **not** promise 100%
accuracy. Several outputs are heuristic.

## Layout

```
cli.py                      Click CLI: `legacy-reverse scan --repo <path> [--force] [--resolve]`
mcp_server.py               FastMCP server; 9 @mcp.tool() entrypoints
scanner/
  repo_scanner.py           module/build-tool detection (Maven/Gradle), .java file walk
  java_parser.py            tree-sitter-java → ParsedFile/ParsedClass/ParsedMethod/ParsedField/ParsedAnnotation
  java_indexer.py           persists parsed classes; derives class→class dependency edges
  spring_scanner.py         pure role classification (@RestController→controller, @Service→service, …) + DI detection
  endpoint_scanner.py       pure endpoint extraction (JAX-RS @GET/@Path + Spring @*Mapping/@RequestMapping)
  dependency_scanner.py     module→module + external (Maven/Gradle) dependency graph; optional gradle version resolve
  pipeline.py               single scan pipeline shared by CLI + MCP (so they never drift)
index/
  schema.sql                full SQLite schema (DDL + views) executed via executescript on init
  repository.py             schema bootstrap + CRUD helpers (insert_*/list_*/clear_*)
  queries.py                read models behind the MCP tools (SQL lives here, server stays thin)
  search.py                 FTS5 build + query (find_code_areas)
  findings.py               cheap structural smells written to the `finding` table
summarizer/
  class_summary.py          deterministic, template-based class summaries (LLM-pluggable seam: summarize_class)
  package_summary.py        deterministic package summaries
  context_pack.py           assembles a task-scoped markdown context pack (retrieval + trim to token budget)
```

No `legacy_reverse/` package — the project uses flat top-level packages
(`scanner`, `index`, `summarizer`) plus `cli.py` / `mcp_server.py` modules.

## Scan pipeline (`scanner/pipeline.build_index`)

Ordered stages, all deterministic, no LLM:

1. module detection (`scan_repo`)
2. Java index (`index_repo` → classes/methods/fields/annotations/endpoints)
3. class→class dependency edges (`index_class_dependencies`)
4. build/external dependency graph (`index_dependencies`)
5. deterministic class + package summaries
6. FTS5 search index
7. structural findings (`detect_findings` → `finding` table)
8. scan manifest row

## MCP tools (all 9 operational)

| Tool | Returns | Shape | Heuristic? |
|------|---------|-------|-----------|
| `scan_repository(repo_path, force=False)` | scan stats dict | structured | no |
| `list_endpoints(http_method?, path_contains?, limit=200)` | `{count, endpoints[]}` | structured | no (direct from annotations) |
| `explain_class(fqn)` | role/annotations/injected deps/methods/endpoints | structured | partly (role) |
| `trace_endpoint(endpoint_id)` | controller→service→repository/persistence `steps[]` + `confidence` | structured | **yes** |
| `get_project_overview()` | stack/totals/roles/top modules/findings | structured | partly |
| `find_code_areas(query, limit=20)` | classes/endpoints/methods grouped | structured | ranking only |
| `get_module_map()` | modules + inter-module deps + endpoint counts | structured | no |
| `get_change_impact(symbol)` | dependents/affected endpoints/test candidates | structured | **yes** (test_candidates, dependents) |
| `generate_context_pack(task, max_tokens=4000)` | `{… , markdown, matched}` | **markdown blob** + small structured envelope | yes |

### Structured vs string/markdown today

- **Structured (dict/JSON):** every tool above returns a Python dict. The data is
  agent-friendly already.
- **Confidence:** only `trace_endpoint` (per-step + overall) and the
  `endpoint_trace` table carry a `confidence` value today. No other tool reports
  confidence, evidence, or limitations.
- **Markdown payloads:** `generate_context_pack` embeds a rendered markdown
  document under `markdown`; `class.summary` / package summaries are prose
  strings. These are presentation, not evidence.
- **Ad-hoc caveats:** `get_change_impact` ships a freeform `note` string;
  `trace_endpoint` encodes confidence but **no evidence** (which file/line/field
  drove each step).

So today the tools are structured but **opinionated without showing their work**:
a consumer cannot see *why* a role was assigned, *which* annotation/line proves an
endpoint, or *what* the method does not know. That gap is what Stages 1–3 close.

## Data model (SQLite, `index/schema.sql`)

Entity tables: `scan_manifest`, `module`, `package`, `class`, `class_annotation`,
`class_interface`, `method`, `method_annotation`, `method_parameter`, `field`,
`endpoint`, `endpoint_trace`, `class_dependency`, `module_dependency`,
`external_dependency`, `config_file`, `config_property`, `summary`, `finding`,
FTS5 `search_index`, plus views `v_endpoint_full`, `v_class_full`,
`v_module_dependencies`.

Notes:
- `class.role` and `class.summary`, `endpoint_trace.confidence` already exist.
- `confidence` vocabulary in the schema header is `high|medium|low` (no `unknown`).
- There is a `finding` table (structural smells) — distinct from the
  `inferred_findings` table introduced in Stage 2 (kept separate; not removed).

## Known heuristics / limitations (observed during inventory)

- **Method calls are not extracted.** `trace_endpoint` follows *injected fields*
  (DI graph), not actual call sites. Polymorphic/真 call edges are unknown.
- **Constructor injection without Lombok is missed.** `field_is_injected` marks a
  field injected only if it has `@Autowired/@Inject/@Resource`, or the class has
  `@RequiredArgsConstructor/@AllArgsConstructor` over a `final` field. A plain
  hand-written constructor that assigns a `final` field is **not** detected, so
  `trace_endpoint` can stop at the controller. (Reproduced on the Stage-0 fixture:
  a `DepositController` with an explicit constructor traced only to step 0.)
- **Interface→impl resolution is naming-based** (`*Impl` preference); no JDT/bytecode.
- **Ambiguous simple-names over-approximate** in `class_dependency` (links all
  candidates) — change-impact never misses but may over-report.
- **Test sources are skipped**; `get_change_impact.test_candidates` are guessed
  names (`<Class>Test`, `<Class>IT`).
- **Dynamic/programmatic endpoint registration is not supported** (annotation-only).
- **Gradle version resolve (`scan --resolve`) does not run on this machine**
  (loopback failure) — external deps stay version-NULL; static index unaffected.

## Tests

- **No test suite existed at Stage 0** (no `tests/`, no `test_*.py`, pytest not a
  dependency). Reproducibility was previously checked via ad-hoc direct-call
  drivers (see project memory).
- Verified at Stage 0 by scanning a 5-class Spring fixture: `build_index`
  returned `classes=5, methods=8, endpoints=2`; `list_endpoints` and
  `trace_endpoint` returned the expected structured data.
- Stages 1–3 add a real `pytest` suite under `tests/` (pytest installed into the
  project `.venv`; added to `pyproject` optional `dev` extra).

## Behavior frozen as the baseline contract

Stages 1–3 must keep these working unchanged:
- `build_index` stats keys, all 9 MCP tool response shapes.
- `scan_repository` returning `{status: exists|scanned, db_path, …}`.
- the full existing schema (tables only added, none dropped/renamed).
