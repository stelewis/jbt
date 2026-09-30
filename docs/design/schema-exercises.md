# Schema Handoff Exercises

These synthetic exercises test whether an independent implementer can carry the [acceptance histories](./acceptance.md) through the [tables](./analysis-boundary.md) and [declaration payloads](./declarations.md). They fix expected rows, equations, and failures, not a processor implementation. Delivery step 0 turns them into machine-schema fixtures and an independent reader before processors depend on the baseline.

IDs below are readable aliases for generated IDs, all within entity `E`; a fixture must substitute the canonical IDs and exact revision digests. Table projections omit only unrelated descriptive/date/provenance columns, which the fixture must populate from its synthetic evidence. They do not define relaxed table schemas. Decimal notation in tables expands to normalized `N` with null source scale unless precision is explicitly stated. Every example uses recognized events unless specified otherwise; null and zero are never interchangeable.

## One brokerage account, several relationships

Account `broker` is the single statement/coverage boundary. It contains three positions, and category `fees` is separate:

| Table | ID | Required distinguishing values |
| --- | --- | --- |
| `accounts` | broker | institution Synthetic Broker; statements expected monthly; coverage start 2026-01-01; civil zone Etc/UTC |
| `positions` | shares | account broker; Q; claim against custodian X; cost/individual; general |
| `positions` | cash | account broker; USD; claim against X; quantity/quantity; general |
| `positions` | debt | account broker; USD; claim against lender Y; quantity/quantity; general |
| `categories` | fees | name Brokerage fees; type expense; parent null |
| `categories` | opening | name Opening equity; type equity; parent null |

An opening event initializes 10 Q with principal 50, cash +100 USD, and debt -40 USD. Its book weights are +50, +100, -40, and -110 opening equity in USD. The Q root has an opening allocation and evidence of original acquisition; quantity-only cash/debt have no cost lots. One subsequent event charges 3 USD:

| Posting | Kind | Account | Position | Category | Commodity | Amount | Role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fee-cash | position | broker | cash | null | USD | -3 | cash |
| fee-expense | boundary | null | null | fees | USD | +3 | fee |

The fee's weights are -3 consideration and +3 expense. A position assertion measures cash 97 and debt -40 separately; an account-net USD assertion measures 57 and says nothing about either gross relationship. Shares remain 10 Q; they cannot be added to 57 USD without a valuation. Render shares/cash under Assets, debt under Liabilities, and fees under Expenses without creating four institutional accounts.

Reject a boundary leg with `account_id=broker`, a position leg with a category, a position whose account differs from its posting, and a category used as an assertion account. Null fee category is an explicitly unclassified working record; a categorized report fails rather than silently classifying it.

## Pool contribution, consumption, and transfer

Use cost-measured pooled positions `AP` in account A and `BP` in B, both for Q. Pools P and P2 use USD. Root lots L1 and L2 retain original acquisition evidence and dates; all fees are known zero. The applicable booking policy is `AVERAGE`, and fee attribution is explicitly `pro_rata_units` with expensed acquisition treatment and two-decimal half-even rounding. Acquisition/entry operations are atomic: a temporary origin in AP is emptied into P in the same step.

| Step / sequence | Event | Operation | Posting amounts |
| --- | --- | --- | --- |
| s1 / 0 | buy1 | pool_entry | q1: +10 Q in AP; cash1: -100 USD |
| s2 / 1 | buy2 | pool_entry | q2: +20 Q in AP; cash2: -260 USD |
| s3 / 2 | sell | reduction | q3: -12 Q in AP; cash3: +180 USD; result3: -36 USD boundary book_result |
| s4 / 3 | move | transfer | q4a: -6 Q in AP; q4b: +6 Q in BP |

Cash legs refer to separate quantity-only USD positions. Result3 refers to an income category and is not a receipt. The exact allocation and change rows are:

