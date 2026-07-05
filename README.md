# legacy-reverse-mcp

**English** · [Русский](README.ru.md)

`legacy-reverse-mcp` is a **source-first** MCP server for understanding legacy
**Java / Spring** backends quickly. It helps a developer or an LLM agent find
REST endpoints, Spring/JAX-RS layers, dependency-injection wiring, heuristic
request traces, change impact, and task-scoped context packs.

It parses Java sources with `tree-sitter-java` into a SQLite index. No Java
compilation is required.

Two paths matter when you use it:

- **This repository**: the cloned `legacy-reverse-mcp` project that contains the
  Python CLI and MCP server.
- **Target repository**: the Java/Spring project you want to analyze. Point
  `LEGACY_REVERSE_REPO` and the scan command at this repo.

- **Stack:** Python 3.11+, [FastMCP](https://github.com/jlowin/fastmcp), SQLite, tree-sitter-java
- **Frameworks:** Spring MVC (`@RestController`, `@GetMapping`, ...) and JAX-RS
  (`jakarta.ws.rs` `@Path`, `@GET`, ...); Spring + Lombok constructor injection
  (`@RequiredArgsConstructor` over `final` fields)

## What it can and cannot do

**Can:** find endpoints; classify Spring/JAX-RS layers from stereotypes, naming
and package; follow controller -> service -> repository using syntactic calls
and the DI graph; estimate candidate change impact; assemble an explained
context pack; produce a baseline project report.

**Cannot** (by design; see [docs/limitations.md](docs/limitations.md)): bytecode
analysis, runtime Spring resolution (proxies/profiles/conditional beans), a full
polymorphic call graph, or data-flow analysis. False positives are possible; that
is exactly why results carry confidence + evidence.

## Quick start

### 1. Install from source

Windows:

```powershell
git clone <repo-url>
cd legacy-reverse-mcp

py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e .
```

macOS/Linux:

```bash
git clone <repo-url>
cd legacy-reverse-mcp

python3.11 -m venv .venv
./.venv/bin/python -m pip install -e .
```

For development and tests, install the optional test dependencies:

Windows:

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
```

macOS/Linux:

```bash
./.venv/bin/python -m pip install -e ".[dev]"
```

### 2. Scan your Java/Spring repository

Run this against the **target Java/Spring repo**, not this MCP repo:

macOS/Linux:

```bash
./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
```

Windows PowerShell:

```powershell
.venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
```

If your virtual environment is activated, `legacy-reverse scan ...` works too.

The scan writes the index and baseline reports into the target repo:

```text
<repo>/.reverse/index.sqlite3
<repo>/.reverse/reports/baseline.md
<repo>/.reverse/reports/baseline.json
```

Use `--force` to rebuild an existing index. Use `--resolve` only when you want
Gradle dependency versions resolved and the target project has a working build.

### 2b. Generate descriptions (optional but recommended)

`scan` builds the *structural* index fast. The `describe` step then adds the
*meaning*: a concise natural-language description of what each class and method
does and why, plus package/module/project summaries. These descriptions power
`get_class_card`, enrich `explain_class`, and (because they are indexed for
search) make `find_feature` answer business/Russian topic queries.

```bash
./.venv/bin/legacy-reverse describe --repo /path/to/java-project
```

```powershell
.venv\Scripts\legacy-reverse.exe describe --repo C:\path\to\java-project
```

`describe` uses a **pluggable, OpenAI-compatible LLM** configured via environment
variables. With no endpoint configured (or `--no-llm`) it writes solid
deterministic descriptions instead, so it always works:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LEGACY_REVERSE_LLM_BASE_URL` | *(empty → LLM disabled)* | e.g. `http://localhost:11434/v1` (Ollama), vLLM, llama.cpp, LM Studio |
| `LEGACY_REVERSE_LLM_MODEL` | `qwen3-coder-next` | model name |
| `LEGACY_REVERSE_LLM_API_KEY` | *(none)* | optional bearer token |
| `LEGACY_REVERSE_LLM_LANG` | `ru` | language of generated descriptions |
| `LEGACY_REVERSE_LLM_TIMEOUT` / `_MAX_TOKENS` / `_TEMPERATURE` | `60` / `512` / `0.1` | request tuning |

Descriptions are cached by content hash in `<repo>/.reverse/descriptions.sqlite3`,
a file separate from the main index, which survives `scan --force`. Pass `--force`
to `describe` (not `scan`) to ignore the cache.

**`scan --force` deletes and rebuilds `index.sqlite3` from scratch** — this wipes
any `describe` output already applied there (`class.summary`/`method.summary` and
the module/project rows of the `summary` table; `scan` only ever regenerates the
plain deterministic package-level summary on its own). The description *cache*
survives, so **re-running `describe` after every `scan --force`** re-applies the
same descriptions from cache almost instantly (no LLM re-spend for unchanged
classes) — but skipping that re-run silently leaves you with only the bare
deterministic fallback text and no module/project summaries at all. You can also
trigger `describe` over MCP with `generate_descriptions`.

### 2c. Flat architecture JSON — export / import / gigacode

The index can be rendered to (and loaded from) a **flat architecture JSON** that is a
drop-in for the GigaCode `architecture-generator` output (`project_architecture_flat.json`):
per class `{id, pkg, name, description, type, kind, class_modifiers, extends, implements,
fields, methods:[{sig, modifiers, description}]}`.

```bash
# produce the flat JSON from our index
legacy-reverse export-arch --repo /path/to/java-project --out arch.json

# load descriptions from a flat JSON (e.g. produced by GigaCode) back into the index
legacy-reverse import-arch --repo /path/to/java-project --in arch.json
```

Imported descriptions win over LLM/fallback and survive re-scans **while the class is
unchanged**: `import-arch` stores a structure hash per class, and once the class's
signatures/annotations/source change, a later `describe` treats that import as stale and
falls back to LLM/deterministic text instead of serving an outdated description. So the
recommended "meaning" source is your GigaCode skill: it produces the JSON, we import it,
and re-import (or `describe`) after significant code changes.

**gigacode harness.** `generate-arch` runs the GigaCode skill for you and imports the
result in one step:

```bash
legacy-reverse generate-arch --repo /path/to/java-project
```

GigaCode CLI is a Gemini-CLI fork → headless `gigacode -p "<prompt>"`. The invocation is
fully env-configurable, because the exact skill trigger / output path is only known on
your work machine:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LEGACY_REVERSE_GIGACODE_CMD` | `gigacode` | CLI binary (resolved on PATH) |
| `LEGACY_REVERSE_GIGACODE_ARGS` | `-p` | flags before the prompt (space-separated) |
| `LEGACY_REVERSE_GIGACODE_PROMPT` | request to run `architecture-generator` and print JSON | the prompt / skill trigger |
| `LEGACY_REVERSE_GIGACODE_OUTPUT` | `stdout` | `stdout`, or a path to the JSON file the skill writes |
| `LEGACY_REVERSE_GIGACODE_TIMEOUT` | `900` | seconds |
| `LEGACY_REVERSE_GIGACODE_CWD` | the repo | working dir for the skill |

If gigacode isn't installed/authenticated, `generate-arch` reports a clear error — run the
skill manually and use `import-arch --in <file>`. Over MCP: `export_architecture`,
`import_architecture`, `generate_architecture`.

**Batch generation for large repos.** For hundreds of classes, one GigaCode session won't
fit; `summarizer.batch_generate` chunks the exported `arch.json` and runs several sessions
in parallel, then validates, merges and imports the results:

```bash
legacy-reverse scan --repo /path/to/java-project --report
legacy-reverse export-arch --repo /path/to/java-project --out arch.json
python -m summarizer.batch_generate arch.json --repo /path/to/java-project --parallel 5
```

What it does beyond naive chunking:

- each GigaCode session runs with the target repo as cwd, and the prompt instructs the
  model to **open every class's source file** (the flat `id` is its repo-relative path)
  and to describe side effects/invariants from real code — never to invent;
- each chunk response is **validated against what was sent**: classes the model renamed,
  invented or dropped are rejected instead of being leniently mis-matched on import;
- results are imported with structure hashes (the staleness contract above), and a final
  `describe` pass rebuilds the package/module/project summaries from the imported class
  descriptions (skippable via `--skip-describe`);
- failed chunks are kept on disk and retried selectively via `--resume <work-dir>`.

Avoid `--no-import` unless you intend to review `arch-merged.json` by hand first: the MCP
tools read **only** the SQLite index, so un-imported descriptions are invisible to the
agent until you run `import-arch`.

### 3. Run the MCP server manually

Before wiring the server into an MCP client, run it once by hand. Use absolute
paths because MCP clients often start servers from a different working
directory.

macOS/Linux:

```bash
LEGACY_REVERSE_REPO=/path/to/java-project /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Windows PowerShell:

```powershell
$env:LEGACY_REVERSE_REPO="C:\path\to\java-project"
C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

If the server starts without crashing, your MCP client can run the same command
as a stdio MCP server.

## Use with MCP clients

All examples below use placeholders. Replace:

- `/path/to/legacy-reverse-mcp` or `C:\path\to\legacy-reverse-mcp` with the
  absolute path to this Python project.
- `/path/to/java-project` or `C:\path\to\java-project` with the absolute path to
  the target Java/Spring repository.

### Claude Code

macOS/Linux:

```bash
claude mcp add legacy-reverse \
  --env LEGACY_REVERSE_REPO=/path/to/java-project \
  -- /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Windows PowerShell:

```powershell
claude mcp add legacy-reverse `
  --env LEGACY_REVERSE_REPO=C:\path\to\java-project `
  -- C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

Verify that Claude Code sees the server:

```bash
claude mcp list
```

Restart Claude Code after changing MCP configuration.

### Codex CLI

Add a stdio MCP server entry to your Codex CLI MCP config location according to
your local Codex setup.

macOS/Linux:

```toml
[mcp_servers.legacy_reverse]
command = "/path/to/legacy-reverse-mcp/.venv/bin/python"
args = ["-m", "mcp_server"]
env = { LEGACY_REVERSE_REPO = "/path/to/java-project" }
```

Windows:

```toml
[mcp_servers.legacy_reverse]
command = "C:\\path\\to\\legacy-reverse-mcp\\.venv\\Scripts\\python.exe"
args = ["-m", "mcp_server"]
env = { LEGACY_REVERSE_REPO = "C:\\path\\to\\java-project" }
```

Restart Codex CLI after changing MCP configuration.

### Qwen Code CLI

Qwen Code reads MCP servers from `mcpServers` in `.qwen/settings.json` for
project scope, or `~/.qwen/settings.json` for user scope. You can edit the
settings file directly or use `qwen mcp add`.

Project-local macOS/Linux `.qwen/settings.json`:

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "/path/to/legacy-reverse-mcp/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "LEGACY_REVERSE_REPO": "/path/to/java-project"
      },
      "timeout": 30000
    }
  }
}
```

Project-local Windows `.qwen/settings.json`:

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "C:\\path\\to\\legacy-reverse-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "env": {
        "LEGACY_REVERSE_REPO": "C:\\path\\to\\java-project"
      },
      "timeout": 30000
    }
  }
}
```

Command alternative for macOS/Linux:

```bash
qwen mcp add legacy-reverse \
  -e LEGACY_REVERSE_REPO=/path/to/java-project \
  --timeout 30000 \
  /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Command alternative for Windows PowerShell:

