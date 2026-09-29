---
title: Delivery step 0 implementation
date_created: 2026-09-29
---

# Implementation Plan: Step 0 Contract Proof

Implement [delivery step 0](./delivery.md#0-establish-and-exercise-the-full-contract) as an executable contract-conformance suite. Begin with the real numeric and artifact boundary, establish strict values and identities, then carry independently calculated cross-domain histories through complete schemas, Parquet, an independent reader, and a validated Beancount projection. Freeze schema version 1 only when every required proof is executable.

The repository contains a package scaffold, quality tooling, and proposed contracts, but no financial pipeline or runtime dependencies. There is no legacy financial implementation to preserve. This plan adds reusable contract foundations and a bounded proof harness, not a cash-first architecture or a parallel prototype pipeline.

## Scope and ownership

The [target architecture](../design/target-architecture.md) owns pipeline behavior; [acceptance](../design/acceptance.md) owns required counterexamples; the [analysis boundary](../design/analysis-boundary.md) owns published semantics; [declarations](../design/declarations.md) owns closed payloads and registries. The [schema exercises](../design/schema-exercises.md) supply partial projections to complete, not valid fixture files to copy unchanged.

Step 0 must distinguish three claims:

| Claim | Evidence in this step | What it does not prove |
| --- | --- | --- |
| Representability | Complete domain records, declarations, expected rows, relational invariants, and independent reconstruction | A parser or booking processor produces those records |
| Boundary feasibility | Actual locked Parquet writer, DuckDB arithmetic/UDF calls, artifact verification, and Beancount loading | Durable acquisition, concurrent publication, or clean-machine restoration |
| Value and identity correctness | Executable bounded arithmetic, canonical encodings, identity derivation, membership validation, and guard checks | Automatic overlap matching, rule evaluation, or inventory selection |

Implement no acquisition service, CLI workflow, stage scheduler, cache, persistent identity database, importer breadth, general booking/action engine, dbt package, hledger sink, or jurisdictional model. Filesystem locking, crash durability, performance, and recovery execution remain in their delivery stages. Step 0 defines the recovery inventory and publication protocol and tests complete static artifacts; it must not claim that this proves atomic runtime publication.

### Implementation boundaries

Use one producer package with direct imports from owning modules and no re-export hub. The following are intended responsibilities, not a requirement to pre-create empty packages:

| Location | Responsibility and allowed dependencies |
| --- | --- |
| `src/jbt/domain/` | Immutable financial values, typed IDs and records, deterministic arithmetic/identity rules and their canonical value encoding, and explicit invariant failures; standard library only, no IO or sink types |
| `src/jbt/contracts/` | Strict wire decoding, versioned schemas and registries, relational validation, and artifact canonical serialization; depends inward on domain types and value encodings |
| `src/jbt/artifacts/` | Concrete Parquet encoding, snapshot assembly/verification, and the Beancount projection needed by the fixtures; receives validated records and explicit settings, never makes financial choices |
| `tests/jbt/` | Module-mirrored unit tests under the existing test-quality rules |
| `tests/integration/step_0/` | Complete synthetic fixture bundles, boundary tests, coverage index, and the independent consumer harness |

Keep machine-readable schema resources with their owning contract modules and include them in built distributions. Use JSON Schema 2020-12 for closed JSON shapes and one versioned table catalog for physical types, numeric groups, keys, foreign keys, nullability, ordering, and registry references. Cross-record financial invariants remain named executable checks, not an invented expression language. Select a maintained schema validator through the dependency review below rather than building a general validation framework.

The producer must not hand-maintain separate table definitions for its validator and Parquet writer. Both use the catalog; tests establish agreement with domain records. The independent reader interprets the shipped schema and rows using its own validation and arithmetic, not the producer's catalog loader, Python types, serializer, or replay helpers. Shared data specifications are the interface; shared executable producer logic is not.

## Execution sequence

Each work package ends with a reviewed, runnable result and its directly affected documentation. Keep commits atomic within a package; do not commit empty abstractions in anticipation of another package. Dependencies are `1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7`; package 3 establishes the fixture format and the smallest complete snapshot, then packages 4 and 5 extend that same corpus and reader rather than creating another harness.

### 1. Prove the risky external boundary first

**Entry:** reviewed numeric requirements from [ADR 0011](../adr/0011-bounded-exact-arithmetic.md), the current Python 3.14 environment, and [supply-chain standards](../developer/standards/supply-chain-security.md).

- Review PyArrow as the Parquet writer, DuckDB as the independent reader/query engine, and Beancount as the ledger validator. Record necessity, maintainership, licenses, transitive dependencies, native distribution support on development/CI platforms, and install/runtime network behavior before accepting them. Consider `jsonschema` for the structural validators in package 3. These are candidates, not already verified dependencies.
- Add only approved dependencies with `uv`, update `pyproject.toml` and `uv.lock` together, and run under the locked environment. Put consumer-only DuckDB and validation-only tools in development dependencies; a library used by shipped producer code belongs in runtime dependencies. Do not add pandas, dbt, or another orchestrator to run the probe.
- Write the smallest real table containing strings, dates, booleans, nullable numeric groups, lists, and a typed empty counterpart. Read it through DuckDB, checking exact logical types and values rather than inferred JSON or a writer-library readback.
- Prototype explicit connection-local exact-integer UDF registration with coefficient strings and integer scales. Pin and test argument/result types, null propagation, exception behavior, and repeated registration/connection isolation. No financial argument, intermediate, or result may pass through a float.
- Exercise checked SQL decimal addition, multiplication, and aggregation, including scale alignment and group-size bounds before execution. Prove that representable operands can still overflow an expression; reject the unsafe decimal path and deliberately choose the admitted integer path, never catch overflow and approximate.
- Exercise integer division, exact rational intermediates, all declared rounding modes, residual allocation, division by zero, and result-storage limits. Keep this consumer implementation independent of the producer implementation added next.
- Load a small explicit Beancount purchase/disposal projection with the installed library. Probe total 10 over three units and a partial reduction without silently changing its authoritative total. A concrete representational limit is a finding to resolve before building the larger sink, not permission to round upstream evidence.

**Exit:** real integrations execute offline after dependency provisioning, accepted/rejected numeric cases have exact assertions, and the Python/DuckDB result shape and Beancount capabilities are evidenced. Unavailable wheels, incompatible APIs, failed exactness, or rejected dependencies block dependent work. Revisit the relevant technology/representation decision with evidence; do not lower the exactness requirement.

Suggested commit: `test: prove exact numeric and artifact interoperability`.

### 2. Establish pure values, canonical encodings, and identity rules

**Entry:** the boundary profile is feasible; no durable schema has been frozen.

- Implement finite coefficient/scale values with separate source precision, bounded rational intermediates, dimensioned monetary components, explicit rounding policy, and deterministic final-slice residuals. Reject malformed or oversized inputs before expensive conversion/expansion. Never use a global decimal context or implicit rounding defaults.
- Implement date-only, offset, zone, and unzoned time forms with explicit precision. Preserve unknown/inherit/value leg distinctions. A required instant needs pinned timezone rules and an explicit gap/overlap decision; preserving an unresolved observation does not require inventing an instant.
- Specify canonical bytes for each hashed structure: domain tag, version, typed field order, UTF-8/NFC rules, integer/decimal encoding, nulls, list order and multiplicity, and line endings. Preserve source bytes and original lexical evidence separately. Commit literal known-answer byte/digest vectors, not expectations obtained from the serializer under test.
- Define strong entity-qualified identities for every referenced record and stable semantic child keys for legs, steps, origins, effects, allocations, and changes. Hash inputs exclude mutable values, display names, sequence indexes, authority, and producer versions. Specify declaration stable-key versus immutable-revision identity and guard projection digests without self-reference.
- Implement canonical-anchor derivation and validation of explicit membership, retirement, merge/split, and guarded rebinding under [ADR 0008](../adr/0008-canonical-economic-identity.md). Do not implement candidate matching: fixture decisions are explicit inputs. Validate missing/ambiguous anchors, contradictory memberships, retirement cycles, and changed guards.
- Use structured named failure variants carrying schema/case location, record references, and violated constraint. Keep diagnostics actionable without dumping source bytes, account identifiers, or entire financial payloads.

**Exit:** singleton A plus confirming B retains A's event/leg/root-lot IDs when B becomes authoritative. Reversed acquisition order, clean reconstruction, and incremental-history input permutations converge on the same final IDs. Missing membership fails; revised values invalidate guards without re-identifying the event; merges/splits require explicit survivor and reviewed rebinding. Identical purchases remain distinct and reacquisition in one scope differs from identical bytes in distinct scopes.

Suggested commit: `feat: establish exact values and canonical identity rules`.

### 3. Define strict schemas and one complete handoff

**Entry:** tested primitives and explicit identity recipes; the first fixture is a small opening/credit/debit cash record, not a restricted cash schema.

- Inventory every table, declaration variant, registry, evidence-custody record, artifact envelope, and manifest/descriptor field required by the design. Give each financial fact and determination exactly one owning grain and typed carrier. Close gaps before declaring a schema complete.
- Implement closed extraction/domain shapes for observations, decisions, determinations, and scoped findings. Distinguish unknown, known zero, and inapplicable values. Preserve unsupported-content diagnostics at extraction without certifying missing economic facts as absent.
- Implement strict declaration and build-configuration schemas, including status/sequence/coverage registries, rule assignability, corrections, source authority, render mappings, booking/fee policies, and structured contract terms. Validate reference arity/types, active versus historical revisions, effective intervals, journal order, and immutable revision digests.
- Implement the complete publication table catalog, including empty tables. Validate physical types, canonical numeric groups, closed variants, primary/composite keys, entity-qualified references, conditional fields, declared ordering, duplicate multiplicity, participation, and provenance completeness. A free-form payload or `extras` field cannot substitute for a missing type.
- Encode named checks for allocation/component conservation, action-to-application coverage, lot/pool lineage, recorded reference continuity, and scoped check applicability. These checks verify supplied determinations; they do not choose allocations or calculate missing financial decisions.
- Define complete recovery-set, manifest, and descriptor schemas: retained evidence/bindings, authored history, dependencies/assets/runtime identities, schema/registry digests, effective declarations, explicit `as_of`, capabilities, required checks, and typed findings. Resolve logical-content identity separately from execution and byte identity.
- Establish one fixture-bundle format: invented source bytes or authored evidence; bindings and declaration history; expected domain records; complete expected table rows/findings; independent arithmetic notes and reader answers. Generate only mechanical encodings, canonical IDs, and digests from readable aliases; never generate expected financial determinations with the implementation under test.
- Instantiate the cash fixture with opening 100, credit 20, debit 5, closing 115, full provenance, and every required table. A dropped debit must distinguish source agreement from model disagreement; compensating omissions must not be certified solely by a balance equation.

**Exit:** the first complete fixture validates, writes to Parquet, and is read in a separate consumer process. Mutation tests reject unknown fields/versions, omitted required-nullable fields, bad references, wrong grain, and invalid combinations. Schemas remain provisional: passing cash does not freeze them.

Suggested commit: `feat: define strict financial and publication contracts`.

### 4. Prove identity, participation, ordering, and reconciliation contracts

**Entry:** one complete fixture format and producer/consumer path.

Extend the corpus with the identity histories from package 2 and the evidence/status/order/assertion families below. Add independent reader checks alongside each fixture, not after the fixture expansion:

- Retain original descriptions/status tokens, pending-to-recognized correspondence, cancellation/retraction versus reversal, a failed transfer's recognized fee, and source-authority scope. Review flags never control financial participation.
- Supply evidenced ordering edges, stable step keys, and hand-authored determinations for both same-day split/buy orders. Validate cycles, missing decisive order, comparable source-sequence domains, and contiguous published sequence. Reader replay follows the recorded order and applies each step atomically; it does not topologically choose an economic order.
- Demonstrate distinct source/model/coverage findings, complete empty assertion sets, position versus account-net scope, civil dates, leg overrides, expected empty periods, explicit horizon/lag/closure, and not-evaluable checks. Scoped failure cannot be replaced by a generic unsupported-family result.
- Validate rule/correction shapes, typed assignment permissions, declaration revisions, provenance, and expected competing/winning decisions without implementing the general rule engine. Include stale correction and lot-selection guards and explicit supersession/retraction.

**Exit:** the independent reader preserves evidence and participation distinctions, rejects invalid order/assertion structures, and reproduces expected scoped views without inference. Every acceptance source/time/correction family has an indexed representative proof; processor-only behaviors are separately named later checks, not replacements for structural coverage.

Suggested commit: `test: prove evidence identity and participation handoffs`.

### 5. Prove the full cross-domain representation

**Entry:** complete foundational fixtures; numeric and identity rules already work.

Extend the same schemas and reader through this minimum coverage map. Each row denotes a fixture group, not permission to replace its distinct cases with one generic event. The executable coverage index maps every acceptance family and schema exercise to concrete case IDs, owning constraints, positive results, negative mutations, and the later processor obligation.

| Fixture group | Required discriminating evidence |
| --- | --- |
| Mixed brokerage relationships | One custody account holds shares, cash, debt, and separate collateral; fees are categories, not inventory. Gross +100/-40 and net 60 checks differ; +110/-50 must fail the gross checks despite the same net. |
| Acquisition/split/transfer/disposal/backfill | Under both acquisition-fee policies, 10 Q/principal 100/fee 2 split 2:1, transfer 8, dispose 5: remaining principal 75 and fee 1.50; match principal 25/fee 0.50. Correcting to 4:1 yields A 32/principal 80, B 3/principal 7.50, and matched principal 12.50/fee 0.25; stale selections fail. |
| Multi-origin and in-transit transfers | Two economic legs span allocations from distinct roots. Selecting 2 units at cost 10 and 4 at cost 20 transfers principal 100; 3 from each transfers 90. Departure/arrival dates retain the slice through a declared in-transit position. |
| Pools and denomination | Contributions 10/100 and 20/260, reduction 12/144, transfer 6/72 leave P 12/144 and P2 6/72. Preserve the contribution graph without invented constituent matches. A 50 EUR contribution converted at 2 USD/EUR retains both amounts, rate, and evidence. |
| Exact totals and component ownership | Total 10 over three units reduces by 3.33, 3.34, 3.33 under the declared policy. Multiple fee currencies, capitalization/expensing, zero versus unknown, source-total/price disagreement, proceeds, and residuals remain distinct without double expense. |
| Corporate actions | Fractional forward/reverse splits, delayed spinoff allocation, mixed merger, return of capital, reinvestment, post-disposal/backdated action, and redenomination versus alias change. Mixed consideration removes principal 100, retains 70, releases 30 against cash 40: result 10, with later settlement counted once. |
| Reference contracts | Entry 100, reset 103/settlement 30, close 105/settlement 20 with multiplier 10 yields settlement 50, not 80. Collateral 200 less withdrawal 40 remains 160. Contrast collateral-only variation; include partial resets, transfers, acquisition-fee attribution, and long-to-short reversal. |
| Options, obligations, and structured claims | Expiry, exercise/assignment and adjusted deliverables; short proceeds/borrow fees/substitute dividends/cover; margin and partial liquidation; perpetual funding/inverse settlement; ETN/ETF, structured note, convertible, retirement and failure claims. Source-evidenced conditional redemption is representable; absent path/event evidence blocks only the dependent determination. |
| On-chain and linked outcomes | Cross-asset network fees including failed attempts; wallet/wrap/unwrap/bridge; rebase, fork, airdrop with unstated basis; pool deposit/withdrawal; NFT and chain-specific identity; reorganization versus reversal; stablecoin settlement; complete-set outcome terms and N-ary strategy links. No jurisdictional characterization or invented zero basis. |
| Loans and authored adoption | Evidenced openings keep original acquisition facts without another purchase; remortgage distinguishes old/new relationships. Missing principal/interest allocation does not authorize an invented amortization schedule. |
| Quotes and downstream policy | Concurrent mark/index/trade and nonreciprocal FX quotes survive. Required valuation/FX inputs, perimeter, timing, ownership, fee treatment, and alternate tax matching are explicit consumer declarations; missing policy fails that question, not faithful upstream evidence. |
| Independent entities and invested handoff | Equal local IDs remain entity-qualified. Joint 110 counted once; 60/40 attribution yields 66/44; disagreeing 111 blocks consolidation. Internal transfer 20 is not external household flow. Authority changes map gross positions, lot/pool lineage, exact principal/fees, and current references, not merely equal market value. |

Use fully instantiated supported examples before adding their missing-fact variants. For an unknown basis, unknown reference, or missing policy, state precisely whether extraction can retain the observation, which determination is blocked, and which configured output cannot be certified. Do not demand a fabricated complete booked artifact, and do not substitute rejection for a supported positive case.

The consumer accepts only the pinned snapshot, schema, retained declarations, and explicit consumer inputs. It reconstructs units, components, contribution lineage, current references, scoped assertions, and question-neutral role projections. It must not read source bytes, import `jbt`, choose transfer slices, rematch inventory, apply action ratios again, infer participation/order, or calculate another settlement. Enforce isolation in a subprocess with producer imports denied; checking that no current import happens to use `jbt` is insufficient.

Keep downstream experiments small: demonstrate alternative matching and explicit valuation/performance assumptions against hand-calculated answers, including opening 100 plus a start-period contribution 50 and closing 165 giving 10%, not 65%. These are consumer contract proofs, not production reporting models or tax-law validation.

**Exit:** every intended family has its positive structural proof and independently calculated answer, compound histories replay without booking, and invalid combinations fail at the owning boundary. Any new required fact reopens the canonical schema and affected fixtures together; there are no compatibility aliases or parallel legacy shapes.

Suggested commits: `test: prove inventory and action handoffs`, then `test: prove contract and independent-consumer handoffs`.

### 6. Complete artifact integrity and sink conformance

**Entry:** representative records for all families and a genuinely independent reader.

- Assemble every fixture as a complete static snapshot with typed empty tables. Write non-build payloads and schemas, compute their checksums, serialize/hash the manifest, write `build.parquet`, then create the descriptor with its checksum. An externally pinned descriptor checksum starts verification; no digest depends on a payload containing that same build ID.
- Validate exact inventories and relative paths before IO: no missing, changed, extra schema-bearing, duplicate, mixed-generation, or escaping files. Do not resolve URLs, install DuckDB extensions, load arbitrary code from declarations, or fetch schemas during a test/build. Use temporary fixture directories and bounded inputs.
- Test byte integrity separately from semantics. For semantic negative tests, recompute mechanical checksums after deliberate row/schema mutations so an integrity error does not conceal a missing relational check.
- Compare complete typed logical content, including schema semantics, original scales, provenance, row order, duplicates, declarations, and findings. Different compression/row-group settings must preserve logical financial content while changing the appropriate execution/byte identities. Named input roles and repeated edges must distinguish execution fingerprints.
- Render the hand-built records directly from the shared domain, never via Parquet or a ledger-shaped intermediate. Validate supported Beancount outputs with its real loader, then independently compare inventory, weights, fee treatment, dates/clearing, and assertions to the expected record. Parsing successfully is not financial agreement.
- Cover mixed account/category paths, exact-total/partial-lot rendering, disposal gain, action lineage, trade/settlement separation, negative prices, symbol normalization/collisions, and beginning-of-day assertion translation. Publish explicit capabilities and a named limitation for unrepresentable configured projections. A sink limitation cannot alter the model or turn a required sink failure into success.
- Repeat fixture assembly from fresh directories under changed enumeration order, hash seed, and working directory; vary available locales explicitly. Compare text bytes and logical tables. Snapshot corruption or a failed required check must yield no conforming descriptor, without claiming that a test-directory assembly proves stage-1 durability.

**Exit:** real Parquet and Beancount boundaries preserve the supported fixture semantics; the reader rejects tampering, incompatible versions, and mixed snapshots; no required exactness question is concealed by a rounded projection or shared implementation.

Suggested commit: `feat: complete artifact and sink conformance`.

### 7. Enforce and review the schema-freeze gate

**Entry:** packages 1-6 pass without skipped or expected-failure proof obligations.

- Make the complete step-0 suite part of ordinary PR pytest execution, using the existing integration/golden markers, not the default-excluded `e2e` marker or a manual-only workflow. Adjust the existing CI job/dependency setup only where measured runtime or platform requirements demand it.
- Validate schema resource inclusion in the built wheel/sdist and exercise the installed producer, not only imports from the checkout. Tests remain offline after provisioning. Keep tooling and local workflow documentation aligned with any dependency or CI changes.
- Run targeted tests during each package, then the full repository quality gates and the required hooks. Require zero uncovered acceptance families/semantic fields, zero unresolved schema references, and positive and negative evidence for each closed variant and cross-record invariant.
- Review the coverage index against delivery 0a/0b/0c and its boundary-owner table. Separate checks that passed from later processor/runtime proofs; no successful hand-authored output is evidence of unimplemented calculation logic.
- Freeze the reviewed schema version and content digest only after all proof obligations pass. Record exact dependency versions, commands/results, covered families, and remaining later-stage responsibilities in the PR evidence. Correct the canonical design/ADR when evidence changes a decision; never preserve a disproved shape through a runtime shim.
- Document implemented boundaries and the conformance entrypoint in the developer guides. Retain design meaning still needed by later processors; retire this execution plan after its enduring contracts and fixture workflow have an owner.

**Exit:** delivery step 1 can implement a producer against a proven complete contract and reuse independently authored expectations without inventing new structural semantics to make cash pass.

Suggested commit: `test: enforce the step zero schema-freeze gate`.

## Decisions that block dependent work

Resolve these with the smallest fixture/probe, update the owning contract, and proceed only when the evidence is explicit:

| Decision | Resolving evidence | Blocks |
| --- | --- | --- |
| Native library support and exact UDF result/error shape | Package 1 on the locked development and CI environments, including explicit nulls, errors, overflow, and no float promotion | Numeric API and artifact adapters |
| Authoring syntax versus canonical retained JSON | Literal vectors for integers, `N` groups, required nulls, Unicode, revision/guard digests, manifest and descriptor bytes; examples currently show JSON integers while canonical manifest integers are strings | Schema validators and durable hashes; no permissive dual-shape decoder |
| Complete identity recipes | Same final retained inputs across acquisition histories, stable semantic child keys, merge/split and correction guard counterexamples | Durable IDs and fixture expansion |
| Schema vocabulary and semantic completeness | Every table/payload/manifest field has a type, owner, constraint, and complete example, including domain records not yet specified as machine schemas | Full corpus and version-1 freeze |
| Economic-order and unknown-component validation | Split/buy, pool-entry atomicity, null-basis, and partial-reference examples separate supplied decision validation from making a new decision | Independent replay and completeness claims |
| Beancount exact-total and complex-event fidelity | Real validated projections and semantic comparison; explicit supported/unsupported capability outcomes | Sink contract and any fixture requiring that projection |

## Verification

Follow the [contract-first workflow](../developer/standards/tests.md#contract-first-slices). Expected financial answers must remain independently reviewable; serialization convenience is not an oracle.

The numeric suite must assert both sides of each boundary: 96 versus 97 coefficient digits; scale/source scale 38 versus 39; intermediate digits 384 versus 385 and scale 152 versus 153; accepted `2^256 - 1`; `1e-38` versus `1e-39`; canonical zero versus forbidden negative zero; nullable groups versus partial nulls. Include the four-96-digit-factor product and a rejected fifth factor, safe/unsafe aggregation, positive/negative half ties, exact rational division, and storage rejection of a result whose intermediate was admitted.

Use one targeted runner for related changes, for example `uv run --locked pytest -q tests/jbt/domain tests/jbt/contracts tests/integration/step_0`, selecting only directories that have been implemented. The complete conformance path starts with the hand-authored expected extracts/domain records associated with retained synthetic evidence, then executes strict validation, real artifact writing, independent verification/replay, and real ledger validation. It does not claim to parse source bytes or invoke unbuilt processors.

At freeze, run:

```bash
uv run --locked ruff format --check
uv run --locked ruff check
uv check --locked
uv run --locked tq check
uv run --locked pytest -q
uv run --locked prek run -a
uv build --no-sources
```

Unavailable tools, unexecuted integrations, omitted families, and unresolved boundary failures are gate blockers, not passing evidence. Passing this gate establishes representation and interoperability only; each later processor must produce these expected results from its real inputs before claiming support.
