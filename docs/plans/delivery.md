---
title: jbt phased delivery
date_created: 2026-08-19
---

# Delivery Plan

Implement the full system, building on the [implemented architecture](../developer/architecture.md). The [target architecture](../design/target-architecture.md) and [analytical contract](../design/analysis-boundary.md) define the full system; [acceptance cases](../design/acceptance.md) define expected behavior.

The [deferred-scope register](../developer/deferred-scope.md) records unsupported capabilities and the evidence needed to reconsider them.

## Design now, implement in stages

Design shared identities and invariants against the intended capability set, then implement processors in slices. Prove the few shared distinctions that could invalidate the next consumer; implement detailed representations when that consumer needs them. Do not implement future review views, body variants, codecs, or exhaustive validation matrices in advance.

## Gate for each processor slice

Before implementing a processor, select small conformance examples covering its meaningful positive, negative, and boundary cases and resolve the expected transformations under the [contract-first workflow](../developer/standards/tests.md#contract-first-slices). Reuse existing evidence rather than multiplying equivalent scenarios. If evidence exposes a missing fact or flawed representation, revise the owning contract and independent expectations together rather than preserving it through a shim.

A slice exits when its processor produces those expected results, with representative integration proof through the affected boundaries. Test transformations directly; do not publish or replay every fixture merely to assert financial meaning. Reuse conformance fixtures as independently calculated expectations, not self-generated snapshots. Representation proof does not certify an unimplemented processor; unsupported families must fail explicitly.

## 2. Complete ingestion and the correction workflow

**Complete.** [Step 3: inventory and corporate actions](#3-implement-inventory-and-corporate-actions) is the next implementation increment; interpreter qualification remains the separate release-readiness gate below.

- Resolve overlapping-source authority, revisions, pending/posted states, multi-account files, bank and card statements, account and instrument aliases, account and event lifecycle, coverage, and authored records.
- Extend rules beyond category assignment, with complete evaluation and typed field policies. Add the guarded correction journal, rendered-ID correction workflow, and replayable lineage.
- Implement internal cash-transfer (including card payment) and evidence links. Extend summary review status and uncategorized-work reporting to these workflows.
- Add exact decimal/time boundary cases and bound each importer. Exercise a synthetic external importer in a provisioned environment; require unique entry-point resolution, conformance, supported installed interpreter provenance, and strict version-pinned replay.

**Exit:** repeated evidence does not duplicate events; distinct identical purchases remain distinct; revised sources and changed correction targets produce the required review. Source and model checks run independently. Coverage uses the recorded horizon. Users can fix an output through its ID without editing generated files.

### Interpreter qualification: separate release-readiness gate

Keep the importer interface experimental until upgrades can be qualified against a pinned generation. Implement this workflow after the ingestion model and correction workflow supply real comparison outputs, not as a prerequisite to Step 2. Compare the same retained financial inputs under the candidate interpreter set, report financial and review changes, and retain explicit acceptance. Missing or incomparable evidence is never an empty successful diff, and acceptance never waives financial invariants or missing recovery inputs. Provenance and strict replay establish what ran; they do not approve a reinterpretation of reviewed history.

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

Promote the independent reader checks into reference staging and deterministic intermediates against published Parquet snapshots. Add declaration resolution and tests at the same time as the first dependent model. Replay recorded allocations and reference changes; do not repeat upstream booking. Use the analytical contract's arithmetic, lineage, and snapshot rules without importing pipeline types.

Implement multi-entity correspondence, shared-account authority intervals, invested-feed continuity, ownership attribution, and internal-flow treatment. Add valuation, FX, and performance models with explicit policies and missing-data outcomes.

**Exit:** independently calculated single-entity and consolidated examples pass. Mixed snapshots, broken references, ambiguous authority, financial disagreements, and unavailable valuation inputs produce the defined failures. Separate entity records still build independently.

## 6. Add jurisdictional models and delivery breadth

Develop jurisdiction/year-specific models against authoritative requirements and representative private examples. Preserve alternative matching or pooling and record elections and other required declarations. Publish reusable jurisdiction packages only after the canonical [jurisdiction-package release gate](../design/project-topology.md#jurisdiction-package-release-gate); private development does not wait for package-release evidence.

Add further source formats, OCR, the deferred hledger sink and beangulp compatibility adapter, legacy-adoption conveniences, and suggestion tools through the existing boundaries. Their implementations follow demand; their inputs, determinism requirements, and fidelity limits are already part of the architecture. Optional integrations preserve the product-wide no-automatic-network contract.

**Exit:** each released capability passes its acceptance cases, and all public fixtures remain synthetic. Packaging changes preserve artifact contracts and independent records.

## Verification throughout

Exercise restoration, corrupt/missing inputs, concurrent writers, interrupted publication, and producer changes throughout development. Before a release, restore the synthetic multi-year corpus using only its recovery set and measure rebuild time and peak memory. Performance improvements preserve the same financial results and complete-snapshot publication.

Supported durable input changes use explicit, reviewed conversion; rebuild derived data. Keep current-correctness tests separate from historical replay, with independently calculated expectations for the current contract.
