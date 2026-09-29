# Declaration Payloads and Registries

This page owns the structured inputs carried by `declarations.payload_json` and the semantic registries shipped with the [tabular contract](./analysis-boundary.md). Tables own published facts and determinations; these payloads own the decisions needed to produce or explain them. [Schema exercises](./schema-exercises.md) instantiate the nontrivial shapes. Machine-readable schemas implement these contracts in delivery step 0; a free-form JSON object is not an implementation of them.

## Common shapes and validation

Every payload has `schema_version:I=1` and `kind:S` equal to its declaration kind, followed only by the fields of that variant. Unknown fields, variants, versions, and enum values fail validation. `S`, `I`, `B`, `D`, `L`, `N`, and nullability follow the tabular notation; JSON `N` is `{coefficient:S, scale:I, source_scale:I?}`. In type expressions, `/` separates enum alternatives; `null` is the JSON null value. Required nullable keys are present with null; absence is not another default. Lists of structured objects have declared keys and ordering, not arbitrary order-dependent maps.

Reusable shapes:

| Shape | Fields and meaning |
| --- | --- |
| `RecordRef` | `table:S`, `key:[typed scalar, ...]`; table and local PK arity/types resolve against the table schema, in the declaration's entity |
| `EvidenceRef` | `kind:source_record/declaration`, `id:S`; declaration evidence names an immutable revision |
| `StepRef` | `txn_id:S`, `step_key:S`; stable semantic operation key within the event, not its sequence number |
| `Guard` | `target:RecordRef`, `fields:L` (nonempty), `expected_digest:S`; digest of the versioned, typed projection of exactly those fields, including explicit nulls, under the row canonicalization contract |
| `Rounding` | `scale:I` in 0–38, `mode:half_even/half_up/toward_zero`, `residual:final_slice`; stable allocation order determines the final slice, whose adjustment is published |
| `Money` | `amount:N`, `commodity_id:S`; never an unqualified monetary number |
| `Binding` | `source_scope_id:S`, `source_section:S`, `source_position_key:S?`; scope resolves in the retained binding map |

Payload revision digests cover kind, schema version, entity, stable declaration key, effective interval, and all payload fields under canonical JSON serialization; paths/locators are custody metadata rather than financial meaning. Do not include the digest itself. A typed field cannot contain executable expressions, Python imports, or a network URL to resolve during a build. Evidence locators reference retained evidence, not an instruction to fetch it.

An active `RecordRef` resolves in this snapshot; an active `StepRef` resolves to a recognized event's declared operation. References retained solely in superseded/retracted decisions need not resolve as active targets. Guards never follow retirement redirects. Self-derived keys are computed from stable identity rules, not by hashing a payload that contains its own revision ID.

## Identity and structural declarations

Each row below is a closed payload variant. Fields borrowed from a published table include its local PK and have exactly its types and combination rules; only the named fields are borrowed. Generated `entity_id` comes from the enclosing project. Input identity keys are stable authored keys, never display names.