| Allocation / kind / step | Change | Role | Posting | Position | Lot | Pool | Units delta | Principal delta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a1 / acquisition / s1 | c1 | target | q1 | AP | L1 | null | +10 | +100 |
| e1 / pool_entry / s1 | c2 | source | q1 | AP | L1 | null | -10 | -100 |
| e1 / pool_entry / s1 | c3 | target | q1 | AP | null | P | +10 | +100 |
| a2 / acquisition / s2 | c4 | target | q2 | AP | L2 | null | +20 | +260 |
| e2 / pool_entry / s2 | c5 | source | q2 | AP | L2 | null | -20 | -260 |
| e2 / pool_entry / s2 | c6 | target | q2 | AP | null | P | +20 | +260 |
| r / reduction / s3 | c7 | source | q3 | AP | null | P | -12 | -144 |
| t / transfer / s4 | c8 | source | q4a | AP | null | P | -6 | -72 |
| t / transfer / s4 | c9 | target | q4b | BP | null | P2 | +6 | +72 |

Every change has `capitalized_fee_delta=0` and `expensed_fee_delta=0`. Allocation `method_declaration_id` names the applicable policy revision and `action_id` is null. Each allocation has at most one source; each posting's change-unit sum equals its Q amount. L1 and L2 have zero held balances after their entry steps, but their contribution edges remain.

Disposal D has `allocation_id=r`, `proceeds_total=180`, `proceeds_commodity_id=USD`, `disposal_fees_total=0`, and the fee-policy revision. It has no acquisition-origin field. Selling 12 of 30 consumes `360 * 12 / 30 = 144`; the sale weights are -144 principal, +180 consideration, -36 book result. After the sale P is 18/216; after transfer P is 12/144 and P2 is 6/72. No disposal exists for t. A transfer link names q4a as outgoing and q4b as incoming; the allocation, not the link, supplies the basis.

An independent reader adds these deltas in step order and traverses e1/e2/t for contributor lineage. It cannot report that the sale consumed L1 rather than L2. Reject a source change naming both lot and pool, a fictitious root for P, a component mismatch at transfer, and a contributor left held at a completed pool-entry step. Independently rematching original acquisitions for tax remains possible without rewriting D.

## Participation, source status, and precedence

### Source statuses are observations, not review flags

The registry for source scope `card` maps tokens `AUTH` to pending and `POSTED` to recognized for event key `purchase`. Scope `chain` maps `FAILED` to cancelled for key `attempt` and `FEE_PAID` to recognized for key `fee`. All four tokens remain text observations:

| Observation | Target table/key | Field / kind / value type | Text | Source record |
| --- | --- | --- | --- | --- |
| o1 | transactions / `["purchase"]` | source.status / source_status / text | AUTH | sr-auth |
| o2 | transactions / `["purchase"]` | source.status / source_status / text | POSTED | sr-posted |
| o3 | transactions / `["attempt"]` | source.status / source_status / text | FAILED | sr-attempt |
| o4 | transactions / `["network-fee"]` | source.status / source_status / text | FEE_PAID | sr-fee |

Decimal/date payloads, commodity, and declaration evidence are null in these observation rows. An identity declaration retains sr-auth as the purchase anchor and admits sr-posted even if its external ID changed. Source authority selects recognized participation, with provenance to o2's source and mapping. The final purchase has one set of recognized financial legs, not both authorization and posting effects. Its pending-only earlier snapshot had inspectable legs with null step and no weights/allocations.

The failed attempt remains cancelled with no financial determinations. A separate recognized fee event credits a token position by -1 and debits an expense boundary by +1. Lifecycle link F contains transaction predecessor `attempt` and posting fee `paid-fee`; it needs no invented successful successor. Review state `ignored` cannot remove that fee.

An authoritative complete replacement retracting the purchase leaves its identity/evidence addressable but removes its steps on replay. A later reversal instead has a new recognized event, opposite movements, and a predecessor/successor lifecycle link; it does not erase the original. A disappearing partial rolling record proves neither outcome. Unknown token `POSTED?`, a status from the wrong source scope, or a recognized canonical state with only an unsupported mapping fails participation resolution, not silently defaults to pending.

### Noncommutative same-day operations

Start with L holding ten Q/principal 100 at step `opening`. A same-day split and purchase of five Q/principal 60 share the date 2026-03-02 but have no comparable source instant. A reviewed ordering declaration chooses split before purchase:

