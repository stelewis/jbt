---
title: jbt phased delivery
date_created: 2026-08-19
---

# Delivery Plan

Implement the full system defined by [target architecture](../design/target-architecture.md), [the analytical contract](../design/analysis-boundary.md), and the ADRs. Step 0 established the shared representation; the following stages implement and verify processors against it, revising the contract when evidence shows it is wrong.

## Design now, implement in stages

Design shared identities and invariants against the intended capability set, then implement processors in slices. A cash pipeline cannot prove multi-origin basis transfers; establish each slice's expected results independently before implementing it. Follow the [contract-first testing workflow](../developer/standards/tests.md#contract-first-slices): representability and boundary feasibility do not prove processor correctness. This plan owns implementation order; [acceptance cases](../design/acceptance.md) own expected behavior.

## 0. Establish and exercise the full contract

Step 0 established exact values and identities, versioned schemas, complete static artifacts, real numeric/Parquet/Beancount boundary checks, hand-calculated cross-domain fixtures, and independent replay. See the [implemented contract foundations](../developer/architecture.md), [conformance workflow](../developer/tools/local-workflows.md#contract-conformance), and [coverage index](../../tests/integration/step_0/fixtures/index.json) for evidence and later processor ownership.

These hand-authored results prove representability and boundary feasibility, not extraction, booking, action processing, or durable publication. Schema version 1 is a working baseline, not a compatibility promise or an approval mechanism for the ultimate design: when a processor exposes a missing fact or flawed representation, revise the owning contract and independent expectations together instead of preserving the seed through a shim. Do not retain a bad shape for inputs that are not supported as durable external contracts.  A snapshot digest identifies its contents; it does not certify that the design is right.

## Gate for each processor slice

Before implementing a processor, extend the shared fixtures with its positive, negative, and boundary cases and resolve the expected transformations under the [contract-first workflow](../developer/standards/tests.md#contract-first-slices). A missing field, ambiguous policy, or impossible independent reconstruction blocks that slice until the owning contract is repaired, not until a workaround preserves the step-0 baseline.

The slice exits only when its processor produces those results through the real model and configured outputs. Reuse the step-0 fixtures as end-to-end expectations; do not replace them with self-generated snapshots. Unimplemented families remain explicitly unsupported by that runtime even though their representation passed step 0.

## 1. Build the reproducible execution path

Start from the values and schemas exercised in step 0. Implement versioned envelopes, retained input snapshots, and execution/content identities. Enforce dependency direction and guard against undeclared runtime inputs.

Add non-destructive acquisition, immutable objects, accession/binding records, and recoverable writes. Execute the fixed stage dependencies sequentially, using integrity-checked artifacts for content-based reuse. Add staged publication and uncached verification against an unchanged baseline.

Use one synthetic OFX cash statement to exercise acquisition through model, checks, Beancount, Parquet, and summary. Keep its records in the shared schema, not a cash-only shape.

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
