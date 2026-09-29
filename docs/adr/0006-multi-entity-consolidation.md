---
id: 0006
title: "Multi-Entity Consolidation Boundary"
status: accepted
date: 2026-08-19
tags: [consolidation, privacy, data-contracts]
supersedes: null
superseded_by: null
---

## Context

Several independent financial-record projects may contribute to one analysis. Their rendered ledgers can discard source detail, their identifiers may collide, their schema versions can differ, and automatic directory discovery can admit another person's private records without review. A jointly titled account adds another problem: each owner may retain and build an independent copy, yet identical bytes neither guarantee identical interpretation nor prove two separately downloaded statements refer to different accounts.

## Decision

### Independent entities and distinct groupings

One project represents one natural or legal person and is independently buildable from its vault, declarations, and correction journal. Its declared `entity_id` identifies that person-scoped project boundary. Household membership and filing groups are downstream declarations, not reasons to merge independently owned histories.

Spouses and children have separate projects, preserving portability and avoiding retrospective reconstruction of ownership. Each entitled owner can retain evidence for a jointly titled account in their project; selecting a consolidation feed does not make its producer the sole owner. Account title, beneficial interests, and tax attribution remain distinct relationships. A custodial account does not make its custodian the beneficial owner.

| Boundary | Meaning |
| --- | --- |
| Reconciliation account | The institution's account, statement, balance, and coverage; it can be jointly titled |
| Project entity | One natural or legal person, with an independently buildable evidence/judgement boundary and declared namespace |
| Ownership or beneficial interest | Source-stated or reviewed rights, shares, and effective dates; not inferred from the producing project |
| Filing group | A jurisdiction's dated grouping or election, potentially including only part of a project's accounts or income |

`entity_id` is declared in project configuration, participates in its digest, and accompanies every emitted table row. It is not derived from a directory, repository remote, or filesystem path; renaming a folder cannot re-identify financial history. The identifier namespaces records but does not itself prove account title or determine tax attribution. Shared declaration packs can reuse instrument definitions, account correspondence, and entity-independent rules without sharing private corrections.

### Composite identity and explicit membership

Every transaction, posting, account, lot, action, and statement identifier and foreign key is scoped as `(entity_id, id)`. Conformance checks uniqueness and referential integrity on the composite. A consumer joining on a bare ID violates the contract; prefixing account strings alone leaves other identities colliding.

Consolidation reads selected tabular outputs, never assembled sink ledgers or another project's vault. Its version-controlled registry names each member's entity, output location, expected contract schema, and admitted feed scope. Undeclared directories contribute nothing; a declared missing member is an error. Every member's build metadata supplies producer and contract versions. Actual versions must match the registry and one compatible input contract; declaring a different expectation for each incompatible schema does not make them unionable.

Missing members, version skew, duplicate composite keys, broken references, and incompatible records block consolidation. No union-by-name coercion or partial successful result hides an incomplete upgrade. Admitting a person's records is a reviewable registry change, not directory discovery.

### Shared-account authority

For each shared account, reporting period, and basis, the registry designates **one authoritative feed**. Every entitled owner may retain and independently build their copies. Neither identical file bytes nor matching transaction keys are a deduplication rule: downloads can differ while describing the same event, and identical bytes can receive different interpretations.

Authority declares account correspondence, precise period bounds, basis and scope, and the producing feed. Coverage checks reject gaps, overlapping authorities, and incompatible basis within the report's declared coverage. A change of authority at a period boundary verifies continuity rather than counting an overlapping statement twice.

Here, **basis** identifies the comparable measurement and recognition view: for example, settled versus available cash, trade-date versus settlement-date positions, or economic cost versus source-reported tax basis. The registry names the view and its denomination where relevant; it does not infer compatibility from matching column names. These views are not ownership shares or filing elections. A report chooses its required view and cannot count the same movement twice merely because two views have separate authority entries.

Available comparable owner copies are checked against the authoritative feed. Unresolved financial conflicts block consolidated output; declared authority is not permission to ignore a different balance, quantity, cash movement, or basis. Presentation-only differences and genuinely incomparable scopes are identified as such, not treated as matching evidence. Resolving a financial conflict requires a reviewed explanation, corrected inputs, or a corrected comparison scope.

Selecting a feed does not assign its income to the feed-producing owner. Beneficial attribution, shared expense allocation, and tax or filing elections are separate dated declarations. An in-project shared-expense split produces postings; it is not a reason to combine projects.

### Durable cross-entity correspondence

Within-project links use only that project's evidence. Transfers and reimbursements across entities are analysis-layer correspondences over admitted feeds. A declared correspondence used for elimination carries both entity-qualified targets, reviewed expected facts for each side, quantities or amounts and currencies, and its intended classification.

Both sides must resolve exactly once and still satisfy the reviewed facts, even on a key hit. Missing, ambiguous, financially inconsistent, or changed sides block the affected analysis. Candidate matches help review but cannot automatically transfer a correspondence. Review is scoped using member producer versions and input changes; new bindings, supersessions, and retractions retain earlier judgement in analysis-owned versioned history.

The report declares whether it presents balances, gross flows, or net external flows. An internal transfer can cancel arithmetically in a net sum while still corrupting gross income and expense classification. Elimination identifies the corresponding internal components once; it does not erase currency conversion, fees, or genuine external income. Unresolved required correspondence cannot quietly disappear.

## Consequences

Each project remains buildable without other people's vaults. Combined reporting requires compatible contracts, reviewed membership, explicit authority, and durable correspondence; upgrades can block it until all admitted members conform.

The household audit trail spans member and analysis projects rather than one combined ledger. A registry does not confer or enforce consent: practical access remains a filesystem and operating-environment boundary. A child or separating household member can take their project without excavating a merged journal.

The pipeline needs no workspace, account-prefix, or cross-vault linking feature. Presentation-currency valuation and price coverage remain analysis concerns, not prerequisites for preserving separate native-currency evidence.

## Alternatives considered

### Generate a combined ledger or pipeline workspace

Rejected: it couples independent builds and consolidates a rendering that may have lost financial detail.

### Deduplicate joint records by bytes or key

Rejected: two downloads can differ while representing the same account, and the same bytes can receive different corrections. A source digest is provenance, not ownership or authority.

### Infer ownership or filing identity from project topology

Rejected: a person-scoped project does not prove that every represented account is solely titled to that person, determine beneficial shares, or establish filing status. Filing groups depend on jurisdiction, election, and year.

### Import member outputs into a household pipeline project

Rejected: generated tabular outputs are regenerable derivations, not acquired evidence for a new vault. This would create another private aggregate and couple independent projects merely to host a correspondence journal.

### Coerce schema versions or discover members by directory

Rejected: both permit plausible output from unreviewed or structurally incompatible inputs.

## Related

- [Correction identity](0001-correction-identity.md)
- [Derivation placement](0007-derivation-placement.md)