```json
{
  "schema_version": 1,
  "kind": "ordering",
  "before": {"txn_id": "split-event", "step_key": "apply-split"},
  "after": {"txn_id": "buy-event", "step_key": "acquire"},
  "guards": [
    {"target": {"table": "transactions", "key": ["split-event"]}, "fields": ["event_state"], "expected_digest": "GUARD_SPLIT"},
    {"target": {"table": "transactions", "key": ["buy-event"]}, "fields": ["event_state"], "expected_digest": "GUARD_BUY"},
    {"target": {"table": "corporate_action_effects", "key": ["split-ratio"]}, "fields": ["ratio_denominator", "ratio_numerator"], "expected_digest": "GUARD_RATIO"},
    {"target": {"table": "postings", "key": ["buy-quantity"]}, "fields": ["amount", "commodity_id"], "expected_digest": "GUARD_QUANTITY"},
    {"target": {"table": "observations", "key": ["source-order"]}, "fields": ["decimal_value", "source_record_id"], "expected_digest": "GUARD_ORDER"}
  ],
  "reason": "Reviewed source confirms split before this purchase",
  "evidence": [{"kind": "source_record", "id": "sr-order"}]
}
```

The displayed guard tokens stand for digests, not accepted digest syntax. They guard participation, the reviewed action terms, purchase quantity, and source sequence evidence that make the order applicable. Stable `StepRef`s resolve to step IDs, with these rows:

| Dependency before | After | Kind | Declaration |
| --- | --- | --- | --- |
| opening | split | causality | null |
| split | buy | decision | order-v1 |

Published sequences are opening 0, split 1, buy 2; split's `step_key` is `apply-split` and buy's is `acquire`. The split removes ten/adds twenty Q under the same root and unchanged principal; the new acquisition adds five. Result: 25 Q/principal 160. The opposite evidenced order gives 30 Q/principal 160. Missing order therefore fails; IDs or row indices cannot decide it. Adding buy→split creates a cycle and fails. A source-sequence edge is valid only with same-domain sequence observations and provenance. An unrelated independent cash event can use stable-ID tie-breaking after a recorded commutativity check; inserting it can change sequence indices, not step IDs.

## Contract terms and recorded settlement

This complete linear-terms payload is retained as revision `linear-v1`:

```json
{
  "schema_version": 1,
  "kind": "contract_terms",
  "commodity_id": "F",
  "payoff": {
    "kind": "linear_difference",
    "reference_commodity_id": "USD",
    "settlement_commodity_id": "USD",
    "multiplier": {"coefficient": "10", "scale": 0, "source_scale": null}
  },
  "settlement": {
    "mode": "cash",
    "variation": "final",
    "reference_reset": "on_final_variation",
    "rounding": {"scale": 2, "mode": "half_even", "residual": "final_slice"}
  },
  "collateral": {"mode": "separate_position", "position_ids": ["margin"], "commodity_ids": ["USD"]},
  "expiry": null,
  "deliverables": [],
  "evidence": [{"kind": "source_record", "id": "sr-contract"}]
}
```

F's position is contract/reference/individual against counterparty X, with terms linear-v1. Its commodity multiplier is also 10. Margin is a separate USD claim position with purpose collateral. Contract entry has one unit, a reference lot, and no principal/capitalized/expensed component or notional weight.

| Reference change / kind | Units | Before | After | Settlement reference | Attributed settlement posting / amount |
| --- | --- | --- | --- | --- | --- |
| entry / entry | +1 | null | 100 | null | none |
| daily / reset | +1 | 100 | 103 | 103 | variation-cash / +30 USD |
| close / close | +1 | 103 | null | 105 | closing-cash / +20 USD |

Each row names the reference lot, contract position, its recognized step, USD reference commodity, and linear-v1. Close additionally removes one inventory unit and has a disposal with proceeds 20, not 50 or 80. Cash is balanced by separate boundary book-result weights of -30 and -20. Margin begins 200 and a withdrawal moves 40 to free cash: margin 160, no reference change. The reader folds references to no open contract and sums cash settlement 50; it never re-evaluates the formula to create more cash.

For collateral-only variation, a different declared terms revision uses `variation=collateral_only` and `reference_reset=none`: a +30 collateral movement leaves reference 100, closing at 105 settles 50, and collateral return is separate. It is not an alternative interpretation of the final-settlement rows above. Reject collateral-only with a reset, missing multiplier, an inconsistent instrument multiplier, and a close using reference 100 after a recorded reset to 103.

The conditional-payoff variant also has a concrete shape, without embedding a pricing program:

