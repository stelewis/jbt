---
id: 0004
title: "Lot-Selection Correction Rebinding"
status: accepted
date: 2026-08-19
tags: [corrections, booking, lots, durability]
supersedes: null
superseded_by: null
---

## Context

A lot-selection override references model inventory, whose identity and availability come from booking even when a broker supplies its own lot identifier. Changing an acquisition's interpretation, an earlier disposal, or a corporate action can change which positions are open when the override applies. A booking-method change can make a formerly necessary selection inapplicable. A lost selection may change reported economic gain without any visible change to the disposal's source record.

## Decision

### Select origin and quantity

A selection names its target reduction, canonical acquisition leg and origin key, selected quantity, position, and any successor branch. It is applied only to eligible inventory open in that position when the target disposal occurs. A pool selection names the pool and quantity under its declared policy; it cannot claim that one constituent acquisition was individually consumed. Published allocations record the chosen slices without changing posting grain.

Neither cost, acquisition date, inventory order, nor a sink's label identifies the selected lot. Cost changes when acquisition commissions are included; two acquisitions can share a date and price; labels can change with rendering. A simple split preserves the lot, while a spinoff or partial reference reset produces distinct branches derived from the determining event and output role. Adding a confirming observation does not replace the canonical acquisition anchor.

Quantity meaning is checked as well as identity. An override selecting an entire remaining origin can survive a split if its scope still agrees with the target reduction. A literal quantity does not become four times larger merely because a four-for-one split occurred: the action lineage and reduction units must justify the allocation, or review is required.

### Rebind transactions before inventory

The correction journal is append-only as in [ADR 0001](0001-correction-identity.md). If an acquisition is reinterpreted, a transaction correction is resolved first; affected lot selections are then checked against the rebuilt inventory. A missing, ambiguous, already closed, or otherwise inapplicable selection **fails** the build. An unused selection after changing a policy is not silently dropped or replaced by a default method. Reviewed bindings, supersessions, or retractions are appended to the journal; generated old lot tables are not migrated, and a build never rebinds anything automatically.

Lot review compares old and new origins, action branches, opening state, and available quantities over the affected accounts through each selected reduction. It exposes changes caused by upstream corrections and policy changes even when transaction identities themselves survived. The diff distinguishes identity relocation from a different economic selection; matching cost or date is only a candidate-finding aid.

Failure diagnostics name the journal entry, target disposal, selected origin and quantity, inventory immediately before disposal, and whether the cause is absent origin, multiple branches, insufficient remaining quantity, wrong account, incompatible policy, or changed expected facts. Historical orphaned selections remain in the journal, including after a later retraction or supersession.

## Consequences

Transaction review and lot review are distinct operations with an enforced order. Booking explicitly depends on lot-selection corrections, not only on corrected transaction values.

Quantity-only actions usually preserve origin; multi-output actions require branch-aware selections. Upstream changes can require human review even if the target disposal did not change. This is preferable to silently replacing the selection with a house rule or a different gain.

## Alternatives considered

### Select by adjusted cost or acquisition date

Rejected: fees change cost, multiple lots share dates, and actions can transform both.

### Fall back to a declared method after a selection fails

Rejected: this silently replaces an explicit human judgement with a different economic result.

### Treat a selection as an ordinary source-record binding

Rejected: transaction identity alone cannot prove that a derived position remains open after earlier reductions or policy changes. Rebuilding inventory and checking applicability is a separate obligation.

### Rebind by label or automatically during a build

Rejected: labels are renderings, and a plausible inventory candidate is not authority to transfer a human selection.

## Related

- [Correction identity](0001-correction-identity.md)
- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Corporate actions](0005-corporate-actions.md)
