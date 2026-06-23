# Golden questions

A tiny, deterministic regression/evaluation layer that answers one question:
**did the tool stay useful?** It does *not* grade prose — it checks structural
quality signals that matter for an LLM agent: every heuristic answer carries
`evidence`, `confidence` and `limitations`; endpoints are found; impacts are
split into direct/candidate; the context pack is non-empty and explained.

## Files

- `golden_questions.yaml` — the questions (≥ 8), each pinned to a tool and a set
  of structural checks.
- `fixture/` — a committed Java/Spring mini-project: `DepositController` →
  `DepositService` → `DepositRepository` → `Deposit` (entity), plus a
  `@Scheduled` job. Small enough to scan in well under a second.
- `run_golden_questions.py` — scans a throwaway copy of `fixture/`, calls the
  tool functions directly (no MCP transport) and prints a report.

## Run

```bash
py eval/run_golden_questions.py          # markdown report; exit 0 if all pass
py eval/run_golden_questions.py --json    # machine-readable
```

Exit code is `0` only when every question passes, so it doubles as a CI gate.
It is also exercised from the pytest suite (`tests/test_golden_questions.py`).

## Check types

| key | meaning |
|-----|---------|
| `expected_contains` | substrings that must appear in the JSON response |
| `expected_min_items` + `items_field` | the named list must have ≥ N items |
| `expected_fields` | top-level keys that must be present |
| `gates` | named structural checks (see `GATES` in the runner) |

Gates currently include: `has_confidence`, `has_limitations`,
`findings_have_evidence`, `steps_have_evidence`, `impacts_have_confidence`,
`pack_not_empty`, `selected_items_have_reason`.

## Note on retrieval

`generate_context_pack` retrieves by matching **source identifiers** (English),
deterministically and without translation. A realistic task therefore mentions
the domain term (e.g. `deposit`); a task phrased only in natural-language Russian
will correctly return an empty, explained pack.