```powershell
qwen mcp add legacy-reverse `
  -e LEGACY_REVERSE_REPO=C:\path\to\java-project `
  --timeout 30000 `
  C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

Verify the configured servers:

```bash
qwen mcp
```

Restart Qwen Code in the same project after changing MCP configuration.

## First prompts

Once your MCP client shows the `legacy-reverse` tools, try prompts like these:

```text
Use legacy-reverse to get a project overview.
```

```text
Use legacy-reverse to list REST endpoints and group them by module.
```

```text
Use legacy-reverse to explain class com.example.SomeService with evidence and confidence.
```

```text
Use legacy-reverse to trace the endpoint GET /api/example from controller to persistence.
```

```text
Use legacy-reverse to generate a context pack for the task: "change validation rules for deposit opening".
```

## Troubleshooting

### `legacy-reverse: command not found`

- Ensure the editable install completed successfully.
- Activate the virtual environment, or call the venv Python explicitly.
- On Windows, prefer `py` or `.venv\Scripts\python.exe`, not bare `python`.

### MCP client starts, but no tools appear

- Use absolute paths in MCP config.
- Check that `LEGACY_REVERSE_REPO` points to the target Java/Spring project, not
  to this `legacy-reverse-mcp` repo.
- Restart the MCP client after changing config.
- Run the server manually first to see startup errors.

