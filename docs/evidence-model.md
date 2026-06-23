# Evidence model: observed facts vs inferred findings

The core idea of this tool: **never make a claim without showing what it is based
on.** Two kinds of statement, with a hard rule between them.

## Observed fact

Something read **directly** from source / config / project structure. Modelled as
a `(subject, predicate, object)` triple, default confidence `high`.

Examples:
- class `DepositController` is annotated with `@RestController`
- method `createDeposit` maps `POST /deposits/create`
- class `DepositService` is declared in package `ru.bank.deposit`
- field `depositService` has type `DepositService`

These are recorded during the scan into the `observed_facts` table, each with one
or more `evidence` rows.

## Inferred finding

A **heuristic conclusion** drawn from observed facts. It **must** carry at least
one piece of evidence, an explicit `confidence`, and may list `limitations`. The
model refuses to construct a finding with empty evidence — a finding without
evidence is treated as a bug.

Examples:
- `DepositController` belongs to the controller layer (because of the
  `@RestController` annotation — evidence: that annotation, file + line)
- the `POST /deposits/create` endpoint reaches `DepositRepository#save`
  (because of two syntactic calls — evidence: each call site, file + line)

## Evidence

Each evidence item has: `kind`, `description` (self-contained), and where
applicable `file_path`, `line_start`, `line_end`, `symbol`, and a `source`
(`source` | `config` | `build` | `structure`).

```json
{
  "kind": "method_call",
  "description": "DepositController#createDeposit calls depositService.create()",
  "file_path": ".../DepositController.java",
  "line_start": 15,
  "symbol": "DepositController#createDeposit",
  "source": "source"
}
```

## How evidence reaches a response

1. **Scan** parses sources and writes `observed_facts` (+ `evidence`) and
   intra-class `method_call` rows. (`models/evidence.py`, `scanner/fact_emitter.py`)
2. **Analysis** (`analysis/*`) reads those facts/calls, draws inferred findings,
   and *reuses the observed facts' evidence* so a finding points back at exactly
   the annotation / call / package that drove it.
3. **MCP tools** return the facts, findings, related symbols, per-result
   `confidence` and `limitations`.

## Storage

`observed_facts`, `inferred_findings`, `evidence`, `limitations` (the last two are
polymorphic via `owner_type`/`owner_id`). The older structural-smell `finding`
table is kept separate. Implementation notes: [evidence-layer.md](evidence-layer.md).
