---
id: 0005
title: "Corporate Actions as a Distinct Model Entity"
status: accepted
date: 2026-08-19
tags: [corporate-actions, booking, reconciliation]
supersedes: null
superseded_by: null
---

## Context

Splits, mergers, spinoffs, returns of capital, and similar actions change existing positions without being ordinary purchases or sales. Replacing a lot with a new purchase on the action date would lose its acquisition history. Ignoring the action makes later quantity and basis wrong.

## Decision

### A distinct event with typed terms

A corporate action is a distinct provenance-bearing fact and model entity, not an ordinary purchase, sale, or standing declaration. It records action identity, affected instruments and accounts, effective date and ordering, kind, input/output roles, and kind-specific terms such as ratio, replacement instrument, cash component, or basis allocation. Booking applies it from that date without changing earlier source facts or earlier holdings.

Source-stated terms remain attributed evidence. Missing events or terms requiring judgement, including allocations among successor holdings, are explicit reviewed authored inputs carrying their rationale and supporting references. The model does not infer them from a ticker or an attractive balancing number. A symbol change that leaves the underlying instrument unchanged belongs to its dated symbol history, not to a disposal and repurchase.

### Transformations preserve lineage

Simple splits preserve the position's origin and acquisition date while transforming units and per-unit basis. An action with several outputs, such as a spinoff or mixed cash-and-stock merger, preserves lineage **and** assigns distinct successor branches. The total basis treatment depends on the actual event: a ratio alone cannot allocate cost between parent and child, cash in lieu can realize a disposal, and a return of capital may reduce basis. An unsupported or insufficiently evidenced transformation fails rather than being forced through a generic split formula. Jurisdiction-specific tax treatment remains outside economic booking.

| Action | Booking obligation |
| --- | --- |
| Split or reverse split | Apply ratio per origin; preserve total economic basis when no other component changes; represent fractional entitlements and their settlement separately |
| Spinoff or stock distribution | Preserve parent/child lineage and distinct instrument identities; require an evidenced or declared allocation where economic cost must be divided |
| Merger or exchange | Map each predecessor to successor consideration, with stock, cash, extinguished rights, and realized components separately identified |
| Return of capital or basis adjustment | Record the stated amount and applicable economic treatment without inventing units or assuming every payment reduces basis |
| Currency redenomination | Record the stated conversion between distinct currency identities; do not collapse it into a display alias or invent a gain |
| Symbol change | Update dated symbols for the same instrument; do not transform lots |

A four-for-one split of 10 shares with total cost 1,000 produces 40 shares with the same origin and total cost, or 25 per share. It does not rewrite the original acquisition as 40 shares. For a spinoff with a reviewed 80:20 allocation, that 1,000 becomes 800 on the parent branch and 200 on the child; the distribution ratio alone does not supply 80:20. A cash-and-stock exchange need not conserve basis by subtracting all cash: its consideration and allocation rule determine the outcome.

Original acquisition dates remain lineage facts. Whether a successor inherits a holding period for a particular tax purpose is analysis, not a claim that every merger preserves legal tax treatment. A quantity-only split preserves full lot identity; a branch-changing action can require selection review under [ADR 0004](0004-lot-selection-rebinding.md).

Basis applications identify exact inventory changes. A spinoff allocation can contain a source reduction, a retained-parent augmentation, and a new-child augmentation; pointing a basis term only at that allocation does not identify its beneficiary. Each declared basis amount is checked against its targeted change and book currency. This also distinguishes return-of-capital principal adjustments from unit transformations and disposals.

### Reconcile action movements

Position reconciliation includes action movements at their effective dates. The relevant equation is opening position plus evidenced ordinary movements **and actions** equals closing position; it is not an unchanged transaction-only sum with actions ignored. A split changes units by its stated ratio while retaining total economic basis when no other component changes; other actions need their own rules, not a generic numeric conservation test. Cash is recognized once: if a broker cash posting already records an action's payment, the action identifies that movement instead of creating a second receipt. The build checks supported action arithmetic and compares against independent statements or position assertions where available. Internal consistency cannot establish that the supplied ratio is true, and a statement balance alone cannot prove completeness of all intervening records.

Checks apply per affected origin and in aggregate: ratio arithmetic, complete successor mapping, eligible holdings, allocated totals under the chosen treatment, and cash correspondence. A missing allocation, unexplained residual, duplicate cash effect, or incompatible effective ordering blocks the affected booking. Position assertions test the resulting holdings; cash balances alone cannot certify a stock split.

### Rendering

The model owns the transformed positions and action lineage. The tabular sink separates action terms from applied inventory allocations and links each applied term to its determination. Exact applied totals survive even when a stated per-unit term is rounded. Readers replay the changes once rather than applying the ratio again. A ledger sink may render paired reductions and augmentations, carrying source acquisition information and successor lineage where the format allows it. Those entries express an already determined action; they do not authorize the sink to infer cost allocation or a new holding period.

A sink that cannot represent a supported action without changing its meaning must report that limitation, not silently emit a new acquisition.

## Consequences

Holdings retain acquisition lineage across decades of transformations without falsifying earlier source facts. Actions require their own terms, provenance, and checks instead of exceptions to reconciliation.

A missing action can surface at a later independent position assertion but can remain undetected where no assertion exists. Multiple-output events require more evidence and branch identities than a split; economic cost allocation and jurisdictional tax allocation remain separately traceable.

## Alternatives considered

### Encode every action as an ordinary transaction

Rejected: it makes non-trading position changes look like purchases and can erase acquisition history.

### Exclude actions from reconciliation

Rejected: doing so conceals precisely the movements most likely to change a long-lived holding.

### Rewrite the original acquisition

Rejected: the source did not report today's changed quantity decades earlier, and rewriting it hides what happened.

### Conserve basis with one formula for every action

Rejected: splits, distributions, returns of capital, and taxable or mixed consideration exchanges have different economic components. A balancing number is not evidence of their treatment.

### Infer a basis term's output from the transformation

Rejected: several outputs can share a commodity or amount. An explicit inventory-change reference retains the reviewed selection without asking consumers to infer the intended branch from labels, totals, or ordering.

## Related

- [Position taxonomy](0002-position-taxonomy.md)
- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Lot-selection rebinding](0004-lot-selection-rebinding.md)
