# Limitations

This is a **source-first MVP**. It reads syntax, not semantics or runtime. The
following are deliberate boundaries — knowing them is how you use the tool safely.
Results carry machine-readable `limitations` (stable `code` + `description`) so an
agent can react to them.

## Not done (by design)

- **No bytecode analysis.** Only `.java` sources are parsed. Generated code,
  compiled-only dependencies and annotation-processor output are invisible.
- **No runtime Spring resolution.** Proxies, AOP advice, `@Profile`/`@Conditional`
  beans, `@Bean` factory methods and dynamic wiring are not resolved
  (`spring_proxies`).
- **No full polymorphic call graph.** Calls are extracted **syntactically** and
  only for receivers that are fields of the class; interface dispatch, lambdas and
  reflection are approximated or missed (`syntactic_calls`, `no_call_graph`).
- **No data-flow analysis.** The tool does not track values, nullability or taint.

## Resolution / heuristic boundaries

- **Interface → implementation** is resolved by naming convention (`*Impl`), not
  JDT/bytecode (`interface_impl_unresolved`).
- **Type matching is by simple name**, so ambiguous names over-approximate — it
  links all candidates rather than miss one (`ambiguous_simple_name`).
- **Constructor injection** is detected via Lombok
  `@RequiredArgsConstructor`/`@AllArgsConstructor` or explicit field-injection
  annotations; a hand-written constructor over a `final` field may be missed
  (`ctor_injection_without_lombok`).
- **Endpoints are annotation-only**; dynamic/programmatic registration is not
  supported (`dynamic_endpoints`).
- **Test sources are not indexed**; test references are heuristic name guesses
  (`tests_not_indexed`).
- **External/library types are not resolved** to a definition
  (`external_types_unresolved`).

## Consequence: false positives are possible

Because much of the output is heuristic, **false positives and false negatives are
expected**. That is exactly why every heuristic result ships with `confidence` and
`evidence`: treat `high` as reliable, `medium` as a strong lead to verify, and
`low`/`unknown` as a hint. Always read the cited file + line before acting.

The reusable catalogue of limitation codes lives in `models/evidence.py`
(`LIMITATIONS`).
