---
title: jbt phased delivery
date_created: 2026-08-19
---

# Delivery Plan

Implement the full system defined by [target architecture](../design/target-architecture.md), [the analytical contract](../design/analysis-boundary.md), and the ADRs. Prove the shared representation against banking, investment, contract, and multi-entity histories before building the pipeline. Then implement and verify processors in vertical stages, keeping the record model and output contract consistent across all stages.

## Design now, implement in stages

Design shared identities, representations, boundaries, and invariants against the full intended capability set before freezing them. Staging implementation postpones processors, not the structural questions they depend on. It does not require building every future algorithm or inventing speculative features.

For example, a cash walking skeleton exercises the execution path. It cannot establish that a leg spanning two acquisition lots preserves transferred basis. Resolve that representation with exact expected rows and an independent reader before freezing it; implement automated lot selection in its later slice. Prove shared contracts broadly, then prove each processor before implementing its slice. This preserves cross-domain design validation without requiring every financial algorithm before the first pipeline.

Plans name entry evidence, deliverables, and exit checks. Follow the [contract-first testing workflow](../developer/standards/tests.md#contract-first-slices) to distinguish representability, real boundary feasibility, and processor correctness. An unresolved feasibility question blocks the dependent commitment rather than silently narrowing the product.

This plan owns implementation order. [Acceptance cases](../design/acceptance.md) owns the behavioral expectations.

## 0. Establish and exercise the full contract

The implemented [contract foundations](../developer/architecture.md) and [conformance workflow](../developer/tools/local-workflows.md#contract-conformance) exercise this gate. The [executable coverage index](../../tests/integration/step_0/fixtures/index.json) distinguishes completed structural proofs from the processors still assigned to later delivery stages.

The design decisions establish intended semantics; they do not constitute an implemented or frozen schema. Complete the checks below before fixing durable identity/persistence APIs or treating a successful cash example as evidence of full representability.

Step 0 proves contracts and boundary feasibility, not every future financial processor. Its deliverables are executable value/identity rules, machine-readable schemas, hand-calculated cross-domain fixtures, a minimal independent reader, and real numeric/artifact round trips. It does not require bank-parser breadth, automated lot selection, corporate-action processors, or jurisdictional models. Those must later produce the expected results through the same contracts; hand-authored outputs cannot certify their implementation.

Start with the small numeric interoperability probe in [0c](#0c-exercise-the-real-numeric-and-artifact-boundary), before expanding schemas across all event families. Then exercise value/identity rules and extend complete fixtures through the shared schema and reader. The subsections group proof obligations, not a waterfall that postpones an uncertain dependency boundary until every schema is written.

### 0a. Establish values, identities, and strict boundary shapes

Implement exact numeric values and bounded arithmetic, time/date forms, canonical serialization, strong IDs, and structured boundary errors under [code standards](../developer/standards/code.md). Add versioned machine-readable domain, declaration, manifest, and table schemas from the [declaration/registry contract](../design/declarations.md), including closed observation fields, source-status mappings, ordering constraints, contract terms, rule outputs, and booking methods. Schema versions cover semantic constraints as well as physical columns.

Start with the canonical-anchor example: singleton evidence, confirming statement, authority change, revision, merge, split, and guarded rebinding. Build the same final retained input set from different acquisition histories. IDs must agree; membership changes must not silently choose a new survivor. Do not implement a generated first-seen identity database.

### 0b. Prove the complete domain-to-publication handoff

For every source/event family in [acceptance](../design/acceptance.md), select small representative synthetic inputs and hand-calculate expected domain records, publication rows, and findings. Cover distinct structural demands and compound histories, not every processor branch before a pipeline exists. Representing an event means its facts and required relationships survive; calculating it means its equations and policy yield the expected result. Establish the expected calculation without requiring its production algorithm in step 0.

Use the compound histories and [schema handoff exercises](../design/schema-exercises.md), including mixed-account/category relationships, multi-origin and pooled transfers, nonterminating unit costs, split/backfill, mixed-consideration actions, gross versus net assertions, pending-to-recognized transitions, same-day precedence, structured terms, contract resets, outcome/strategy links, concurrent price bases, and invested-feed handoff. Instantiate complete rows, evidence, and declaration revisions from the displayed projections; validate cross-references and closed variants, not merely JSON syntax. An expected failure must name the missing fact or violated invariant; an unsupported-input rejection cannot stand in for the positive result of an intended supported capability.

Build a minimal independent reader of the expected artifacts. It must reconstruct units, exact principal/fee components, pool contribution lineage, current contract references, and scoped assertions using only the schema, rows, and retained declarations. It must not import domain booking code, consult source bytes, infer status/order, select transfer slices, reapply action ratios, or recompute policy allocations. This is a conformance harness, not an early full analysis product.

| Boundary and owner | Decisive step-0 check | Later runtime proof |
| --- | --- | --- |
| [Evidence custody](../design/target-architecture.md#acquisition-and-preservation) | Blob, source namespace, accession, and binding schemas distinguish repeated acquisition from distinct contexts; complete recovery-set inventory | Stage 1 interruption/retry and clean restore |
| [Extraction and values](../design/target-architecture.md#extraction-contract) | Exact totals/scales, dates, per-leg recognition, unsupported content, null/zero, and original observations round-trip without inference | Stages 1–2 source conformance |
| [Economic identity](../adr/0008-canonical-economic-identity.md) | Final-snapshot IDs are history-independent; confirming evidence preserves IDs; explicit merge/split/rebinding checks guards | Stage 2 overlap and correction workflow |
| [Participation and order](../adr/0009-event-participation-and-replay.md) | Inactive events have no financial determinations; required precedence has evidence; ambiguous order and cycles fail; reader uses published steps | Stages 2–4 event processors |
| [Relationship identities](../design/schema-exercises.md#one-brokerage-account-several-relationships) | One custody account spans asset/debt positions without a ledger-root type; categories cannot hold inventory; N-ary link roles and complete-set membership validate | Stages 2–4 linking and sinks |
| [Checks and coverage](../design/target-architecture.md#what-a-successful-check-establishes) | Separate source/model/coverage outcomes; position versus net scopes; complete empty sets; explicit `as_of` and not-evaluable results | Stages 1–2 dropped/compensating movements |
| [Inventory and components](../adr/0010-authoritative-inventory-allocations.md) | Independent replay distinguishes different transfer slices, pools versus origins, and principal versus fee treatment; exact totals conserve | Stage 3 booking |
| [Actions and contracts](../adr/0005-corporate-actions.md), [positions](../adr/0002-position-taxonomy.md) | Every action term maps to an application; branches, resets, settlement attribution, collateral, short cover, and direction reversal survive with exact expected outcomes | Stages 3–4 processors |
| [Publication](../design/analysis-boundary.md#build-metadata-and-evolution) | Typed empty tables, PK/FK and combination checks, acyclic digest construction, logical equality, and corrupted/mixed snapshot rejection | Stage 1 atomic filesystem publication |
| [Sinks](../design/target-architecture.md#quality-and-output-contracts) | Hand-built complex records agree in independently read tables and validated ledger projection, or expose a specific configured sink limitation | Every processor adds both sinks |
| [Reference analysis](../adr/0007-derivation-placement.md) | Mechanical folds and role projections preserve grain, participation, and exact totals; no hidden rebooking or fee multiplication | Stage 5 reusable reference package |
| [Consolidation](../adr/0006-multi-entity-consolidation.md) | Entity-qualified joins, incompatible/missing member rejection, shared authority, gross position comparison, pool/lot/reference handoff, and guarded flow correspondence | Stage 5 independent entity builds |
| [Policy analysis](../design/analysis-boundary.md#consolidation-and-consumer-declarations) | Representative alternate matching, valuation, FX, ownership, and performance examples identify required declarations; absence fails the question, not faithful upstream publication | Stages 5–6 models and jurisdiction review |

Every published financial field must have one owner and a meaning demonstrated by these examples. Every domain determination needed by a sink or consumer must have a typed carrier. If the reader needs an undocumented convention or another financial decision, repair the owning contract before proceeding.

### 0c. Exercise the real numeric and artifact boundary

Select and review the smallest dependencies needed for Parquet writing, an independent DuckDB reader, and Beancount validation; add them and their lock changes deliberately. The scaffold's installation and automation tests do not prove those integrations exist. Run the actual consumer arithmetic profile: checked decimal addition/multiplication/aggregation, exact-integer division, positive/negative rounding, residual allocation, and operand/intermediate overflow. Prove that source total 10 over three units survives without a finite unit-price approximation becoming authoritative.

Prototype the exact-integer UDF connection registration and result types before committing to that integration API. Test Python/DuckDB/Parquet interoperability and the [bounded profile](../adr/0011-bounded-exact-arithmetic.md), including the 96/38 storage and 384/152 intermediate boundaries, under the locked environment. Do not build a second orchestrator or full dbt package merely to run these checks. A failed feasibility test revisits the relevant arithmetic decision, not the exactness requirement.

### Schema-freeze gate

**Exit:** all step-0 checks have executable evidence, every acceptance family has a representative valid fixture and independently calculated expected result or genuinely required missing-input outcome, and the independent reader reconstructs the compound histories without booking. Unknown fields, unsupported versions, broken references, invalid combinations, missing decisive inputs, and incompatible snapshots have negative tests. No known structural question is deferred to the cash slice. This establishes representability and boundary feasibility, not implementation of the unbuilt processors.

Only then freeze schema version 1 for the first implementation stages. This is an intentional baseline, not a perpetual compatibility promise; later evidenced changes revise the contract and fixtures together. Keep performance, platform locking/durability experiments, additional parser implementations, and real jurisdictional validation in their owning later stages. Missing tool availability or blocked checks remain reported blockers to this gate, not successful validation.

### Gate for each processor slice

Before implementing a processor, extend the shared fixtures with its positive, negative, and boundary cases and resolve the expected transformations under the [contract-first workflow](../developer/standards/tests.md#contract-first-slices). A missing field, ambiguous policy, or impossible independent reconstruction blocks that slice and revisits the owning contract.

The slice exits only when its processor produces those results through the real model and configured outputs. Reuse the step-0 fixtures as end-to-end expectations; do not replace them with self-generated snapshots. Unimplemented families remain explicitly unsupported by that runtime even though their representation has passed the schema gate.

## 1. Build the reproducible execution path

Use the values and schemas proved in step 0. Implement versioned envelopes, retained input snapshots, and execution/content identities. Enforce dependency direction and guard against undeclared runtime inputs.

Add non-destructive acquisition, immutable objects, accession/binding records, and recoverable writes. Execute the fixed stage dependencies sequentially, using integrity-checked artifacts for content-based reuse. Add staged publication and uncached verification against an unchanged baseline.

Use one synthetic OFX cash statement to exercise acquisition through model, checks, Beancount, Parquet, and summary. Keep its records in the full schema established in step 0.

**Exit:** opening 100.00, credit 20.00, debit 5.00, and closing 115.00 agree in independently read outputs. Removing the debit after extraction fails model reconciliation. Identical retained inputs reproduce outputs; interrupted acquisition preserves evidence; failed publication preserves the previous complete generation.

## 2. Complete ingestion and the correction workflow

Implement overlapping-source authority, revisions, pending/posted states, multi-account files, account and instrument aliases, lifecycle, coverage, and authored records. Add exact decimal/time boundary cases and explicitly bound importers.

Add the rule engine, typed field policies, guarded correction journal, rendered-ID correction workflow, and replayable lineage. Implement internal cash-transfer and evidence links. Expose review status and uncategorized work in the summary.

Exercise a synthetic external importer in the locked environment. Validate unique entry-point resolution, conformance, and refusal of missing or changed executable dependencies before exposing that interface publicly.

**Exit:** repeated evidence does not duplicate events; distinct identical purchases remain distinct; revised sources and changed correction targets produce the required review. Source and model checks run independently. Coverage uses the recorded horizon. Users can fix an output through its ID without editing generated files.

## 3. Implement inventory and corporate actions

Implement the lot and position semantics already proved in the schemas: openings, acquisitions, reductions, declared matching methods, exact component allocations, multi-origin transfers, pools, and ordered replay. Keep economic legs distinct from their allocation rows.

Apply corporate actions at their effective point. Retain action parent/child relationships, consideration, allocation evidence, and acquisition lineage. Implement strict ambiguity and insufficient-inventory failures.

Extend executable coverage through the acquisition/split/transfer/disposal history, then mergers, spinoffs, return of capital, reinvestment, and pooling. Every new processor feeds the existing ledger and tabular sinks in the same change.

**Exit:** the independently calculated histories agree across source checks, booked inventory, fee/basis components, and outputs. A backdated correction changes dependent results correctly and revalidates guarded decisions. Economic matches and original evidence remain available for jurisdictional alternatives.

## 4. Complete contract and on-chain event families

Implement source-stated exercise, assignment, expiry, funding, settlement, and liquidation events using the position and contract terms already in the model. Include margin liabilities, collateral, short obligations, and settlement references without netting away their separate effects.

Implement the on-chain families in the acceptance inventory: separately paid network fees, failed transfers, wrapping/bridging, rebases, forks, pool-token changes, and evidence revisions. Preserve source-described instrument identity and keep jurisdictional characterization downstream.

**Exit:** contract settlement is counted once, collateral remains independently reconcilable, and lifecycle events conserve their declared components. All intended family cases have executable results. Calculations requiring unstated market paths remain explicit downstream requirements.

## 5. Build reference analysis and consolidation

Promote the step-0 reader checks into reference staging and deterministic intermediates against published Parquet snapshots. Add declaration resolution and tests at the same time as the first dependent model. Replay recorded allocations and reference changes; do not repeat upstream booking. Use the analytical contract's arithmetic, lineage, and snapshot rules without importing pipeline types.

Implement multi-entity correspondence, shared-account authority intervals, invested-feed continuity, ownership attribution, and internal-flow treatment. Add valuation, FX, and performance models with explicit policies and missing-data outcomes.

**Exit:** independently calculated single-entity and consolidated examples pass. Mixed snapshots, broken references, ambiguous authority, financial disagreements, and unavailable valuation inputs produce the defined failures. Separate entity records still build independently.

## 6. Add jurisdictional models and delivery breadth

Develop jurisdiction/year-specific models against authoritative requirements and representative private examples. Preserve alternative matching or pooling and record elections and other required declarations. Publish reusable jurisdiction packages only after the canonical [jurisdiction-package release gate](../design/project-topology.md#jurisdiction-package-release-gate); private development does not wait for package-release evidence.

Add further source formats, OCR, the deferred hledger sink and beangulp compatibility adapter, legacy-adoption conveniences, and suggestion tools through the existing boundaries. Their implementations follow demand; their inputs, determinism requirements, and fidelity limits are already part of the architecture. Optional integrations preserve the product-wide no-automatic-network contract.

**Exit:** each released capability passes its acceptance cases, and all public fixtures remain synthetic. Packaging changes preserve artifact contracts and independent records.

## Verification throughout

For every stage, run the relevant repository quality gates plus schema, independent-reader, ledger validation, and clean-replay tests. Expected financial results are calculated independently; updating a golden file requires explaining the semantic change.

Exercise restoration, corrupt/missing inputs, concurrent writers, interrupted publication, and producer changes throughout development. Before a release, restore the synthetic multi-year corpus using only its recovery set and measure rebuild time and peak memory. Performance improvements preserve the same financial results and complete-snapshot publication.

Supported durable input changes use explicit, reviewed conversion. Derived data is rebuilt. Historical reproduction uses the historical input/toolchain snapshot; current correctness uses the current contract and expected results.