| Kind | Payload fields beyond the common envelope |
| --- | --- |
| `account` | The `accounts` fields except `declaration_id`; `bindings:[Binding]`; `publication_lag_days:I` (nonnegative); `period_rule:calendar/explicit/none`; `expected_periods:[{start:D,end:D,due:D}]`; `coverage_basis_ids:L`. Explicit periods use inclusive endpoints, are ordered/nonoverlapping, and cover the declared expectation window through `as_of`; due cannot precede end. Irregular and non-calendar cycles use explicit periods, including known empty periods. Calendar monthly/quarterly/annual cadence uses civil calendar periods and end plus lag, with an empty list. No expected statements requires rule none and an empty list. |
| `category` | The `categories` fields except `declaration_id`; names/parents classify, not hold inventory |
| `commodity` | The `commodities` fields; `symbols:[{symbol_id:S,symbol:S,source_scope_id:S?,exchange:S?,valid_from:D,valid_to:D?}]`, sorted by symbol ID |
| `counterparty` | `counterparty_id:S`, `name:S` |
| `position` | The `positions` fields except `declaration_id`; `bindings:[Binding]`; `booking_declaration_id:S?`, required for cost/reference measurement; quantity-only permits null or an explicit `NONE` policy |
| `assertion_scope` | The `assertion_scopes` fields except `declaration_id`; `quote_commodity_id:S?`, `valuation_evidence:[EvidenceRef]`, `tolerance:N` (nonnegative in measured units). Unit checks have null quote and empty valuation evidence; value checks require quote and sufficient valuation evidence. |
| `action_scope` | `commodity_id:S`, `selection:instrument_all/explicit`, `account_ids:L`, `position_ids:L`, `lot_ids:L`, `pool_ids:L`. Instrument-all has empty lists; explicit requires at least one nonempty list. Each nonempty dimension restricts pre-action eligibility, with lot/pool lists forming one inventory-slice dimension. Selections must be mutually consistent; they never create absent holdings. |
| `opening_lot` | `opening_txn_id:S`, `posting_key:S`, `origin_key:S`, `position_id:S`, `units:N`, `acquisition_date:D?`, `principal:Money?`, `fees:[{fee_key:S,amount:Money,treatment:capitalized/expensed/attribution_only}]`, `fees_complete:B`, `broker_lot_id:S?`, `reference:N?`, `reference_commodity_id:S?`, `evidence:[EvidenceRef]`. Cost and reference components follow measurement rules; empty incomplete fees are unknown, not zero. |
| `corporate_action` | The `corporate_actions` fields except `action_id` and `txn_id`, plus `event_key:S`, `event_state:S` and `review_state:S` using the transaction enums, `effects:[corporate_action_effects fields except action_id, effect_id, effect_index]`, `evidence:[EvidenceRef]`. The authored anchor/event key generates event identity; effects sort by stable effect key, which supplies identity rather than the generated serialization index. |
| `link` | `link_id:S`, `link_kind:S`, `terms_declaration_id:S?`, `members:[{role:S,kind:S,id:S}]`, `evidence:[EvidenceRef]`; members sort by role/kind/ID and satisfy the link table's exact role/cardinality rules |

Account and position bindings must resolve uniquely in their declared scope. Multiple same-currency relationships require source position keys or reviewed distinctions; an absent source key cannot arbitrarily select a lender. Declarations do not rewrite extracted account identifiers.

Source precedence and rendering are versioned build-configuration shapes, retained with the manifest rather than invented as categories or transactions:

- `source_authority:[{scope:RecordRef,period_start:D,period_end:D,fact_kind:movement/balance/action,coverage_basis_id:S?,authoritative_sources:[RecordRef],mode:confirming/complete_replacement,evidence:[EvidenceRef]}]`. Movement/balance scopes are accounts and require a coverage basis; action scopes are commodities with null coverage basis. Sources name retained statement or source-record rows, not ambiguous bare names. Complete replacement requires evidenced completeness/revision semantics over precisely that scope. Conflicts fail; outside the scope nothing is eclipsed. Distinct price quotes remain observations, with selection owned by valuation policy rather than this precedence relation.
- `rendering:[{sink:beancount/hledger,target:RecordRef,positive_name:S,negative_name:S?,primary_date_role:authorized/traded/posted/settled/value}]`. Targets are positions or categories; optional negative name handles a claim that crosses sign without changing its upstream identity. Validate names/collisions against the chosen sink; omitted required date or unrepresentable position fails that sink.

## Inventory policies

