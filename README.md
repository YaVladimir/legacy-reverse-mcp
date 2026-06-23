# legacy-reverse-mcp

An MCP server that **reverse-engineers legacy Java / Spring backends** so an agent
can navigate an unfamiliar codebase: REST endpoints, Spring/JAX-RS layers,
class roles, dependency-injection wiring and heuristic request traces.

Built around a static index (no compilation required): `tree-sitter-java`
parses sources into SQLite, and MCP tools answer questions over that index.

- **Language/stack:** Python 3.11+, [FastMCP](https://github.com/jlowin/fastmcp), SQLite, tree-sitter-java
- **Frameworks understood:** Spring MVC (`@RestController`, `@GetMapping`, …) **and**
  JAX-RS (`jakarta.ws.rs` `@Path`, `@GET`, …); Spring + Lombok constructor injection
  (`@RequiredArgsConstructor` over `final` fields)

## Install

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .   # Windows
# source .venv/bin/activate && pip install -e .   # Unix
```

## Scan a repository

```bash
legacy-reverse scan --repo /path/to/java-project [--force]
```

This walks the repo, detects Maven/Gradle modules, parses every non-test
`.java` file and writes an index to `<repo>/.reverse/index.sqlite3`.

## Run as an MCP server

```bash
LEGACY_REVERSE_REPO=/path/to/java-project python mcp_server.py
```

The server resolves its index from the repo passed to `scan_repository`, or
from the `LEGACY_REVERSE_REPO` environment variable.

## MCP tools

| Tool | Status | Purpose |
|------|--------|---------|
| `scan_repository(repo_path, force)` | ✅ | Scan + (re)build the index |
| `list_endpoints(http_method, path_contains, limit)` | ✅ | REST endpoints (JAX-RS + Spring) |
| `explain_class(fqn)` | ✅ | Role, annotations, injected deps, methods, endpoints |
| `trace_endpoint(endpoint_id)` | ✅ | Heuristic controller → service → repository/persistence chain |
| `get_project_overview()` | 🚧 | Stack, modules, counts, suspicious deps |
| `find_code_areas(query)` | 🚧 | Keyword search over classes/methods/summaries |
| `get_module_map()` | 🚧 | Modules, inter-module deps, public endpoints |
| `get_change_impact(symbol)` | 🚧 | Direct dependents, affected endpoints, test candidates |
| `generate_context_pack(task, max_tokens)` | 🚧 | Compact task-scoped context for an agent |

## Layout

```
cli.py                  legacy-reverse CLI (scan)
mcp_server.py           FastMCP server + tool registrations
scanner/
  repo_scanner.py       module detection (Maven/Gradle), file walking
  java_parser.py        tree-sitter-java AST -> classes/methods/fields/annotations
  spring_scanner.py     role classification + injection detection (pure)
  endpoint_scanner.py   JAX-RS + Spring endpoint extraction (pure)
  java_indexer.py       orchestration: parse -> persist
index/
  schema.sql            SQLite schema
  repository.py         CRUD
  queries.py            read models for MCP tools
summarizer/             LLM summaries + context packs (planned)
```

## Status

Days 1–4 of a 5-phase plan are implemented and verified against
[Apache Fineract](https://github.com/apache/fineract) (47 Gradle modules,
~5.3k non-test classes): **974 endpoints** extracted (971 JAX-RS + 3 Spring),
roles classified (170 controllers / 810 services / 280 entities), and
constructor-injection traces reaching the persistence layer. Day 5
(dependency graph / `get_module_map`) is next.
