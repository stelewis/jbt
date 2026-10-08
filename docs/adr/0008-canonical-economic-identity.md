---
id: 0008
title: "Canonical Economic Identity from Retained Anchors"
status: accepted
date: 2026-09-28
tags: [identity, merge, corrections]
supersedes: null
superseded_by: null
---

## Context

A source occurrence identifies evidence, not the event it describes. Hashing all confirming observations changes an event's identity when another statement arrives. Selecting the smallest current anchor has the same defect; remembering the first generated ID makes clean reconstruction depend on build history.

## Decision

Economic identity has one retained anchor, independent of which source supplies authoritative facts. An acquired anchor is the source blob identity, retained source namespace, source-account section, and format-qualified source locator. The namespace identifies a reviewed source binding context independently of model account names and accession attempts; the same bytes can have distinct genuine contexts, while repeated acquisition in one context does not multiply occurrences. An authored anchor is a stable authored-record ID, not its revision digest. A stable event key distinguishes economic components from the same anchor, such as a failed attempt and its paid fee. Canonical IDs are domain-separated SHA-256 hashes of the entity ID, record kind, anchor tuple, and event key under the versioned identity schema. A source identifier alone is usable only inside its documented uniqueness scope. Descriptions, amounts, current dates, display names, and observation-set membership never enter the tuple.

A singleton event observation uses its own anchor and event key without a persisted generated binding. When several occurrences describe one event, a versioned identity decision names the canonical anchor/key and all admitted occurrence/key pairs, with reviewed discriminants. Importers may prove correspondence, but do not choose a survivor among different anchors. The review operation can propose a batch; acceptance records the decisions before the build. A clean build never creates identity decisions. Overlap without sufficient evidence or a required decision blocks resolution.

Adding support extends that decision while retaining its canonical anchor. Fact authority may move to another source without moving identity. The anchor remains addressable in retained evidence even if its occurrence is eclipsed or retracted as a source assertion. A correction to an amount changes facts and guards, not the anchor. Pending and posted observations can share an identity only through evidenced correspondence and the same decision process.

| Identity change | Required decision |
| --- | --- |
| Add confirming evidence | Extend the occurrence membership; preserve the anchor |
| Merge two established events | Name the surviving anchor and retired ID explicitly; review corrections and downstream correspondences targeting either interpretation |
| Split an incorrectly merged event | Assign each occurrence/key pair to exactly one resulting event; explicitly retain or retire the old anchor and review affected decisions |
| Reinterpret an anchor's locator or source section | Record a guarded rebinding; never search-and-apply by similar amount or description |
| Authoritative revision removes the event | Retain its identity and history as retracted; do not delete the event or reuse its ID |

Active identity decisions form disjoint occurrence/key sets with one canonical anchor/key each. One source record can support several distinct economic components, but the same component cannot be assigned twice. Retired-to-surviving references are acyclic and informational: readers and corrections do not silently follow them. Retired IDs remain addressable in the identity history; applying a correction still requires an active, unique, guard-valid target under [ADR 0001](0001-correction-identity.md). Builds fail on contradictory membership, missing anchors, cycles, or unreviewed affected decisions.

Leg IDs derive from the canonical event and a stable semantic leg key. A source discriminator distinguishes repeated roles; generated child keys derive from the parent and an explicit rule/correction output key. Neither serialization indexes nor matched-lot order identify legs. Lot roots derive from the canonical acquisition/opening leg and its stable origin key; successor branches additionally name the determining event and output role. Changes to these discriminants require explicit identity review.

Position and pool IDs are explicitly authored stable identifiers. Their published rows must name the same ID as their owning declaration payload. Renaming a declaration key or display label does not re-identify the holding or pool; changing the declared ID is an explicit identity change, not an automatic consequence of hashing revised content. Authors can refer to these IDs without precomputing generated model values. Event, leg, and lot identities retain their canonical derivation rules.

## Consequences

Identity can be reproduced from the final retained inputs without a first-seen database. Extra observations cost an explicit membership decision when they overlap an existing anchor; review tooling should batch that work, not weaken it. A legitimate merge or split can retire IDs, but the change is visible and cannot silently retarget human judgement.

## Alternatives considered

### Hash all members or choose their minimum

Adding evidence can re-identify an unchanged event.

### Persist generated first-seen IDs

This introduces hidden input and build-order dependence.

### Treat source authority as identity authority

A better statement should improve facts without replacing acquisitions and corrections.

### Automatically redirect retired IDs

This hides a change to the object a person reviewed.

### Require authors to precompute position and pool hashes

This makes ordinary declarations depend on generated identifiers before their references can be authored. Explicit declared IDs provide the same stable referential boundary without coupling authoring to the identity encoder or treating declaration-key changes as holding changes.

## Related

- [Correction identity](0001-correction-identity.md)
- [Event participation and replay](0009-event-participation-and-replay.md)