| Kind | Closed fields and applicability |
| --- | --- |
| `booking` | `position_ids:L` (nonempty), `method:STRICT/FIFO/LIFO/AVERAGE/NONE`, `acquisition_date_role:traded/posted/settled/value`, `tie_break:origin_key`, `fee_attribution_declaration_id:S`, `rounding:Rounding`. `NONE` explicitly disables cost determination and is invalid for cost/reference measurement; `AVERAGE` requires a pool. `STRICT` is a declared choice, not a missing-policy default. |
| `pool` | `pool_id:S`, `position_id:S`, `book_commodity_id:S`, `booking_declaration_id:S`, `eligibility:{mode:all_origins/listed_origins,lot_ids:L}`, `fee_treatment:capitalized/expensed`, `rounding:Rounding`, `conversions:[{lot_id:S,component_kind:principal/capitalized_fee/expensed_fee,source_commodity_id:S,target_commodity_id:S,rate:N,evidence:[EvidenceRef],rounding:Rounding}]`. All-origins admits roots/branches in the declared position and has an empty list; listed-origins requires a nonempty list. Neither admits unlike instruments. Transfers from another pool carry that pool's contribution graph and require compatible policies, not invented origin selections. |
| `fee_attribution` | `method:source/pro_rata_units/explicit/none`, `acquisition_treatment:capitalized/expensed/attribution_only`, `rounding:Rounding`, `slices:[{fee_source:RecordRef,target:RecordRef,amount:Money}]`. Explicit/source methods require evidenced slices; pro-rata/none have empty slices. Source figures take precedence where available. `none` is explicitly incomplete attribution, not zero. |

Selectors are economic decisions, not tax elections. A cost reduction may proceed only with known required basis and applicable fee treatment. Reference inventory uses individual selection with attribution-only acquisition fees. Quantity-only positions require no fictitious lots; an explicit `NONE` policy, if supplied, states that same absence of cost determination.

Pool entry/conversion, transfer, action, and reduction allocations name the policy revision used. Per-component source/target totals and residuals are published; readers do not reinterpret these policies to choose another result.

Fee slice sources are posting records or opening-lot declarations, and targets are root lots or disposals, matching `fee_allocations`. Pool fee treatment must agree with its booking policy's fee declaration; inconsistent policy references fail rather than establishing another precedence rule.

## Identity, ordering, rules, and corrections

| Kind | Closed payload |
| --- | --- |
| `identity` | `record_kind:transaction/corporate_action`, `event_key:S`, `anchor:{kind:source_record/authored_key,id:S}`, `members:[{source_record_id:S,event_key:S}]`, `authored_keys:L`, `retired_ids:L`, `successor_ids:L`, `guards:[Guard]`, `reason:S`. Members sort by source ID/event key and are disjoint across active identities. Anchor belongs to membership or authored keys; explicit merge/split decisions retain retired/successor references. |
| `ordering` | `before:StepRef`, `after:StepRef`, `guards:[Guard]` (both events covered), `reason:S`, `evidence:[EvidenceRef]`. This is a precedence edge, not a supplied sequence number; endpoints differ. |
| `rule` | `order:I`, `matches:[{field:S,operator:equals/in/contains,value:typed value}]`, `assignments:[{field:S,value:typed value}]`, `split:[{child_key:S,amount:N,category_id:S?}]`. Predicates are ANDed; `contains` is literal text containment, not regex/code. `in` takes a nonempty homogeneous list. Empty matches are an explicit catch-all. Splits conserve original instrument units exactly and have distinct stable child keys; no split means an empty list. |
| `correction` | `operation:assign/select_lots/supersede/retract`, `target:RecordRef?`, `guards:[Guard]`, `assignments:[{field:S,value:typed value}]`, `selection:[{position_id:S,lot_id:S,units:N}]`, `prior_declaration_id:S?`, `reason:S`, `journal_sequence:I`. Assign/select require target and nonempty guards; select requires exact slices and guards for inventory and event facts. Supersede names a prior decision and carries a guarded replacement; retract names the prior decision and has no replacement values. Unused lists are empty. |

Journal sequence is unique and contiguous in retained append order; explicit retraction/supersession, not effective-date guessing, resolves decisions. Assign has nonempty assignments and empty selection; select has the reverse. Supersede requires a target and exactly one of these two replacement forms, with its corresponding guards. Retract has null target and empty assignments/selection/guards. Prior revision is null for assign/select and required for supersede/retract. Retraction of a decision differs from retraction of an economic event. A `select_lots` correction cannot select fictitious constituents of a pool.

The versioned field registry has `field_name`, `target_table`, `value_type`, `nullable`, `units`, `rule_mode`, and `correction_allowed`. It includes:

