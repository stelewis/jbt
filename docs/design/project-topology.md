# Projects and Sharing

`jbt` builds independent financial records. Shared declarations keep their conventions aligned; downstream analysis combines their published outputs. This page defines those deployment boundaries. [Target architecture](./target-architecture.md) defines the pipeline inside each record.

## Start with one independent record

Start with the `jbt` software repository and one private financial project per natural or legal person whose record is managed. Each project declares a stable `entity_id`, owns its inputs, and builds without reading another project's vault or calling its code.

Declare entity IDs independently of directory names, account paths, household roles, and tax filing status. Household and filing groups are downstream reporting scopes over these independently portable records. Account title, beneficial ownership, control, and tax attribution are explicit relationships that can differ.

A household analysis project consumes independently published records. Each record builds and remains usable on its own.

## Joint accounts and consolidation

Each owner may retain and reconcile a joint account independently. Neither receiving identical files nor using the same importer is required.

Consolidation needs an explicit registry:

- Identify the participating entity builds and their immutable published manifests.
- Map local accounts to a shared account identity where correspondence is known.
- Select one authoritative feed for each shared account, covered interval, and compatible reporting basis.
- Require non-overlapping authority intervals and sufficient opening/closing continuity when the feed changes.
- Compare overlapping copies where they are comparable; unresolved differences in quantities, identity, or basis block certified consolidation. Declared presentation differences are not automatically financial conflicts.

The selected feed contributes the shared account once. Other copies are evidence for comparison, not additional holdings. Source byte equality is useful provenance but is not economic identity: the same account can arrive as a PDF for one owner and OFX for another.

Beneficial attribution, tax ownership, membership in a reporting group, and elimination of internal flows are separately declared downstream. A report states which basis it uses. Consolidating jointly held balances into a household total counts the account once; attributing an individual's share applies the relevant ownership relationship.

An invested-account authority switch maps position relationships, lot branches or pools, and their acquisition/contribution lineage. The handoff must match quantities, exact principal and fee components, and current contract references at the boundary. Pool mapping does not invent constituent-lot selections. The consolidation check validates this opening/closing state before applying the new feed. Equal cash totals or net balances alone cannot establish continuity.

## Share declarations, not live state

Shared declarations can prevent repeated merchant rules or divergent instrument identities. Begin with local files. Introduce a shared pack only when a second project actually needs the same declarations.

A pack contains declaration files, vendored or pinned and present before a build. Project configuration lists declaration sources in explicit order. Each pack declares its schema version; loading validates it before composition. Builds never fetch packs, run pack code, or read a mutable external directory.

Use explicit namespaces and conflict rules:

| Kind | Composition rule |
| --- | --- |
| Shared instrument identities and aliases | Conflicting meanings fail; do not silently override identity |
| Rules and presentation conventions | Explicit ordered precedence, with local policy able to override shared policy |
| Account lifecycle, openings, ownership, and coverage | Project-specific inputs; not generic pack defaults |

All effective contents enter the build fingerprint. Matching pack names or version labels alone do not establish equality.

Independent projects need not rewrite their histories to adopt one registry. Cross-project correspondence belongs in the analysis registry when local identities differ, with explicit one-to-one or scoped mappings and ambiguity checks. A shared pack is a convenience for avoiding divergence, not a prerequisite for representing an inherited or independently managed record.

## Add artifacts when there is a consumer

| Artifact | Initial home | Trigger for separation |
| --- | --- | --- |
| Pipeline and built-in importers | `jbt` | Already the software product |
| External importer | Independently pinned package when distribution or privacy requires it | A reviewed adapter has consumers outside the core release |
| Ledger project | Private input repository plus protected evidence storage | A record requiring independent custody |
| Shared declarations | Local declarations | A second consumer needs the same data |
| Analysis models | Private analysis project | A concrete question cannot be answered by existing outputs |
| Reference models and declaration resolver | A reference package alongside `jbt` | Independent consumers need a separate release or dependency boundary |
| Jurisdiction models | Private marts | The jurisdiction-package release gate below |

The reference package owns canonical staging, question-neutral intermediates, declaration resolution, and their conformance tests. Private analysis projects own declaration values and personal marts. Jurisdiction packages own jurisdiction/year-specific rules, declaration schemas, and policy tests.

Keep public mechanisms free of private data. Public importers require synthetic conformance fixtures; a private importer follows the same interface without disclosing institution-specific private details.

### Jurisdiction-package release gate

Develop and validate jurisdiction/year-specific marts privately before extracting `pta_tax_<jurisdiction>`. Independent reusable release requires all of:

- Validation against identified authoritative requirements and representative cases for the advertised scope, including failures and taxpayer elections.
- One completed filing cycle whose calculations, declarations, reconciliation to filed outputs, and limitations have received suitable jurisdictional review. Submission or authority acceptance alone is not proof of correctness.
- A demonstrated second independent use with materially different inputs, establishing that the shared model is not merely one taxpayer's incidental layout. Another run of the same filing is not second use.
- Versioned policy and declaration schemas, synthetic public conformance cases, documented supported years and exclusions, and an identified review/maintenance owner.

These are release gates, not prerequisites for implementing private models or full-system schemas. The filing cycle tests end-to-end fitness; the second use justifies abstraction. Neither replaces authoritative rule validation. Until all pass, models remain private marts rather than a generally reusable jurisdiction package.

## Independence contracts

Entity/account identity, provenance, and the tabular contract are shared foundations. Adding a person, a pack, or an analysis project changes inputs and consumers rather than the pipeline's ownership boundaries.

A single record can publish Parquet without deploying an analysis project. Multiple records can remain independent without consolidation. The deployment and packaging triggers above are separate from the software sequence in [delivery](../plans/delivery.md).
