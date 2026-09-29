---
id: 0003
title: "Booking Determination and Lot Identity"
status: accepted
date: 2026-08-19
tags: [booking, lots, investments]
supersedes: null
superseded_by: null
---

## Context

If each ledger sink selects its own lot, the same disposal can have different proceeds, basis, and economic gain in different outputs. Matching only at report time also lets a disposal of a never-held position go undetected. Booking therefore needs the complete ordered record, not an arbitrary report slice. It must respect [position kinds](0002-position-taxonomy.md): a bank deposit, a purchased security, and a cash-settled future are not interchangeable cost lots.

## Decision

### One ordered determination

The model books once from recognized events, linked source records, declarations, opening positions, corporate actions, and active record-targeted corrections. It produces atomic ordered steps, explicit inventory allocations and component totals, reference transitions, and supported economic results. [ADR 0009](0009-event-participation-and-replay.md) owns participation and replay order; [ADR 0010](0010-authoritative-inventory-allocations.md) owns publication of those determinations. An ordering ambiguity that can change inventory or gain requires evidence or an explicit decision, not an arbitrary extract sort.

Booking folds inventory state but remains a pure function of those inputs. Reports aggregate booked records over arbitrary subsets without rebooking or rejecting a disposal merely because its acquisition lies outside the report interval. A report flag cannot change the economic match. When evidence does not establish a gain required for an output, that determination fails; unresolved basis is never a fabricated zero.

### Origins, partials, and pools

Acquired lots derive their identities from the canonical acquisition leg and stable origin key under [ADR 0008](0008-canonical-economic-identity.md), not the current authoritative source occurrence. Successors additionally identify the determining event and output role. Cost, quantity, dates, account path, and rendered label are attributes, not identifiers. This remains true when acquisition fees alter economic basis or a split changes quantity. Opening positions need evidenced acquisition details if matching or gain requires them; a single opening total cannot be treated as a set of known lots.

Each cost lot carries its instrument, account and position relationship, original and remaining units, acquisition consideration, separately allocated acquisition fees, total economic cost and currency, acquisition date, and lineage. Reference-measured positions instead retain entry/reset reference, reference denomination, units, contract terms, and unsettled state. Rendered labels derive from origin with collision checks, not from a user-maintained namespace; changing the label scheme cannot change a selection.

A reduction records one or more `(inventory slice, quantity, allocated basis or reference)` matches: an individual slice names a lot origin/branch, while a pooled slice names the pool. Partial disposal retains the remaining slice; full disposal closes it. The sum consumed must equal the reduction and cannot exceed available quantity. For example, selling 12 individually tracked units may consume all 10 from one acquisition and two from another, with both allocations visible. Fees, proceeds, and basis allocations retain totals and declared rounding; independently rounding a per-unit average must not make cost disappear.

A pool has a stable declared identity scoped by position, measurement, and policy, plus contribution and reduction lineage. Contributions move lot quantities and components into the pool; they do not leave a second individually held balance. A pooled reduction consumes aggregate units and components without asserting particular constituent acquisitions were sold. Inter-pool transfers retain the ordered contribution graph. Changing pooling policy cannot silently reinterpret identities or discard existing selections; unsupported reconstruction of individual remaining origins fails explicitly.

### Selection and basis methods

Selection follows, in order: an unambiguous source-stated match, an active lot-selection override, then an applicable declared booking policy. Source broker lot IDs are mapped to model origins; they are not substitutes for model identity. A correction to an erroneous source statement is explicit. A conflicting, inapplicable, or unused override is an error, not something precedence allows the build to ignore.

Accounts declare applicable methods by position or inventory class and inherit explicit project policy. Initialization proposes `STRICT` for individual inventory and records the accepted policy in declarations; runtime models never fill a missing policy silently. A policy change is a versioned input change, not a report option.

| Method | Meaning and applicability |
| --- | --- |
| `STRICT` | Individually tracked inventory must have one determinate allocation; ambiguity requires a selection |
| `FIFO` / `LIFO` | Consume eligible individually tracked origins in acquisition order or reverse order; the declared method breaks equal-date selection ties by stable origin key, never by inventing event chronology |
| `AVERAGE` | Explicitly declared eligible contributions form a weighted-average pool; each reduction consumes proportional total cost while preserving contribution lineage |
| Quantity-only (`NONE`) | Explicitly no cost determination for the declared position; quantities are checked, but no known basis or gain is implied |

For weighted average, hold total units and total cost rather than repeatedly rounding an average price. If a pool contains 10 units costing 100 and 20 costing 260, a 12-unit reduction consumes 144 of total basis, leaving 18 units and 216. The policy defines pool boundaries, eligible events, fee treatment, denomination, and allocation precision. It cannot average unlike currencies without an explicit conversion determination. Pooling is not a generic rule for debts or reference-settled contracts, and individual-lot selectors cannot masquerade as pool reductions.