| Fields | Type | Rule mode | Correction |
| --- | --- | --- | --- |
| `transactions.payee`, `transactions.narration` | nullable text | first assignment wins | guarded assignment |
| `transactions.tags` | sorted unique strings | union | guarded replacement |
| `transactions.review_state` | review enum | first assignment wins | guarded assignment |
| `postings.category_id` | nullable category FK, boundary legs only | first assignment wins | guarded assignment |
| `transactions.event_state`; transaction/posting date fields and override modes; `postings.amount` | their exact table types | prohibited | guarded assignment, with all dependent checks replayed |
| Identity keys, source values, account/position bindings, steps, weights, allocations, references | their schema types | prohibited | no scalar edit; reviewed identity/structural declaration or lot selection produces new determinations |

Rules match typed merged observations, including manifest-registered source fields, but assign only the allowlist above. Rule order is nonnegative and unique within each declared source; explicit source composition order precedes it. A typed value is validated against its field registry entry; `in` alone wraps that type in a list. Corrections cannot waive referential integrity or overwrite observations. A schema extension must declare assignment semantics and checks, not infer permission from a matching field name.

## Source-status and ordering registries

The manifest retains these registries with schema and content digests; importing code cannot provide an undocumented enum translation:

| Registry | Row fields and checks |
| --- | --- |
| Observation fields | `field_name:S`, `value_type:decimal/text/date/json_text`, `units:commodity/ordinal/none`, `payload_schema:S?`. `json_text` uses `observations.value_type=text` and requires a closed payload schema. |
| Status mappings | `source_scope_id:S`, `source_field:S`, `source_token:S`, `event_key:S`, `candidate_state:pending/recognized/cancelled/retracted`. Unique by scope/field/token/event key; no trimming, case-folding, or unknown-token default. |
| Source sequences | `source_scope_id:S`, `sequence_domain:S`, `field_name:S`, `direction:ascending/descending`. Only comparisons within an evidenced domain establish precedence. |
| Coverage bases | `basis_id:S`, `date_role:authorized/traded/posted/settled/value`, `opening_boundary:before_first/after_last`, `closing_boundary:before_first/after_last`, `measurement:units/value`, `balance_view:settled/available/held`, `quote_commodity_id:S?`. A value basis requires quote currency; interval endpoints use the account's civil zone, with boundaries before the first or after the last movement on that endpoint date. |

Built-in observation fields include `source.status` (text, no units), `source.sequence` (decimal, integral scale zero, ordinal), `source.sequence_domain` (text), and `broker.match` (JSON text). The broker-match payload is `{schema_version:1, acquisition_source_record_id:S, disposal_source_record_id:S, quantity:N, commodity_id:S, broker_lot_id:S?}`. Repeated matches are separate observations; totals and source origin must reconcile.

A status mapping supplies a source candidate, not authority or identity. Merge resolves competing candidates using source authority and explicit corrections, retaining all tokens and provenance of the canonical state. Absence of a record only supports cancellation/retraction when the retained complete replacement or lifecycle evidence establishes it. Unknown status permits archival extraction but blocks any participation determination that depends on it. Review status never substitutes for economic status.

Precedence kinds remain `effective_time`, `source_sequence`, `causality`, and `decision` in the table contract. Effective-time edges require comparable instants or evidenced economic dates; source-sequence edges require the domain above; causal edges identify the depended-on inventory/lifecycle determination; decisions reference the guarded payload. Conflicting edges/cycles fail. A commutative stable-ID ordering has no fabricated economic edge and publishes its commutativity check.

## Contract terms

`contract_terms` has common fields `commodity_id:S?`, `payoff:Payoff`, `settlement:Settlement`, `collateral:Collateral`, `expiry:D?`, `deliverables:[Deliverable]`, `evidence:[EvidenceRef]` (nonempty). Commodity is required except for complete-set terms, which have null commodity and explicitly enumerate their outcome instruments. Dates and amounts are source-stated or explicitly declared with provenance. Unknown required terms block the dependent calculation rather than becoming zero, one, or a pricing-engine default.

