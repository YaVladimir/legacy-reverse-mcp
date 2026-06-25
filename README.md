# legacy-reverse-mcp

A **source-first** MCP server that helps a developer or an LLM agent understand a
legacy **Java / Spring** backend fast: REST endpoints, Spring/JAX-RS layers, class
roles, dependency-injection wiring, heuristic request traces, change impact and
task-scoped context packs.

It parses sources with `tree-sitter-java` into a SQLite index (no compilation
required) and answers questions over that index. It is **not** an exhaustive
reverse-engineering suite and does **not** promise 100% accuracy. Instead, every
heuristic answer **shows its work**: it separates *observed facts* from *inferred
findings*, attaches *evidence* (file + line) to each, reports a *confidence*
level, and lists the *limitations* that bound the answer.

- **Stack:** Python 3.11+, [FastMCP](https://github.com/jlowin/fastmcp), SQLite, tree-sitter-java
- **Frameworks:** Spring MVC (`@RestController`, `@GetMapping`, …) **and** JAX-RS
  (`jakarta.ws.rs` `@Path`, `@GET`, …); Spring + Lombok constructor injection
  (`@RequiredArgsConstructor` over `final` fields)

## What it can and cannot do

**Can:** find endpoints; classify Spring/JAX-RS layers from stereotypes, naming and
package; follow controller → service → repository using syntactic calls and the DI
graph; estimate candidate change impact; assemble an explained context pack;
produce a baseline project report.

**Cannot** (by design — see [docs/limitations.md](docs/limitations.md)): bytecode
analysis, runtime Spring resolution (proxies/profiles/conditional beans), a full
polymorphic call graph, or data-flow analysis. False positives are possible; that
is exactly why results carry confidence + evidence.

## Install

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -e .          # Windows (use `py`/venv, not bare `python`)
# source .venv/bin/activate && pip install -e .    # Unix
# dev (tests): pip install -e ".[dev]"
```

## Scan a repository

```bash
legacy-reverse scan --repo /path/to/java-project [--force] [--resolve] [--report]
```

Walks the repo, detects Maven/Gradle modules, parses every non-test `.java` file,
records **observed facts with evidence** and intra-class method calls, builds the
dependency graph, and writes an index to `<repo>/.reverse/index.sqlite3`.
`--report` also writes a [baseline report](docs/) (see below). `--resolve` runs
gradle to fill external dependency versions (slower, needs a working build).

## Baseline report

```bash
legacy-reverse scan --repo /path/to/java-project --report
legacy-reverse report --repo /path/to/java-project   # from an existing index
```

Writes `baseline.md` + `baseline.json` to `<repo>/.reverse/reports/`: inventory
counts, top modules/packages, public API surface, candidate domain areas,
low-confidence findings and the tool's limitations.

## Run as an MCP server

```bash
LEGACY_REVERSE_REPO=/path/to/java-project python mcp_server.py
```

The server resolves its index from the repo passed to `scan_repository`, or from
the `LEGACY_REVERSE_REPO` environment variable.

## MCP tools

Every heuristic tool returns a structured response carrying `confidence`,
`limitations` and `warnings`; errors are structured (`error`, `kind`,
`suggestions`). Full schemas + examples: [docs/mcp-api.md](docs/mcp-api.md).

| Tool | Purpose |
|------|---------|
| `scan_repository(repo_path, force)` | Scan + (re)build the index |
| `list_endpoints(http_method, path_contains, limit)` | REST endpoints (JAX-RS + Spring) |
| `explain_class(fqn)` | Observed facts + inferred findings + related symbols, all with evidence |
| `trace_endpoint(endpoint_id \| http_method, path_contains)` | Controller → service → repository trace with per-step + overall confidence |
| `get_change_impact(symbol)` | `direct_impacts` vs `candidate_impacts`, each with reason/evidence/confidence |
| `generate_context_pack(task, max_tokens, max_items)` | Explained pack: `selected_items` (with reasons) + `excluded_items` |
| `get_module_map()` | Modules, inter-module deps, external coordinates, endpoint counts |
| `get_project_overview()` | Stack, totals, role distribution, top modules, findings |
| `find_code_areas(query, limit)` | FTS keyword search over classes/methods/endpoints |
| `get_findings(subject, finding_type, limit)` | Inferred findings persisted during scan, each with evidence + confidence |
| `get_config(key_contains, profile, limit)` | Spring config (`application*`/`bootstrap*`) — files + properties; secret values masked |
| `get_class_summary(fqn)` | Deterministic one-line class summary (LLM-swappable `summarize_class` seam) |

## Interpreting confidence

- **high** — a direct fact or an inference over direct (unambiguous) links: a
  stereotype annotation, an endpoint read from a mapping, a call found
  syntactically in a method body.
- **medium** — a heuristic inference from several signals: layer from name **and**
  package, a service/repository found via injection + naming.
- **low** — a guess from naming/package/keyword similarity only.
- **unknown** — no usable signal.

Details + examples: [docs/confidence-model.md](docs/confidence-model.md). The
observed-fact vs inferred-finding model: [docs/evidence-model.md](docs/evidence-model.md).

## Golden questions (evaluation)

```bash
py eval/run_golden_questions.py          # markdown report; exit 0 only if all pass
py eval/run_golden_questions.py --json
```

A deterministic regression layer that scans a committed Java/Spring fixture and
checks **structural quality gates** (evidence/confidence/limitations present,
endpoints found, context pack non-empty). See [docs/golden-questions.md](docs/golden-questions.md).

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

## Layout

```
cli.py                  CLI: scan (+ --report), report
mcp_server.py           FastMCP server + tool registrations
models/evidence.py      Evidence / Confidence / Limitation / ObservedFact / InferredFinding
scanner/                repo + java parser, spring/endpoint scanners, fact emitter, indexer, pipeline
index/                  schema.sql, repository (CRUD + facts), queries, search, findings
analysis/               evidence-based tools: explain, trace, impact, context_pack, layers, report
summarizer/             deterministic class/package summaries
eval/                   golden_questions.yaml, run_golden_questions.py, fixture/
tests/                  pytest suite
docs/                   mcp-api, confidence-model, evidence-model, limitations, golden-questions
```

## Status

Verified against [Apache Fineract](https://github.com/apache/fineract) (47 Gradle
modules, ~5.3k non-test classes): **974 endpoints** (971 JAX-RS + 3 Spring), roles
classified, constructor-injection traces reaching persistence, a 147-edge module
graph, 16.5k class-dependency edges, **~48k observed facts with evidence**,
intra-class call edges, FTS index, baseline report and a green golden-questions
run. See [CHANGELOG.md](CHANGELOG.md) for the evidence-layer work.