For cost-measured lots, acquisition fees enter economic acquisition cost under the declared treatment; disposal costs reduce proceeds. Both remain separately traceable to the source. Commission-inclusive per-unit cost is derived, often appears in no source, and cannot identify a lot. Reference-measured contracts retain commissions as attributable movements rather than inventing an acquisition cost or altering the reference price.

### Economic inventory and tax analysis

The booked match describes the economic record under the broker evidence and the chosen project policy. It is **not** an assertion that a jurisdiction recognizes that lot for tax. Jurisdictional analysis may rematch under its rules and elections, preserving the booked result and explaining the difference. Tax basis is not written back into economic basis.

Every match and cost allocation identifies whether its selection or treatment came from source evidence, a correction, or a named policy and version. Declared FIFO can determine an economic record without claiming that the broker stated FIFO. Source-reported tax basis remains attributed evidence, not an instruction to overwrite economic basis.

The disposal's economic book result is published with posting role `book_result` and supplies the balancing effect between matched carrying cost and proceeds under that treatment. For matched cost 100 and proceeds 120, the signed book weights are `−100 + 120 − 20 = 0`; the final component is the derived book result, not another receipt. It is neither an external cash flow nor a taxable-gain assertion. Acquisition consideration, acquisition-fee allocations, gross proceeds, and disposal fees remain distinct; a balancing result cannot replace those inputs. A downstream alternative match can recompute their allocation and explain its difference without recovering source amounts from a net gain.

### Transfers preserve acquisition lineage

An in-kind transfer moves inventory between positions without becoming a sale. It preserves acquisition origin, original dates, and the transferred slice's exact principal and fee components. Each source lot or pool slice has an explicit allocation with source and destination changes; a link alone cannot express that allocation. A transfer fee remains a separate movement with declared treatment; neither a changed account nor a transfer-date price creates a new acquisition basis. Distinct departure and arrival times use an explicitly declared in-transit position when inventory must remain represented between them.

The complete acquisition, split, transfer, and partial-disposal example belongs to the [cross-capability acceptance histories](../design/acceptance.md#cross-capability-histories). Booking must preserve principal and fee allocations across consumed and remaining holdings, with quantities measured in the unit scale established by each action. A result must never charge an allocated fee twice or treat a split as conserving numeric units.

### Position-specific reductions and failures

Property cannot cross into negative ownership by an unnoticed sale. Claims can change sign, while a contract changing direction closes its old exposure before opening the opposite direction. Closing a reference-measured contract uses its unsettled reference after prior settlements; a mere collateral movement does not reset it. A short position is a claim to deliver shares, not negative property. Each reduction must match a position actually open at that time.

For a reversal from long one contract to short one, a two-unit sell closes the long and opens a short as two linked booking components of the source fill. It does not realize gain on two closing units or erase the opening reference. Claim sign changes such as an overdraft do not require permission to own negative property.

Errors identify the source record and leg, state immediately before application, candidate origins and available quantities, selector or policy in effect, and the precise conflict, shortage, missing basis, or ambiguity. This diagnostic is part of the contract: “ambiguous match” without inventory cannot be acted upon.

## Consequences

Sinks agree on one booked economic result and can expose matched trade pairs and per-disposal gain without repeating selection logic. Strict matching creates review work precisely where evidence does not determine an allocation. Policy-based answers remain distinguishable from broker statements.

Inventory folding requires the full ordered history and usable opening positions. Pool contributions, partial reductions, and action successors enlarge lineage but prevent plausible totals from concealing a different allocation. Tax analysis can produce a separate match without rewriting this history.

## Alternatives considered

### Book in each sink or at report time

Rejected: the answer would depend on format or query, and reductions could escape validation.

### Default to FIFO or identify lots by cost and date

Rejected: a house rule can choose a tax-sensitive lot without review, while commissions, same-price purchases, and corporate actions make cost and date unreliable identities.

### One cost-lot method for every instrument, or no negative positions anywhere

Rejected: the former fabricates costs for reference-measured contracts; the latter prevents legitimate debt, shorts, and direction reversals. Position kind and terms define valid state changes.

### A separate `AVERAGE_ONLY` matcher

Rejected: eligibility for a pool is an explicit policy constraint, not a second averaging algorithm. One `AVERAGE` method with declared pool boundaries supplies the calculation; it cannot silently fall back to individual-lot selection for ineligible inventory.

## Related

- [Correction identity](0001-correction-identity.md)
- [Position taxonomy](0002-position-taxonomy.md)
- [Lot-selection rebinding](0004-lot-selection-rebinding.md)
- [Corporate actions](0005-corporate-actions.md)
