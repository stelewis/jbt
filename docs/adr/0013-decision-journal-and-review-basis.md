---
id: 0013
title: "Decision Journal and Pinned Review Basis"
status: accepted
date: 2026-10-09
tags: [decisions, corrections, review, durability]
supersedes: null
superseded_by: null
---

## Context

Reviewed decisions must survive source replacement, software reinterpretation, and rebuilds. Editing generated rows loses those decisions; storing corrections inside standing project declarations makes commands rewrite a different class of input. A build that stops at its first reviewable conflict also prevents the user from seeing the other decisions needed to obtain a complete record.

A failed build cannot publish partial financial tables without weakening the publication contract. It nevertheless needs a retained interpretation that the user can inspect and correct. A published generation is insufficient as the review basis when newly interpreted evidence blocks publication.

Guarding final output rows is also wrong: an assignment can change the very field whose old value authorized it. Applicability must be established before the decision takes effect, not inferred from its eventual output.

## Decision

### Separate standing policy from reviewed decisions

Standing declarations and configuration own accounts, bindings, rules, authority, rendering, and transit positions. One append-only decision journal owns decisions about particular records. Identity and statement revision execute in merge; assignment and split execute in enrich; transfers and evidence links execute in link. Interpreter qualification, inventory selection, and ordering introduce journal representations with their consuming processors rather than prebuilding a general review framework.

Each entry records a decision, supersedes a prior entry of the same variant, or retracts a prior entry. Changing variant requires retraction and a new record. Supersession and retraction retain the complete earlier entry; they do not edit it. Active decisions are derived from the retained entries, never from a mutable status database.

Recording a decision establishes its logical position from its first journal sequence. Supersession replaces the active revision at that original position; it does not move the decision behind its dependents. Retraction disables it. To change the position, retract it and record a new decision with a new key.

Journal entries use stable, entity-qualified declaration keys and immutable declaration revisions. They publish as declarations with journal custody, not a second identity namespace. An entry retains its reason and the attempt against which it was reviewed. That attempt is lineage, not an applicability condition.

### Commit a durable head over immutable entries

Entries form one ordered chain per financial root. Each carries its sequence and previous-entry digest. Append under the project-wide writer lock: write and synchronize the immutable entry, then atomically replace and synchronize the head reference naming its sequence and digest. Head replacement is the commit point.

Before that replacement, the old head remains authoritative. An entry beyond the head is an explicitly reported, uncommitted append and is never applied automatically. After replacement but before durability acknowledgement, failure is uncertain acknowledgement, not guaranteed rollback.

Input capture validates the entire selected chain and pins its exact head. The current head must extend every head retained by earlier attempts: the digest at each retained sequence must agree. A valid shorter chain together with a rolled-back head is therefore rejected rather than mistaken for a fresh journal. Missing or changed committed entries also fail capture.

Verification replays the baseline's pinned head, not the live head. Later entries cannot alter that historical replay. Financial roots retain the immutable entries needed by their captured selections; builds do not garbage-collect them.

### Evaluate guards at the decision boundary

Fold active decisions in first-record order within their owning stages, using each key's active revision. Identity and statement revision execute in merge before assignment and split in enrich. Transfers and evidence links belong in link, and qualification outside financial mutation.

A variant and target kind have a contract-defined guard profile. It covers the reviewed facts required by [ADR 0001](0001-correction-identity.md), plus the structural context on which the decision depends. The profile cannot be weakened by choosing an arbitrary subset of fields.

Evaluate the profile against the state immediately before its entry applies. Facts may come from upstream stages or earlier active entries in the same stage; they cannot depend on a later stage or the entry's own output. This permits a later decision to guard an earlier assignment's result without making either depend on final booking.

One evaluator returns the closed outcomes `applied`, `missing`, `ambiguous`, `inactive`, and `changed`. Each receipt retains its expected target, all matching pre-entry candidates, projection seals, and outcome. The target kind determines the profile. An unavailable field is not an invented null; an explicit typed unknown remains distinguishable from absence.

