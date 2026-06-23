# Confidence model

Every heuristic result carries a confidence level. It answers one question: *how
much should you trust this, and why?* It is **not** a probability — it is a label
for the strength of the underlying signal.

| Level | Meaning | Typical sources |
|-------|---------|-----------------|
| `high` | A direct fact, or an inference over direct (unambiguous) links. | Stereotype annotation (`@RestController`); endpoint read from a mapping annotation; a method call found syntactically in a body. |
| `medium` | A heuristic inference from several signals that agree. | Layer from class **name + package**; service/repository found via DI injection + naming. |
| `low` | A guess from naming / package / keyword similarity alone. | A `*Service`-named class with no annotation and a neutral package; a keyword match. |
| `unknown` | No usable signal. | A plain class with no annotation, no telling name and no telling package. |

## Per-tool rules

### `explain_class` — Spring layer
- **high** — a direct Spring/JAX-RS stereotype annotation is present.
- **medium** — the class *name* and the *package* agree on a layer.
- **low** — only the name (or only the package) hints at a layer.
- **unknown** — neither name nor package nor annotation hint at a layer.

### `trace_endpoint` — per step
- **high** — endpoint found directly via mapping annotation; controller method
  found directly; a downstream call found **syntactically** in the method body.
- **medium** — service/repository found via injection + naming; persistence found
  via an injected `JdbcTemplate`/`EntityManager`/… field.
- **low** — link found only by package/name similarity.

Overall trace confidence = `high` only if every step is `high`; otherwise it is
the weakest step (it never overstates the chain).

### `get_change_impact`
- **direct_impacts** are `high` when the dependent references the symbol via a
  field of its type, a syntactic call, or inheritance; `medium` for a param/return
  type reference.
- **candidate_impacts** are `medium` (endpoints of dependent controllers) or
  `low` (test-name matches).

### `generate_context_pack`
- `high` for the controller behind a matched endpoint; `medium` for matched
  service/repository/entity classes and injected dependencies; `low` for other
  keyword matches.

## Aggregation

When a response has one overall `confidence`, it is the **weakest link** of the
parts it summarises (so a chain is only as trustworthy as its shakiest step).
List-style tools may instead place `confidence` on each item.