```json
{
  "kind": "conditional",
  "underlying_ids": ["INDEX"],
  "conditions": [
    {
      "condition_key": "call",
      "kind": "autocall",
      "underlying_id": "INDEX",
      "comparison": "gte",
      "level": {"amount": {"coefficient": "100", "scale": 0, "source_scale": null}, "commodity_id": "USD"},
      "monitoring": "dated",
      "observation_dates": ["2026-06-30"],
      "event_evidence_required": true
    }
  ],
  "cashflows": [
    {"flow_key": "redemption", "condition_key": "call", "date": null, "amount": null}
  ]
}
```

This is the `payoff` member of a terms payload, not a standalone declaration. Supply the common terms fields and retained issuer evidence; a source-reported autocall and redemption of 1,020 USD can then produce a linked recognized redemption. Without that event evidence or required amount, there is no fabricated redemption. Reject an unknown condition key, an undeclared underlying, a bare string formula in amount, or a continuous-monitoring condition with invented observed dates. A close quote above 100 does not prove an unstated intraday barrier path.

## N-ary links and concurrent quotes

Three distinct outcome instruments O1, O2, and O3 form complete set C. A `contract_terms` declaration has null commodity, `payoff.kind=complete_set`, sorted outcome IDs O1/O2/O3, one unit per outcome, and redemption of one USD for a complete bundle; cash settlement has no variation/reset, no collateral, and retained market-rule evidence. Resolution evidence is required. Its link rows are:

| Link | Kind | Role | Member kind | Member ID | Terms |
| --- | --- | --- | --- | --- | --- |
| C | complete_set | outcome | commodity | O1 | complete-v1 |
| C | complete_set | outcome | commodity | O2 | complete-v1 |
| C | complete_set | outcome | commodity | O3 | complete-v1 |
| strategy | trade_chain | open | transaction | open | null |
| strategy | trade_chain | roll | transaction | roll1 | null |
| strategy | trade_chain | roll | transaction | roll2 | null |
| strategy | trade_chain | close | transaction | close | null |

Origin is declaration for C and derived for strategy. The three outcomes remain separately held and valued; neither relation emits inventory changes. Reject C missing O3 or containing another instrument, mismatched terms revisions among members, two strategy opens, and a blob in a roll role. Partial strategy closes can have several distinct close members; chronology follows their steps, not alphabetical role order.

At the same market and instant, F has three quotes: mark 103, index 102, and last trade 104. Publish three price IDs with bases `mark`, `index`, and `trade`, all F/USD. A mark valuation of two contracts with multiplier 10 uses 2 × 103 × 10 = 2,060 as marked notional, not book acquisition cost or unrealized settlement gain. A consumer requesting index must select 102; a consumer requesting absent settlement has unavailable valuation rather than substituting mark. No uniqueness constraint may collapse rows by instrument/date alone.

## Numeric limits and rounding

| Input or operation | Exact expected outcome |
| --- | --- |
| `2^256 - 1` | 78-digit coefficient, scale 0, accepted and round-tripped without SQL int64 cast |
| A 96-digit coefficient of all nines at scale 38 | Accepted; 97 nines fails storage |
| `1e-38` / `1e-39` | First is coefficient 1/scale 38; second rejected, never rounded to zero |
| Stated `84.20`; stated `0.00` | `(842,1,2)` and `(0,0,2)` for coefficient/scale/source scale |
| `10^11 × 10^9` | Exactly `10^20`; reject `DECIMAL(38,18)` execution and use the bounded integer path |
| Four 96-digit all-nine coefficients multiplied | Product fits at most 384 digits; a fifth such factor is rejected before expansion |
| Three units with principal 10, then three one-unit reductions at scale 2/half-even | Consumed totals 3.33, 3.34, 3.33; remaining principal zero, not 0.01 |
| `1/3` | Retained rational until declared rounding; scale 2 gives 0.33, not an exact authoritative unit cost |
| ±1.005 and ±1.015 to scale 2/half-even | ±1.00 and ±1.02; no asymmetry for credits |
| 1 divided by zero; invalid negative zero coefficient; scale -1 | Typed validation/arithmetic failures |

For the sequential reductions, allocate from remaining total and units: 10/3 rounds 3.33, then 6.67/2 rounds 3.34, and the final slice takes 3.33. This is different from independently rounding the original unit average three times. Test both operand and intermediate rejection; individually valid operands do not guarantee an admitted expression. Published results must also fit the storage domain even if their intermediates fit.
