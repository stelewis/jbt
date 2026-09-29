---
id: 0001
title: "Correction Identity Across Re-extraction"
status: accepted
date: 2026-08-19
tags: [identity, corrections, durability]
supersedes: null
superseded_by: null
---

## Context

Extracts can be rebuilt; a person's corrections cannot. A change to an importer must not silently move an override to another record or make it disappear. File offsets in generated extracts are unstable, while a hash of all extracted fields changes when an importer improves its parsing.

An account, date, and amount are not enough to identify a source record. Two files can contain the same payment, two identical payments can occur in one file, and a source-supplied ID can be reused. A key that merely *usually* distinguishes records is not safe for corrections. Nor is a successful build evidence that an old correction still means the same thing.

## Decision

### Source anchors and reviewed facts

A source occurrence is anchored by committed bytes, retained source namespace, section, and stable locator. The locator must distinguish repeated records, including records with identical financial fields; an importer must not fabricate a unique source ID or use its own extract row order as evidence of source order. A source ordinal is scoped to the artifact and follows source order, not sorting introduced by an importer. Source account context and genuinely source-supplied identifiers can help disambiguate, but reused institution IDs are not globally unique. [ADR 0008](0008-canonical-economic-identity.md) defines the separate canonical economic identity and its survival when supporting evidence changes.

Neither a declared commodity or rendered account name nor normalized payee, description, memo, or category is part of the source anchor. Reclassifying an instrument, renaming a rendered account, or fixing merchant whitespace must not re-identify the evidence. Two exports containing the same event still have distinct evidence anchors; explicit identity decisions establish their correspondence and canonical anchor. Unresolved duplicate or overlapping financial events block the build rather than receiving invented identities.

Each record-targeted override in the append-only correction journal carries its target identity and a readable description of the original source facts the author reviewed. Before applying it, the build verifies both that the target resolves **exactly once** and that the expected facts still describe that target. A key hit with conflicting facts is as serious as a key miss: a key alone must never attach an old decision to a newly interpreted record. A description that happens to match another record is a diagnostic candidate, **not** permission to apply the override.

The reviewed facts include the source account, relevant date, signed amount and currency, source identifier where supplied, and any additional discriminants needed for that correction. A sign-convention fix can leave an artifact locator unchanged while reversing the record's meaning: this must fail the expected-facts check, not pass because the key survived. Conversely, an absolute-amount match may help the reviewer find a candidate but cannot authorize a transfer of judgement.

### Explicit rebinding and retained history

When parsing or source boundaries change, the build fails with the affected correction, old expected facts, interpreted target, candidate records, and whether the failure is missing, ambiguous, or conflicting. A review operation compares old and new interpretations over the affected source artifacts and importer versions. The human chooses whether to append a new binding, supersede the override, or retract it, producing a reviewable journal diff.

The operation does not rewrite or delete earlier journal entries or alter a binding during a build. Orphaned judgements remain available even if an importer bug is not repaired for years. Generated extracts and their old digests are rebuilt, not migrated. Schema/version changes make the rebuild explicit and scope review without depending on an accumulated first-seen binding database.

The journal owns record-targeted overrides, including link and lot-selection overrides. Standing declarations and rules own ongoing policy; they are versioned inputs, not entries in a universal journal of human decisions.

## Consequences

Identity and matching need tests for repeated identical records, reused source IDs, overlapping artifacts, parsing changes, and changed reviewed facts despite a key hit. Some importer changes require manual review even when a candidate looks obvious. That cost is preferable to attaching a correction to a different financial event.

A journal entry survives later changes as history, including supersession and retraction; only its active binding affects a build. Correction durability depends on committed evidence and reviewed journal entries, not on which builds happened to run first.

## Alternatives considered

### Keys from normalized extracted fields

Rejected: routine parsing changes orphan corrections, and a changed key can collide with another record.

### Automatic fallback to a match specification

Rejected: matching another record by similar facts silently transfers an irreplaceable human decision.

### Persisted first-seen surrogate IDs or generated-file positions

Rejected: these depend on build history or regenerate when the extract changes.

### Warn about an orphan and continue

Rejected: a successful build that silently reverts a reviewed financial decision defeats the purpose of preserving the journal.

## Related

- [Booking and lot identity](0003-booking-and-lot-identity.md)
- [Lot-selection rebinding](0004-lot-selection-rebinding.md)
