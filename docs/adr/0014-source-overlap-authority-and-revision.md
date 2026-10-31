---
id: 0014
title: "Source Overlap, Authority, and Revision"
status: accepted
date: 2026-10-10
tags: [identity, sources, authority, participation]
supersedes: null
superseded_by: null
---

## Context

Rolling downloads, complete statements, pending snapshots and balance points describe different evidence grains. Acquisition order cannot establish which financial facts are correct. A repeated scoped source key can confirm an event, while a genuine replacement changes its facts without necessarily changing its identity. Reusing a key for another purchase requires a different correspondence, not a latest-row policy.

An entire statement envelope is too broad a revision unit: replacing posted movements must not discard an independent balance basis or a later reversal. Reviewed decisions also need the exact financial facts and current membership of every operand, including observations that cannot yet resolve to one economic event.

## Decision

### Separate identity from financial selection

Default event identity uses the source namespace, section, key domain and key, plus event component. Keyless events use acquired occurrence anchors. Compatible observations confirm one event; distinct keys do not merge through similarity.

A reviewed key-family decision persists across future downloads. An occurrence decision moves only named acquired occurrences, allowing a reused key to split without undoing other family membership. Source-stated replacement supplies correspondence directly and preserves the original event identity. Removed identities remain retired, with no active source membership or booked effects. Corrections never follow redirects.

### Apply authority and revisions at financial scope

Authority selects one source namespace for an account, fact kind, registered basis and half-open period. Conflicting authority on one scope fails; parallel bases remain independent. Confirming authority selects supported facts. Complete replacement additionally requires live complete statement evidence and can eclipse weaker evidence only inside the dates that evidence actually covers.

Overlapping authoritative statements may confirm shared keys and contribute distinct keys without a revision. Disagreeing authoritative observations block. A source-stated or reviewed reissue replaces evidence only within its declared account/fact/basis/period. Replaced complete evidence cannot qualify as live authority. Events without live recognized support become retracted under their retained identities; a subsequent reversal remains a separate event.

Movement intervals, balance points and pending snapshots retain their separate meanings. OFX search bounds do not prove completeness. Balance guards include bound observation quantities, position, commodity and point semantics, not just envelope membership.

### Keep participation evidence-backed

Importer-declared status mappings establish source candidates. Recognized evidence can establish recognition; a pending item disappearing from another window remains pending. A reviewed participation assignment may cancel, but cannot manufacture recognition or substitute for source revision. Retracting cancellation restores source-derived participation.

Pending-to-posted proposals require unique compatible signed quantities and bound positions within a bounded date window. They remain suggestions until an explicit identity decision is recorded.

### Review before enrichment

Identity and statement-revision decisions execute in merge, before rules, assignments and splits. Original first-record order applies within each stage. Every structural guard seals all named source or statement operands, including financial facts and canonical membership or scoped disposition. Supersession and retraction re-evaluate dependent guards at their original positions.

Journal declaration provenance records reviewed identity and support changes. Native source evidence retains source provenance. Retired identities retain inspectable inactive event targets but cannot duplicate canonical source support.

## Consequences

- New downloads can confirm a family without growing the journal or regenerating retired IDs.
- Scoped reissues do not discard unrelated financial facts, and pending absence does not invent cancellation.
- Unresolved disagreement remains reviewable without permitting uncertified publication.
- Replay retains exact authority policy, source revision evidence, journal bodies and pre-entry bases.
- Importers must state completeness and revision semantics explicitly; format search bounds and arrival timestamps are insufficient.

## Alternatives considered

### Let the latest download win

Arrival order is custody, not financial authority, and makes replay depend on acquisition history rather than evidence.

### Replace whole envelopes

This discards parallel bases and out-of-scope movements, and can hide a genuine later reversal.

### Merge similar transactions automatically

Equal amounts and nearby dates cannot establish identity. Deterministic proposals reduce review work without making that decision.

### Keep historical support on retired published identities

One occurrence/component has one canonical membership. Duplicating it under a retired identity would violate occurrence accounting; retired rows remain inspectable without active support.

## Related

- [Canonical economic identity](0008-canonical-economic-identity.md)
- [Decision journal and pinned review basis](0013-decision-journal-and-review-basis.md)
- [Declaration and authority configuration](../design/declarations.md)
- [Reviewed decision bodies](../design/decision-bodies.md)
