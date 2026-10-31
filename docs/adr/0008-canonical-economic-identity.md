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

Economic identity has one retained anchor, independent of which source supplies authoritative facts. An importer-declared source key defaults to an economic anchor of retained source namespace, source-account section, key domain, and source key. The key domain documents uniqueness within that section. An acquired occurrence anchor instead contains source blob identity, retained source namespace, source-account section, and format-qualified source locator. These anchors identify different things: repeated downloads can contain distinct acquired occurrences of one source-keyed event. Keyless events default to acquired occurrence anchors. An authored anchor is a stable authored-record ID, not its revision digest.

The namespace identifies a reviewed source binding context independently of model account names and accession attempts; the same bytes can have distinct genuine contexts, while repeated acquisition in one context does not multiply occurrences. Sections use stated account context, not response order or the resolved model account. A stable event key distinguishes economic components from the same anchor, such as a failed attempt and its paid fee. Canonical IDs are domain-separated SHA-256 hashes of the entity ID, record kind, anchor tuple, and event key under the versioned identity schema. Source-key anchor framing is `("source_key", source scope, section, key domain, key)`, distinct from acquired and authored framing. Descriptions, amounts, current dates, display names, and observation-set membership never enter the tuple.

Compatible observations with the same scoped key and event key support one event without a membership decision per download. Distinct keys remain distinct even when their facts are identical. Conflicting facts under one key require authority, revision evidence, or an occurrence decision; arrival order never resolves them. Keyless overlap requires a versioned decision naming the canonical anchor/key and admitted occurrence/key pairs, with reviewed discriminants.

Identity decisions override the default at two grains. A key-family decision merges whole scoped keys: merging B into A admits every present and future observation of B to A and keeps B's default ID retired. An occurrence decision assigns particular occurrence/key pairs of a reused source key to a separate event anchored at an acquired occurrence; it does not merge or redirect the whole key family. Source-stated replacement supplies evidence of key-family correspondence without a human membership decision. Importers may prove that correspondence, but do not otherwise choose a survivor among different anchors. Keyed/keyless correspondence, an unstated pending-to-posted key change, and correspondence across a source-section change require decisions. Adding or removing importer key support is an identity change requiring interpretation review.

A clean build derives these bindings from retained inputs, never first-seen state or newly created decisions. Adding compatible keyed support retains the default anchor; adding support to an occurrence-based decision extends its membership without changing its anchor. Fact authority may move without moving identity. Canonical anchors remain addressable in retained evidence even when support is eclipsed or replaced. A correction to an amount changes facts and guards, not the anchor.

| Identity change | Required decision |
| --- | --- |
| Add confirming evidence | Same scoped key joins its family without a new decision; otherwise extend reviewed occurrence membership; preserve the anchor |
| Merge two established events | Name the surviving anchor and retired ID explicitly; review corrections and downstream correspondences targeting either interpretation |
| Separate occurrences of a reused key | Assign the named occurrence/key pairs to an acquired anchor; leave the remaining key family intact |
| Split an incorrectly merged event | Assign each occurrence/key pair to exactly one resulting event; explicitly retain or retire the old anchor and review affected decisions |
| Reinterpret an anchor's locator or source section | Record a guarded rebinding; never search-and-apply by similar amount or description |
| Authoritative revision removes the event | Retain its identity and history as retracted; do not delete the event or reuse its ID |

Resolved support forms disjoint occurrence/key sets with one canonical anchor/key each. Member component keys identify source components and need not equal the retained canonical event key. One source record can support several distinct economic components, but the same component cannot be assigned twice. Published `event_support` records selected, confirming, eclipsed, and replaced support independently of field provenance; authored events have no invented acquired support. The [tabular contract](../design/analysis-boundary.md#evidence-declarations-and-relationships) owns these rows and their participation constraints. Retired-to-surviving references are acyclic and informational: readers and corrections do not silently follow them. Retired IDs remain addressable in the identity history; applying a correction still requires an active, unique, guard-valid target under [ADR 0001](0001-correction-identity.md). Builds fail on contradictory membership, missing anchors, cycles, or unreviewed affected decisions.

Leg IDs derive from the canonical event and a stable semantic leg key. A source discriminator distinguishes repeated roles; generated child keys derive from the parent and an explicit rule/correction output key. Neither serialization indexes nor matched-lot order identify legs. Lot roots derive from the canonical acquisition/opening leg and its stable origin key; successor branches additionally name the determining event and output role. Changes to these discriminants require explicit identity review.

Position and pool IDs are explicitly authored stable identifiers. Their published rows must name the same ID as their owning declaration payload. Renaming a declaration key or display label does not re-identify the holding or pool; changing the declared ID is an explicit identity change, not an automatic consequence of hashing revised content. Authors can refer to these IDs without precomputing generated model values. Event, leg, and lot identities retain their canonical derivation rules.

## Consequences

Identity can be reproduced from the final retained inputs without a first-seen database. Documented source keys avoid journal growth for each compatible rolling download; ambiguous correspondence and key reuse still require review. A legitimate merge or split can retire IDs, but the change is visible and cannot silently retarget human judgement.

## Alternatives considered

### Hash all members or choose their minimum

Adding evidence can re-identify an unchanged event.

### Persist generated first-seen IDs

This introduces hidden input and build-order dependence.

### Require membership decisions for every keyed download

This repeats an already evidenced correspondence, grows the journal with each rolling window, and turns review into rubber-stamping. Scoped key families retain identity without treating conflicting facts or reused keys as resolved.

### Treat source authority as identity authority

A better statement should improve facts without replacing acquisitions and corrections.

### Automatically redirect retired IDs

This hides a change to the object a person reviewed.

### Require authors to precompute position and pool hashes

This makes ordinary declarations depend on generated identifiers before their references can be authored. Explicit declared IDs provide the same stable referential boundary without coupling authoring to the identity encoder or treating declaration-key changes as holding changes.

## Related

- [Correction identity](0001-correction-identity.md)
- [Event participation and replay](0009-event-participation-and-replay.md)