| Shape | Closed variants |
| --- | --- |
| `Payoff` | Discriminator `kind:S`; `linear_difference` or `inverse_difference`: `reference_commodity_id:S`, `settlement_commodity_id:S`, `multiplier:N`; `option`: `right:call/put`, `underlying_id:S`, `strike:Money`, `multiplier:N`, `exercise_style:european/american/bermudan`, `exercise_start:D?`, `exercise_dates:[D]`; `stated_cashflows`: `flows:[{flow_key:S,date:D,role:principal/interest/redemption,amount:Money}]`; `conditional`: `underlying_ids:L`, `conditions:[Condition]`, `cashflows:[{flow_key:S,condition_key:S,date:D?,amount:Money?}]`; `complete_set`: `outcome_ids:L`, `units_per_outcome:N`, `redemption:Money`, `resolution_evidence_required:B=true` |
| `Settlement` | `mode:cash/physical/mixed`, `variation:final/collateral_only/none`, `reference_reset:on_final_variation/none`, `rounding:Rounding` |
| `Collateral` | `mode:none` has no other fields; `mode:separate_position` requires `position_ids:L` (nonempty), `commodity_ids:L` (nonempty) |
| `Deliverable` | `deliverable_key:S`, `commodity_id:S`, `units_per_contract:N`, `role:underlying/strike_cash/redemption`, `condition_key:S?`; units are signed for the declared long/right side, multiplied by actual signed contracts |
| `Condition` | `condition_key:S`, `kind:barrier/autocall/default/conversion`, `underlying_id:S?`, `comparison:gte/lte/null`, `level:Money?`, `monitoring:continuous/dated/source_event`, `observation_dates:[D]`, `event_evidence_required:B=true` |

Lists use stable child keys; dated lists are increasing and unique. Multipliers and complete-set units are positive. Barrier/autocall conditions require underlying, comparison, and level; default/conversion may be purely source-event conditions with all three null. Dated monitoring requires dates; other modes have empty dates. Conditional cashflows with unknown amount retain null until evidenced, not a formula executed from text. This supports source-evidenced structured notes without claiming to simulate an unstated payoff path. Original full specifications remain retained evidence; adding another supported term requires a typed schema extension, not an `extras` escape hatch.

Linear cash settlement is signed units times multiplier times `(settlement_reference - current_reference)` in settlement currency. Inverse cash settlement is signed units times multiplier times `(1/current_reference - 1/settlement_reference)` in the declared settlement currency, with nonzero references and explicit rounding. The multiplier's denomination is fixed by the payoff variant; an inverse multiplier carries the conversion dimension and is not a unit-cost weight. Negative linear prices are valid; no universal price-positivity guard is imposed.

Final variation requires `reference_reset=on_final_variation`; collateral-only/none require `none`. A reference-measured position's published terms revision is the one used by each reference change. Collateral moves only through the declared separate positions. Physical/mixed exercise requires deliverables; option expiry alone does not fabricate delivery. Options require expiry. Bermudan style requires exercise dates no later than expiry; American requires exercise start no later than expiry; European uses expiry alone. Unused exercise start is null and unused dates are empty. Complete-set terms require at least two outcome IDs and agreement with link membership; redemption occurs only on an evidenced event. Terms cannot collapse outcomes into one instrument.

## Authored facts

`authored_fact` has `event_key:S`, `fact_kind:transaction/price/assertion/note`, `record:typed record`, and `evidence:[EvidenceRef]`. Price uses the price table fields except `price_id` and origin; assertion uses scope, date, assertion kind/completeness, time bundle, and a keyed list of position/commodity/amount observations. Transaction uses the transaction fields except generated ID, plus legs keyed by stable `posting_key` and carrying the posting fields except generated `posting_id`, `txn_id`, `posting_index`, and `step_id`. Note uses narration/date/time and has no financial legs. Its declaration key/event key supplies the anchor; model booking supplies derived steps and allocations. Authored initialization of cost/reference lots uses the separate `opening_lot` payload, not invented acquisitions.

All variants use the same boundary validation as extraction. Authorship is provenance, not a bypass for conservation, coverage, identity, or status checks.
