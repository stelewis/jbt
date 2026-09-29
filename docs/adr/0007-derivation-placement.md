---
id: 0007
title: "Derivation Placement: Pipeline, Reference Model, or Mart"
status: accepted
date: 2026-08-19
tags: [analysis, data-contracts]
supersedes: null
superseded_by: null
---

## Context

If every output independently books lots, the records disagree. If every useful query becomes a pipeline table, the published contract grows into a second owner of derived facts. Analysis also needs reusable definitions that are neither new evidence nor one person's reporting policy.

## Decision

### Apply the placement test in order

1. **Must the build decide it once for all sinks to describe the same economic record?** The pipeline owns it. Identity, merge, links, typed position movements, action transformations, and economic booking qualify.
2. **Is it a stable mechanical derivation from the published contract, with a reusable definition and a verification contract?** The analysis reference package owns it. Its parameters select scope or express an explicit common convention; they cannot silently supply an accounting or tax election.
3. **Does it answer a policy-dependent question?** An analysis mart owns it, together with the policy inputs and lineage.

The first answer wins. Booking still belongs in the pipeline when a declared method participates in its determination: otherwise sink choice could change the record. Being deterministic does not make a computation a source fact, and being derivable in principle does not remove the need to publish an authoritative match and its provenance.

Publish the necessary determination and lineage, not redundant aggregates with a second owner. Open inventory snapshots are sums of exact recorded inventory changes in book-step order; current contract references are folds over recorded reference transitions. Consumers do not choose transfer slices, divide source totals, select pool constituents, or apply action ratios again. Omitting snapshots does not authorize omitting those determinations.

### Reference definitions and verification

Reference models form a reviewed, tested, versioned dbt package maintained alongside `jbt`, not snippets each project copies. Their analysis-layer ownership does not require a separate repository, distribution, or release process. Staging models expose the compatible tabular contract; reusable intermediates implement the mechanical folds. Jurisdiction packages and personal marts depend on those definitions without changing the pipeline or making tax rules part of the reference layer.

| Reference definition | Required behavior and checks |
| --- | --- |
| Open economic inventory | Replay allocation deltas and reference transitions in published order; retain pool contribution lineage without fictitious constituent selection; agree with position-scoped or explicitly net assertions |
| Daily position series | Declare range, calendar, day-boundary and coverage semantics; fold movements without inventing evidence in uncovered periods; reconcile sampled endpoints to the inventory fold and available assertions |
| Posting-role projection | Admit only recognized events for financial measures; preserve repeated roles, currencies, source amounts, and economic-leg grain; prevent allocation joins from multiplying fees; reconcile quantities and monetary weights separately |

The date argument in “positions at date X” does not itself turn a mechanical inventory fold into an accounting election. Conversely, a dense daily series is not parameter-free merely because callers often use the same calendar. A reference definition specifies these choices instead of relying on an implicit global convention.

Every reference model ships verification alongside its implementation: accounting identities, allocation and cardinality checks, synthetic edge cases, and independent assertions where present. It states which checks are internal consistency and which compare independent evidence. A verified closing statement does not prove every intervening event was captured, and agreement with the pipeline does not independently prove the pipeline correct. Without an adequate verification contract, a useful query remains a mart rather than acquiring reference authority.

The package declares compatible contract versions and validates actual producer metadata before models run. Package versioning alone cannot detect an incompatible data schema; unsupported versions block analysis rather than being coerced. Separate distribution and independent releases are warranted only when consumer needs require them; the compatibility gate applies regardless of release topology.

### Economic basis, tax basis, and policy provenance

Economic basis records acquisition value, separately traceable costs, and action adjustments under the project's declared economic treatment. Source-stated amounts remain evidence; selected fee allocation, booking method, or action allocation remains an attributed policy decision. The determination carries both rather than labelling all of it “what the source said.”

Tax basis depends on jurisdiction, taxpayer, dates, and elections. A wash-sale rule can defer a loss and add it to a replacement lot's tax basis without changing its economic acquisition cost. A jurisdiction can require a different lot match or pool from the broker/economic record. Tax models preserve their own matching and adjustment lineage and explain their difference; they never write the result back into economic booking.

Holding-period classifications, wash-sale tests, foreign-investment-fund eligibility, filing groups, valuation dates, forward-fill horizons, and exchange-rate conventions therefore belong in explicit policy-bearing marts. A reusable jurisdiction package remains a mart-level owner even when widely shared. Reports never sum unlike currencies without declared conversion and date policies; stale prices are not repaired by an unbounded reference-model fill.

External-flow classification uses event kinds and posting roles, not boundary categories alone. Opening initialization, noncash `book_result`, and balancing rounding cannot masquerade as contributions or expenses. Fees and rounding remain explicit components, and notes carry no financial legs; a report cannot turn those distinctions into inferred cash movements.

The analysis boundary also owns membership, shared-account authority, correspondence, and reporting declarations under [ADR 0006](0006-multi-entity-consolidation.md). Their reusable mechanics may live in the package, but each project's choices remain versioned inputs with their own validation.

## Consequences

Sinks share one economic determination, reusable folds share one reviewed implementation, and policy-specific answers expose their assumptions. Cash, investment inventory, and contract lifecycles use these same ownership and verification boundaries.

The package introduces shared definitions and compatibility checks without requiring another release system. Its authority depends on tests with stated limits, not publication alone. Tax and personal analysis can evolve without enlarging the producer contract or rewriting economic history.

## Alternatives considered

### Compute every useful table in the pipeline

Rejected: redundant outputs broaden the conformance surface and invite analysis choices into the evidence-building layer.

### Publish no reference definitions

Rejected: repeated independent reimplementations of useful folds can disagree without any change to the source data.

### Put reference queries into the producer's data contract

Rejected: their implementation and releases would become producer obligations even though consumers can derive them from the authoritative events.

### Decide placement by convenience or by the presence of any parameter

Rejected: convenience continually broadens the pipeline, while a blanket parameter ban excludes ordinary date-scoped folds and disguises conventions in “parameter-free” queries.

## Related

- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Consolidation boundary](0006-multi-entity-consolidation.md)