### `Index not found`

- Run the scan first:

  ```bash
  ./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
  ```

  ```powershell
  .venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
  ```

- Or ask the MCP tool `scan_repository` to scan the repo.
- Ensure the target Java project has `<repo>/.reverse/index.sqlite3`.

### Windows path escaping

- In JSON and TOML strings, use double backslashes: `C:\\path\\to\\repo`.
- In PowerShell commands, normal backslashes are OK: `C:\path\to\repo`.

## CLI reference

If your virtual environment is activated, or if the venv `bin`/`Scripts`
directory is in `PATH`, you can use the shorter command:

```bash
legacy-reverse scan --repo /path/to/java-project [--force] [--resolve] [--report]
legacy-reverse describe --repo /path/to/java-project [--force] [--no-llm]
legacy-reverse export-arch --repo /path/to/java-project --out arch.json
legacy-reverse import-arch --repo /path/to/java-project --in arch.json
legacy-reverse generate-arch --repo /path/to/java-project
legacy-reverse report --repo /path/to/java-project
```

Without activation, call the installed console script from the venv directly:

```bash
./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
```

```powershell
.venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
```

`scan` walks the repo, detects Maven/Gradle modules, parses every non-test
`.java` file, records observed facts with evidence and intra-class method calls,
builds the dependency graph, and writes an index to
`<repo>/.reverse/index.sqlite3`.

`report` writes `baseline.md` and `baseline.json` to
`<repo>/.reverse/reports/`: inventory counts, top modules/packages, public API
surface, candidate domain areas, low-confidence findings, and the tool's
limitations.

