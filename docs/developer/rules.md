# Bank and Card Rules

Standing rules enrich resolved quantity events before guarded corrections. Their declarations remain inputs even when changed evidence stops matching them. Rules do not alter source observations, amounts, bindings, identity, or economic participation.

## Match the pre-enrichment record

The evaluation unit is a balancing boundary leg with its event context. Every rule sees the same immutable facts, never values assigned by another rule or a correction. Rules are effective-dated and ordered; competing event-field assignments respect rule order across all of an event's balancing legs, not the order of those legs.

| Match fields | Meaning and type |
| --- | --- |
| `transactions.payee`, `transactions.narration`, `transactions.description_as_stated` | Exact nullable descriptive text; OFX name supplies payee and description, and memo supplies narration |
| `transactions.event_kind`, `transactions.event_state`, `transactions.review_state` | Their declared enum keys before enrichment |
| `transactions.tags` | The original sorted unique text list |
| `transactions.date_authorized`, `date_traded`, `date_posted`, `date_settled`, `date_value` | Nullable civil dates, each qualified with `transactions.` |
| `postings.amount` | Exact signed balancing-leg units: a source cash debit of -5 produces a +5 boundary amount |
| `postings.commodity_id`, `postings.category_id` | Resolved commodity key and original nullable category key |
| `postings.account_id` | The event's account when exactly one account owns its position legs; null for multi-account events, never an invented boundary-leg owner |
| `postings.date_authorized`, `date_traded`, `date_posted`, `date_settled`, `date_value` | Each qualified with `postings.`; effective leg dates after explicit inheritance, before corrections |
| Manifest-registered source fields | Exact record-linked observations, with the registry's text, decimal, or date type; `json_text` remains text, not an executable expression |

Source predicates require manifest registration. Absent observations are null; section-level observations without a movement subject are not promoted to event facts. Compatible keyed repeats evaluate once at their resolved event. Disagreement in source facts consumed by a rule blocks correspondence rather than choosing arbitrary matching evidence. Other catalog fields have no bank/card match view and produce `rule_semantics_unsupported`; their processors must establish their own views.

## Typed operators

- `equals` compares the field's exact semantic value. Decimal comparison ignores lexical source scale; date operands use valid ISO `YYYY-MM-DD`.
- `in` takes a nonempty homogeneous list of values of the field's type. A decimal operand remains `{coefficient, scale, source_scale}`, not a string or floating-point number. Membership for a list-valued field wraps whole lists, for example `[["a"], ["a", "b"]]`.
- `contains` is literal, case-sensitive containment of descriptive text. It accepts neither regular expressions nor identifier, decimal, date, or list operands.

Text is never trimmed, case-folded, or Unicode-normalized. `"Cafe\u0301"` and `"Caf\u00e9"` are distinct descriptive values. Identifier operands retain the catalog's strict key requirements. Null is accepted only for nullable fields.

## Fold assignments

The field registry permits:

| Field | Fold |
| --- | --- |
| `transactions.payee`, `transactions.narration`, `transactions.review_state`, `postings.category_id` | First matching assignment wins, including an explicit null for a nullable field |
| `transactions.tags` | Union of original tags and every matching rule's tags, sorted and unique |

Assignments within a rule are unique and sorted by qualified field name. Category assignments apply only to boundary legs and must resolve to registered categories. Transaction assignments belong to the parent event; category assignments belong to the individual balancing leg.

For an original payee `"Invented expense"`, two matching rules may assign different categories and overlapping tags. If the earlier rule assigns category `expense` and tags `["a"]`, and the later rule assigns category `income` and tags `["a", "b"]`, the result is category `expense` with tags `["a", "b"]`. A third rule matching an enriched payee does not match unless that was also the original payee.

Guarded corrections run after this complete rule-only result. Scalar corrections replace rule output; a tag correction replaces the union rather than adding to it. Changing rule policy alone does not invalidate a correction's reviewed economic guard.

## Explain and diagnose the same fold

Each retained report records every evaluated rule at its transaction/posting unit, effective-date eligibility, typed expected and observed operands, predicate results, and matching assignment outcomes (`applied`, `shadowed`, or `redundant`). The assignment's `selected` flag distinguishes first-wins ownership or a tag-union contribution from value change: a first scalar assignment repeating the original value is selected but redundant, and still excludes later assignments. `show` selects this recorded evidence without rerunning rules. Split children retain the original evaluation unit as their rule basis; each evaluation names its current target postings, so inspecting a child explains the original parent facts rather than claiming that rules matched the child's changed amount.

An unused rule receives a warning when it matches no resolved unit. A fully shadowed rule receives a warning when it matches but no assignment is selected. A rule that only repeats existing values receives informational redundancy, whether those values came from original evidence or earlier rules. A redundant first scalar winner is not shadowed; a later rule repeating that winner is both shadowed and redundant. A correction reproducing rule-only values is informational. These diagnostics never remove declarations or suppress financial effects. Unmatched categories remain null and are subject to the output's classification policy.

Field provenance records the contributing rule revisions. A tag union can name several rules and the authored origin of pre-existing tags; a correction has precedence over those rule origins.

Standing fixed-amount splits are [deferred](./deferred-scope.md); the supported structural operation is a conserving reviewed split. Rules do not gain matching or assignment semantics merely because another catalog field or declaration variant exists.
