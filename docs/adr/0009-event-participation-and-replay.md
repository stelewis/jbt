---
id: 0009
title: "Explicit Event Participation and Ordered Book Steps"
status: accepted
date: 2026-09-28
tags: [events, ordering, publication]
supersedes: null
superseded_by: null
---

## Context

Review state does not say whether a movement is posted. Source dates and stable file ordering also cannot resolve a same-day split before a purchase or a contract settlement before its close. A downstream inventory reader needs the upstream determination, not permission to choose again.

## Decision

Every canonical event publishes a resolved state: `pending`, `recognized`, `cancelled`, or `retracted`. Recognized means admitted to the economic record, including evidenced non-cash lifecycle events and authored openings; it does not mean every leg has settled. Source status and recognition evidence remain observations. Review state is independent and cannot suppress a recognized financial effect.

Only recognized financial events produce book steps, monetary weights, inventory allocations, or reference changes. Other states retain their identity, evidence, and observed legs for inspection, but contribute no recorded balances. A failed transfer with a paid fee has a recognized fee event linked to the failed attempt; it is not a cancelled transaction whose fee is accidentally discarded. A later reversal is a new recognized event, whereas a source retraction removes participation on replay and retains the old identity.

The model publishes stable, atomic book-step IDs and a contiguous entity-scoped sequence for each complete snapshot. Steps carry their effective date/time, operation kind, event reference, and ordering evidence. One event can have multiple steps when its economic components occur at distinct points. A step is the indivisible unit of inventory replay, not necessarily a whole source transaction. Posting recognition dates remain separate and may differ between security and cash legs.

Ordering constraints come from effective times, source sequence, causality, or an explicit guarded ordering decision. Steps that touch the same inventory or reference state are ordered before booking; missing order that can change a result blocks that determination. Proven-commutative steps may use their stable IDs as serialization tie-breakers. Such a tie-breaker is recorded as commutative, not presented as source chronology. Cycles and conflicts fail. The published sequence may change after backfill; step IDs do not depend on their rank.

Independent readers replay the recorded allocations and reference changes in that sequence. They do not reapply corporate-action ratios, rematch lots, infer pending participation, or reset references themselves. Date-scoped reports apply their explicit recognition basis to the complete resolved stream; they do not rebook a truncated history. An inventory cut must be a valid prefix for its dependent steps, or use separately specified quantity projection rather than invent an alternative historical book state.

For reference-settled contracts, publish entry, reset, and close transitions. Daily final settlement resets the remaining reference; collateral-only variation does not. The opening reference remains at the entry transition. Partial resets that would leave different references create explicit successor branches, preserving origin. Contract-specific formulas and currencies remain declared; the sequence does not assume every payoff is linear.

## Consequences

Readers have a small deterministic replay operation, while all financial choices stay upstream. Explicit transitions cost more rows than a date-sorted posting list, but avoid both downstream booking and published daily snapshots. The simple sequential model matches the local build design.

## Alternatives considered

### Infer state from review flags or dates

Pending activity can have dates and human approval.

### Sort by transaction ID

Deterministic output is not economic order.

### Publish only opening and closing snapshots

This conceals the path needed for backfill, lineage, and intraperiod checks.

### Require consumers to reapply terms and booking policy

This creates a second owner of economic determinations.

## Related

- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Derivation placement](0007-derivation-placement.md)
