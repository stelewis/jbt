---
id: 0010
title: "Authoritative Inventory Allocations and Exact Components"
status: accepted
date: 2026-09-28
tags: [inventory, amounts, publication]
supersedes: null
superseded_by: null
---

## Context

One economic leg can consume several lots. Relationship membership cannot say how much moved from each origin, and a pool reduction is not a choice of constituent acquisition. Per-unit decimals also cannot preserve an exact total of 10 over three units.

## Decision

Keep postings at economic-leg grain. Publish booking separately as allocation headers and signed inventory changes. Each allocation identifies one source inventory slice, if any, and its destinations, if any. Each change names a position and either a lot or a pool, its posting, signed units, and exact principal, capitalized-fee, and expensed-fee-attribution deltas. Transfers and pool entry/exit use the same contract as acquisitions, reductions, and transformations, with different conservation rules.

Lots retain acquisition lineage independently of custody. Successor branches remain separate per predecessor origin; a merger does not combine origins into a fictitious acquisition. Pools have their own declared identities. Pool entry removes individually held units and records a contribution; pool reduction consumes the pooled balance, not a named constituent. The ordered contribution, transfer, and reduction graph retains the acquisitions that contributed without asserting particular constituent units were sold. Tax models rematch the original stream independently.

Exact totals are authoritative. Remaining state is the sum of recorded deltas; per-unit amounts are optional derived views and never inputs to that fold. Book carrying cost is principal plus capitalized fees. Expensed acquisition fees remain attributable to inventory for analysis but are not carrying cost and are not posted again on disposal. Original-currency fee slices travel with inventory changes, including reference contracts; converted book deltas and any pool-denomination conversion remain separate determinations. Every allocation uses a declared rounding and residual rule; allocated and remaining components conserve the input totals exactly.

Publish signed monetary weights separately from commodity units. Reference prices and notionals are not cost weights. A disposal references its reduction or transformation allocation and separately records gross proceeds and disposal fees. Released basis is the consumed amount less any exact components retained in successors; no per-unit quotient substitutes for these totals. Source amounts and fee currencies remain observable even where booking converts them into a book denomination.

Positions have explicit stable identities for the account/instrument/relationship being measured. Multiple counterparties, collateral, and free holdings can coexist in one reconciliation account. Assertions name either an explicit position scope or an explicitly net account scope. A net balance is not evidence that each gross position is correct.

## Consequences

An independent consumer can recover open lots, pools, contract references, exact components, and statement comparisons without choosing policy or reparsing evidence. The extra relations publish financial determinations, not redundant aggregate snapshots. Their fields and invariants belong to the [tabular contract](../design/analysis-boundary.md).

## Alternatives considered

### Split postings per match

This confuses observed/economic legs with booking allocations and multiplies fees.

### Use generic links for allocation

Membership lacks quantities, components, and source/destination semantics.

### Represent a pool as an acquisition

This loses contributor lineage and invents an acquisition date.

### Store only unit costs

This cannot exactly encode nonterminating quotients.

### Force one position per account/commodity

This nets distinct rights and obligations before reconciliation.
