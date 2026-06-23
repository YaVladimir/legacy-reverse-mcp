# Golden questions

A small, deterministic evaluation/regression layer. It does **not** grade prose;
it checks that the tools keep returning the structural signals an agent needs:
evidence, confidence, limitations, found endpoints, a non-empty explained pack.

## Run

```bash
py eval/run_golden_questions.py          # markdown report; exit 0 only if all pass
py eval/run_golden_questions.py --json   # machine-readable
```

It scans a throwaway copy of the committed fixture (`eval/fixture/`: a
`DepositController → DepositService → DepositRepository → Deposit` mini-project
with a `@Scheduled` job), calls each tool function directly (no MCP transport),
and applies the checks defined in `eval/golden_questions.yaml`. The same run is
asserted from the pytest suite (`tests/test_golden_questions.py`).

## Questions (9)

`project-overview`, `module-map`, `list-endpoints`, `find-code-areas`,
`explain-controller`, `trace-create-endpoint`, `trace-get-endpoint`,
`change-impact-service`, `context-pack`.

## Check types

- `expected_contains` — substrings that must appear in the JSON response.
- `expected_min_items` (+ `items_field`) — a named list must have ≥ N items.
- `expected_fields` — required top-level keys.
- `gates` — named structural checks: `has_confidence`, `has_limitations`,
  `findings_have_evidence`, `steps_have_evidence`, `impacts_have_confidence`,
  `pack_not_empty`, `selected_items_have_reason`.

## Adding a question

Append to `eval/golden_questions.yaml` with an `id`, `tool`, optional `input`, and
the checks/gates above. Keep gates structural — never assert exact wording. See
`eval/README.md` for the runner internals.