Superseding an earlier entry reconstructs the active fold and re-evaluates its dependents. For example, a split into child amounts 40 and 60 followed by a category assignment guarding the first child's amount 40 stays applicable when that split is superseded with identical values. Changing the split to 45 and 55 makes the dependent guard `changed`, not `missing`: the replacement split still precedes its child assignment. Two active assignments to one field are a conflict, not last-writer-wins. A failed guard blocks publication, never redirects to a retired successor or a similar record, and cannot be waived.

Publication validates receipts and their relationship to retained entries. It does not re-evaluate the guard against final rows. For example, a category assignment guarded against an unclassified leg records an unchanged pre-entry projection, then produces a classified leg; the changed final category does not invalidate its own receipt.

### Retain one review basis per attempt

Each captured build attempt has an immutable invocation witness naming its ordinal and deterministic input-selection digest, and a separately sealed review report registered for that witness. Attempts that cannot publish retain reports too. The report owns resolved and blocked record projections, findings, guard outcomes, and observations needed to explain rules. Malformed inputs and invariant failures remain explicit failures rather than success-shaped empty determinations.

Blocked projections live only in this report. They are not published financial rows, and their status does not enlarge the economic event-state enum. Only a complete determination set satisfying the publication gate becomes a generation. Its manifest pins the deterministic financial input selection, not the invocation witness or report seal. Identical-input attempts can therefore retain distinct reports without changing generation identity.

Review and decision commands require an explicit attempt pin. They resolve displayed IDs against that attempt's registered report and compute guards from the facts the user actually saw; they do not re-resolve current sources inside the command. Capture authenticates this report role, not merely a content-addressed document claiming the same attempt, and retains the attempt-to-report binding with the selected inputs. Historical replay authenticates from those captured references without consulting later live attempt metadata.

The report has one complete stage-tail inventory for new decisions. Target-specific receipts and sparse reverse changes reconstruct the complete pre-entry state at each existing decision's logical position; unrelated records and duplicate boundary inventories are not retained per decision. Supersession may deliberately retarget to another eligible event or leg in that reviewed pre-entry state while preserving kind, key, and logical position. A replacement never guards its own prior revision's output or facts introduced later. If a required reviewed fact is unavailable, append fails with a located diagnostic rather than manufacturing a guard.

Readers take no writer lock. Commands that append use the writer lock and report the new immutable entry ID. They never modify generated files or authored standing policy. The next build revalidates the entry against its captured inputs.

## Consequences

- A blocked interpretation remains inspectable without publishing an uncertified financial generation.
- Unchanged reviewed facts permit an entry to apply across attempts; changed sources or superseded prerequisites produce reviewable guard failures.
- Recovery needs the pinned journal head and its committed chain as financial inputs, alongside evidence and standing declarations.
- Independent readers can verify explicit membership, decisions, and receipts rather than inferring them from final field provenance.
- Review commands must expose unavailable facts and distinguish proposed correspondence from an authorized decision.

## Alternatives considered

### Corrections inside an editable project file

Commands would rewrite standing policy, and retraction or supersession could erase reviewed history. Journal custody separates those responsibilities.

### Check guards against final rows

An assignment can invalidate its own check, while a later entry can conceal a changed prerequisite. Pre-entry receipts preserve the actual applicability decision.

### Move a superseded decision to the latest append position

An unchanged split would move behind existing child assignments and make their targets appear missing. Logical position belongs to the decision key; the append sequence belongs to its immutable history.

### Treat a valid chain and head as sufficient

A truncated chain with a matching older head still looks valid. Retained-head extension proves that committed history has not been rolled back.

### Publish partial or uncertified generations for review

This conflates diagnostics with the financial table and baseline contracts. A separate immutable report retains reviewable interpretations while generations remain complete.

### Re-resolve inputs when recording a correction

The new guard could describe facts the user never reviewed. A pinned attempt makes the review basis explicit and reproducible.

## Related

- [Correction identity](0001-correction-identity.md)
- [Canonical economic identity](0008-canonical-economic-identity.md)
- [Local publication and checked replay](0012-retained-local-execution.md)
