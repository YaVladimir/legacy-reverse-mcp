# MCP API

All tools return JSON. **Heuristic** tools always include `confidence`,
`limitations` and `warnings`; **errors are structured**. List/overview tools carry
`limitations` (+ `confidence`) too. The per-tool contracts below are authoritative
(the cross-cutting fields are placed at the top level rather than nested under a
`result` wrapper, which is friendlier for an agent).

## Conventions

- `confidence`: `"high" | "medium" | "low" | "unknown"` — see
  [confidence-model.md](confidence-model.md).
- `evidence`: list of `{kind, description, file_path?, line_start?, line_end?, symbol?, source}`.
- `limitations`: list of `{code, description}` — see [limitations.md](limitations.md).
- Errors: `{ "error": "not_found", "kind": "...", "query": ..., "message": "...", "suggestions": [...] }`.

## Tools

### `scan_repository(repo_path, force=False)`
Builds/rebuilds the index. Returns `{status: "scanned"|"exists", db_path, classes,
methods, endpoints, observed_facts, method_calls, ...}`.

### `list_endpoints(http_method?, path_contains?, limit=200)`
`{ count, endpoints: [{http_method, full_path, controller_fqn, handler_name, ...}],
confidence: "high", limitations, warnings }`.

### `explain_class(fqn)`
`{ class: {name, fqn, file_path, package, module, kind, role},
observed_facts: [...], inferred_findings: [{finding_type, subject, summary,
confidence, evidence, limitations, layer}], related_symbols: {injected_dependencies,
called_methods, endpoints}, confidence, limitations, warnings }`. Each inferred
finding has ≥ 1 evidence item.

### `trace_endpoint(endpoint_id? | http_method?, path_contains?)`
Controller → service → repository/persistence. Look up by id **or** by
method/path. Example (fixture, `POST /deposits/create`):

```json
{
  "query": "POST /deposits/create",
  "endpoint": {
    "http_method": "POST", "path": "/deposits/create",
    "controller_class": "DepositController", "controller_method": "createDeposit",
    "evidence": [{ "kind": "mapping_annotation", "description": "POST /deposits/create handled by DepositController#createDeposit", "file_path": ".../DepositController.java", "line_start": 13, "symbol": "DepositController#createDeposit", "source": "source" }]
  },
  "trace": [
    { "step": 1, "kind": "controller_method", "symbol": "DepositController#createDeposit", "confidence": "high",
      "evidence": [{ "kind": "mapping_annotation", "description": "POST /deposits/create handled by DepositController#createDeposit", "file_path": ".../DepositController.java", "line_start": 13, "symbol": "DepositController#createDeposit", "source": "source" }] },
    { "step": 2, "kind": "service_call", "symbol": "DepositService#create", "confidence": "high",
      "evidence": [{ "kind": "method_call", "description": "DepositController#createDeposit calls depositService.create()", "file_path": ".../DepositController.java", "line_start": 15, "symbol": "DepositController#createDeposit", "source": "source" }] },
    { "step": 3, "kind": "repository_call", "symbol": "DepositRepository#save", "confidence": "high",
      "evidence": [{ "kind": "method_call", "description": "DepositService#create calls repo.save()", "file_path": ".../DepositService.java", "line_start": 13, "symbol": "DepositService#create", "source": "source" }] }
  ],
  "confidence": "high",
  "limitations": [
    { "code": "syntactic_calls", "description": "Method calls are extracted syntactically and may miss polymorphic or reflective calls." },
    { "code": "spring_proxies", "description": "Spring runtime proxies, AOP advice and dynamic bean wiring are not resolved." },
    { "code": "interface_impl_unresolved", "description": "Interface implementations are resolved by naming convention only (no JDT/bytecode analysis)." },
    { "code": "no_call_graph", "description": "No call-site graph is built; relations are derived from the dependency-injection graph and type references." }
  ],
  "warnings": []
}
```

Step `kind`s: `controller_method`, `controller_helper` (high; same-class
delegation hop), `service_call` (high) / `likely_service` (medium),
`repository_call` (high) / `likely_repository` (medium), `persistence`.
Not found → structured error with a sample of endpoints as `suggestions`.

### `get_change_impact(symbol)`
`{ symbol, resolved: [...], direct_impacts: [{kind, target, reason, confidence,
evidence}], candidate_impacts: [{kind: "endpoint"|"test_candidate", target, reason,
confidence, evidence}], suggested_files_for_context: [...], confidence,
limitations, warnings }`. Direct = real references (field type, syntactic call,
inheritance, param/return). Candidate = endpoints of dependent controllers, test
names.

### `generate_context_pack(task, max_tokens=8000, max_items=20)`
Example (fixture, `task="deposit create"`, `max_items=3`):

```json
{
  "task": "deposit create",
  "max_tokens": 4000,
  "selected_items": [
    { "kind": "class", "symbol": "DepositController", "fqn": "ru.bank.deposit.DepositController", "file_path": ".../DepositController.java", "reason": "Exposes endpoint POST /deposits/create", "confidence": "high", "evidence": [ ... ] },
    { "kind": "class", "symbol": "DepositRepository", "fqn": "ru.bank.deposit.DepositRepository", "file_path": ".../DepositRepository.java", "reason": "Matches task keywords (role: repository)", "confidence": "medium", "evidence": [ ... ] },
    { "kind": "class", "symbol": "DepositService", "fqn": "ru.bank.deposit.DepositService", "file_path": ".../DepositService.java", "reason": "Matches task keywords (role: service)", "confidence": "medium", "evidence": [ ... ] }
  ],
  "excluded_items": [
    { "symbol": "Deposit", "fqn": "ru.bank.deposit.Deposit", "reason": "Lower relevance or token/item budget exceeded" },
    { "symbol": "DepositRequest", "fqn": "ru.bank.deposit.DepositRequest", "reason": "Lower relevance or token/item budget exceeded" }
  ],
  "context_markdown": "# Context pack: deposit create ...",
  "confidence": "high",
  "limitations": [ ... ],
  "warnings": []
}
```

Selection priority: matched endpoint controllers → matched service/repository →
entities/DTOs/injected deps → other keyword matches. Retrieval matches **source
identifiers** (English); a task in natural-language only (no domain token) returns
an empty, explained pack with a `warning`.

### `get_module_map()` / `get_project_overview()` / `find_code_areas(query, limit)`
Structured inventory/search results, each annotated with `confidence` +
`limitations`.

### `get_findings(subject?, finding_type?, limit=200)`
Inferred findings persisted during scan (e.g. low-confidence layer guesses), each
with evidence + confidence. `{ count, findings: [...], confidence: "low",
limitations, warnings }`.

### `get_config(key_contains?, profile?, limit=200)`
Spring config indexed from `application*.{yml,properties}` and `bootstrap*.*`.
`{ config_file_count, files: [{file_path, kind, profile, module_name,
property_count}], property_count, properties: [{key, value, is_secret, file_path,
profile}], confidence: "high", limitations, warnings }`. Secret-bearing values
(`password`/`secret`/`token`/…) are masked (`***`); the index keeps them raw.
Static read — `${...}` placeholders are not resolved (`config_not_resolved`).

### `get_class_summary(fqn)`
Deterministic one-line summary (role, module, endpoints, injected deps, method
count). Accepts FQN or simple name. `{ fqn, name, summary, confidence: "medium",
limitations, warnings }`. The `summarize_class` seam is where an LLM-backed summary
can later be swapped in. Not found → structured error with `suggestions`.