## MCP tools

Every heuristic tool returns a structured response carrying `confidence`,
`limitations` and `warnings`; errors are structured (`error`, `kind`,
`suggestions`). Full schemas + examples: [docs/mcp-api.md](docs/mcp-api.md).

| Tool | Purpose |
|------|---------|
| `scan_repository(repo_path, force)` | Scan + (re)build the index |
| `list_endpoints(http_method, path_contains, limit)` | REST endpoints (JAX-RS + Spring) |
| `explain_class(fqn)` | Observed facts + inferred findings + related symbols, all with evidence |
| `trace_endpoint(endpoint_id \| http_method, path_contains)` | Controller -> service -> repository trace with per-step + overall confidence |
| `get_change_impact(symbol)` | `direct_impacts` vs `candidate_impacts`, each with reason/evidence/confidence |
| `generate_context_pack(task, max_tokens, max_items)` | Explained pack: `selected_items` (with reasons) + `excluded_items` |
| `get_module_map()` | Modules, inter-module deps, external coordinates, endpoint counts |
| `get_project_overview()` | Stack, totals, role distribution, top modules, findings |
| `find_code_areas(query, limit)` | FTS keyword search over classes/methods/endpoints |
| `get_findings(subject, finding_type, limit)` | Inferred findings persisted during scan, each with evidence + confidence |
| `get_config(key_contains, profile, limit)` | Spring config (`application*`/`bootstrap*`): files + properties; secret values masked |
| `get_class_summary(fqn)` | Class description (LLM-generated if `describe` has run, else deterministic) |
| `generate_descriptions(force, no_llm)` | Generate meaningful class/method/hierarchy descriptions over the index (LLM + deterministic fallback) |
| `find_feature(topic, limit, methods_per_class)` | Topic/feature → ranked class cards **with their methods, parameters and descriptions** (no grep) |
| `get_class_card(fqn)` | Full structured card for one class: id/pkg/name/description/type/kind/class_modifiers/extends/implements/fields/methods |
| `export_architecture(out_path?)` | Render the index as a flat architecture JSON (reference schema, drop-in for the GigaCode generator) |
| `import_architecture(in_path)` | Load descriptions from a flat architecture JSON into the index (imported wins over LLM/fallback) |
| `generate_architecture()` | Run gigacode-cli's `architecture-generator` skill and import its flat JSON (configurable via env) |

## Interpreting confidence

- **high**: a direct fact or an inference over direct, unambiguous links: a
  stereotype annotation, an endpoint read from a mapping, a call found
  syntactically in a method body.
- **medium**: a heuristic inference from several signals: layer from name and
  package, a service/repository found via injection + naming.
- **low**: a guess from naming/package/keyword similarity only.
- **unknown**: no usable signal.

Details + examples: [docs/confidence-model.md](docs/confidence-model.md). The
observed-fact vs inferred-finding model: [docs/evidence-model.md](docs/evidence-model.md).

## Golden questions (evaluation)

```bash
py eval/run_golden_questions.py
py eval/run_golden_questions.py --json
```

A deterministic regression layer that scans a committed Java/Spring fixture and
checks structural quality gates: evidence/confidence/limitations present,
endpoints found, context pack non-empty. See
[docs/golden-questions.md](docs/golden-questions.md).

## Tests

Windows:

```powershell
.venv\Scripts\python -m pytest -q
```

macOS/Linux:

```bash
./.venv/bin/python -m pytest -q
```

## Layout

```text
cli.py                  CLI: scan (+ --report), describe, report
mcp_server.py           FastMCP server + tool registrations
models/evidence.py      Evidence / Confidence / Limitation / ObservedFact / InferredFinding
scanner/                repo + java parser, spring/endpoint scanners, fact emitter, indexer, pipeline
index/                  schema.sql, repository (CRUD + facts), queries, search, findings
analysis/               evidence-based tools: explain, trace, impact, context_pack, layers, report, flat_arch (flat JSON export/import)
summarizer/             llm.py (pluggable LLM client), describe.py (Phase-2 descriptions + cache), harness.py (gigacode-cli runner), deterministic class/package summaries
eval/                   golden_questions.yaml, run_golden_questions.py, fixture/
tests/                  pytest suite
docs/                   mcp-api, confidence-model, evidence-model, limitations, golden-questions
```

## Status

Verified against [Apache Fineract](https://github.com/apache/fineract) (47 Gradle
modules, ~5.3k non-test classes): **974 endpoints** (971 JAX-RS + 3 Spring),
roles classified, constructor-injection traces reaching persistence, a 147-edge
module graph, 16.5k class-dependency edges, **~48k observed facts with
evidence**, intra-class call edges, FTS index, baseline report and a green
golden-questions run. See [CHANGELOG.md](CHANGELOG.md) for the evidence-layer
work.
