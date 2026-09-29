# Target Architecture

`jbt` is a reproducible build system for financial records. It turns archived evidence and versioned human inputs into a reconciled, booked record, then renders standard ledgers and analytical tables from that record.

This document defines the complete pipeline: banking, investments, liabilities, contract lifecycle events, corrections, and the substrate for multi-entity and jurisdictional analysis. [Delivery](../plans/delivery.md) sequences implementation. All stages use this financial model.

## Product contract

The durable asset is the input set: original evidence, authored records, declarations, rules, and corrections. Derived artifacts are disposable. Users must be able to leave with ordinary source files, readable decisions, standard ledgers, and documented tables, without a running `jbt` service.

> Given the same retained input snapshot, schemas, and executable toolchain, a clean build reproduces text outputs byte for byte and tabular outputs as identical typed logical content.

The recovery set includes vault objects, acquisition records, authored input history, dependencies, external parser assets, and the required runtime. Digest verification detects corruption; independent backups and restore tests protect availability.

The pipeline is local, single-user, and offline during builds. It owns extraction, identity resolution, reconciliation, enrichment, linking, and economic inventory booking. Downstream analysis owns valuation, performance, tax calculations, and consolidated reporting. Bank connectivity, statutory period closing, and a general workflow service are outside the product.

The product sends no telemetry, crash uploads, financial data, update checks, or other automatic phone-home traffic. Any supported network fetch is explicitly user-initiated, outside the build, with its destination and purpose visible before execution; fetched evidence or assets must be retained before use. Local analysis, diagnostics, plugins, and suggestion tools obey the same boundary; optional integrations do not imply consent to upload data.

A lifetime of records favors a sequential build with content-based reuse over distributed infrastructure. Measure rebuild time and peak memory with a realistic synthetic corpus; optimize artifact reuse without changing financial semantics.

## Financial record model

Model the financial relationship and event before choosing its ledger representation. A bank balance is a claim on an institution; `Assets:Bank:Checking` is one way to display it. A category such as groceries describes a movement across the record's boundary rather than an inventory that holds money.

| Concept | Identity, grain, and responsibility |
| --- | --- |
| Entity | One natural or legal person per independently built financial record; a stable declared ID scopes local identifiers |
| Account | Relationship or custody container with lifecycle, institution/counterparty references, source bindings, and statement expectations; also supports self-custodied cash and wallets |
| Category | Stable income, expense, or equity classification for a boundary leg; no custody, inventory, or statement lifecycle |
| Instrument | Stable identity for a currency, security, token, or contract; source-scoped dated aliases, quote conventions, multiplier, and applicable terms |
| Position | A stably identified account/instrument relationship, distinguishing counterparties and collateral independently of instrument identity |
| Event | An identified transaction or lifecycle occurrence with resolved participation state, related legs, source observations, and economic ordering |
| Leg | One signed quantity effect in an instrument, with a semantic role, dates, monetary components, and provenance |
| Lot or pool | A lot preserves acquisition lineage and successor branches across positions; a declared pool retains contribution/reduction lineage without pretending to select individual constituents |
| Corporate action | An effective-dated transformation with parent/child instruments, affected lots, consideration, and allocation inputs |
| Assertion or observation | A source-stated balance, price, or supported metric with time, measurement, precision, and explicit position-level or account-net scope |
| Link | A typed relationship with named participants, including transfers, strategy chains, and evidence associations |

A leg changes a position or records a categorized boundary effect. Boundary effects distinguish external movements, opening inventory, and non-cash book results or rounding; only actual movements enter cash-flow analysis. Account paths, display symbols, and category trees label these records without identifying them. [Position semantics](../adr/0002-position-taxonomy.md) and [booking](../adr/0003-booking-and-lot-identity.md) define how the model treats ownership, obligations, costs, and reference values.

Keep three origins distinguishable throughout:

- **Observation:** what acquired or authored evidence states, including an institution's own calculations.
- **Decision:** a standing declaration/rule or a recorded choice about particular evidence.
- **Determination:** the result of applying those inputs, such as a matched lot, allocated fee, or balancing leg.

Every determined value can be traced to its inputs and policy. Corrected values never overwrite the observation they correct. Re-extraction can recover omitted detail from retained bytes, while downstream consumers receive all supported financial detail without reopening the archive.

## What a successful check establishes

A statement can provide independent evidence for a check:

```text
opening quantity + stated quantity changes = closing quantity
```

Apply this within an account, using the assertion's position selection, denomination, recognition basis, and source-defined interval. Gross claims against different counterparties are not interchangeable with their net account balance. Investments also require corporate-action quantity changes; market values do not obey this equation without valuation effects. A source with only a point balance cannot establish transaction completeness.

Reconciliation establishes agreement with supplied assertions within declared tolerances. Identity, coverage, classification, and completeness checks provide separate evidence: two compensating omissions can leave a balance unchanged.

Coverage is the union of qualifying statement intervals over a declared expectation window. The window ends at an explicit `as_of` input. Account opening, record adoption, statement cadence, publication lag, and closure are distinct declarations. Empty statements cover their intervals; rolling downloads supply only the scope they actually assert.

Keep three checks separate:

1. **Extraction fidelity:** each source's own movements agree with its own assertions.
2. **Model fidelity:** the resolved model agrees with the applicable assertions after merge, correction, linking, and booking.
3. **Coverage:** qualifying evidence covers the expected intervals, including periods with no activity.

A source-local pass cannot certify a model that later dropped a transaction. An authored correction may intentionally disagree with a source; record the discrepancy and its decision rather than rewriting the assertion to manufacture a pass.

## Inputs, outputs, and ownership

| Input | Owner and lifetime |
| --- | --- |
| Acquired evidence | Original bytes in an immutable content-addressed vault |
| Acquisition records | Retained accession facts and explicit source/account/importer bindings |
| Authored records | Versioned manual transactions, opening inventories, prices, notes, and imported legacy evidence |
| Declarations | Versioned identities, lifecycle, source conventions, tolerances, and standing policies |
| Rules | Versioned repeatable classification and enrichment policy |
| Corrections | Append-only record-targeted decisions, including explicit supersession or retraction |
| Build configuration | Enabled outputs, coverage horizon, schema versions, and pinned execution inputs |

A booking policy belongs in declarations; a decision about one disposal belongs in the journal. Rules classify repeatable patterns. All three are versioned inputs with explicit precedence.

Declarations specify account/instrument identity, opening inventories, civil-time and balance conventions, primary rendering dates, expected coverage, tolerances per account and instrument, booking method, fee allocation, and sink naming. Applicable policy changes carry effective dates. Shared declaration composition is defined in [project topology](./project-topology.md).

| Derived artifact | Responsibility |
| --- | --- |
| Extracts | Source-scoped observations, without household rules or booking |
| Model | Resolved identities, enriched movements, links, and supported booking results |
| Checks | Scoped findings with evidence, applicability, and severity |
| Outputs | Standard ledger files, typed tables, and a deterministic change summary |
| Build manifest | Exact input/execution fingerprints, artifact digests, and check results |

The vault is an input. Generated artifacts can be deleted and rebuilt; retained historical outputs remain useful audit references.

## Acquisition and preservation

Acquisition is a separate side-effecting operation, not a node executed during an offline rebuild. It copies bytes into the vault, verifies their digest, and durably records the accession before reporting success. Do not move or delete the inbox original automatically. An interrupted or repeated acquisition must not lose evidence or produce conflicting records.

A blob identifies bytes, not an account, statement, or acquisition occurrence. The same bytes may have several accessions or bindings. A binding carries a stable source namespace independent of its resolved model account and accession attempts. Repeated acquisition in one source scope does not create another source occurrence; identical bytes in genuinely different contexts need not collapse. A file containing several accounts is extracted once per bound importer; a reviewed source-account mapping resolves all its account sections. Do not select an importer by whichever plugin matches first.

Secrets may assist acquisition-time binding. Builds use the retained binding and require no keyring access. Importers process the original bytes, including any sensitive identifiers they contain.

Authoritative storage consists of whole-file immutable objects and retained acquisition records, with no storage-service dependency. A browsable account/date view is generated from them. Paths and display names never identify evidence. Changing a view cannot reorganize or rewrite the authoritative objects.

Vaults, extracts, model artifacts, and outputs are untracked by default. Private projects version authored inputs and the build manifest; their corrections, declarations, summaries, and references also require appropriate access controls. Committing financial output is an explicit private-project choice. Public fixtures contain synthetic data only.

Require every snapshot object locally and verify its bytes before reuse; a digest-shaped filename proves neither integrity nor origin. Tooling never garbage-collects irreplaceable inputs. Repair may reconstruct derived views or indexes from authoritative records, but must not replace the expected recovery inventory with files currently present and hide evidence of loss.

Unbound evidence such as receipts and policy documents can be archived without producing transactions; a typed evidence link associates it with records it supports.

## Extraction contract

An extract records what a source asserts, including its scope and precision. Sources can be wrong. Preserve source values separately from corrected values and derived results.

The extraction schema has closed, typed variants:

| Kind | Required distinctions |
| --- | --- |
| Transactions and legs | Source identifiers and locators; descriptions; status; dates; signed quantities; instrument references; original and converted monetary components; costs; broker lot labels; semantic roles |
| Balance assertions | Quantity or value; commodity; opening, closing, or point; temporal boundary; complete or partial holdings scope |
| Price observations | Instrument, quote commodity, observation time, price basis, stated rate and precision |
| Corporate actions | Effective event, affected instruments, stated ratios, consideration, and allocation evidence |
| Source scope | Accounts, period, source class, completeness claim, revision relationships, and institution |

Leg roles distinguish principal, commission, other fees, withholding, accrued interest, cash, and rounding. Keep gross income and withholding separate, and retain original amount, converted amount, and stated exchange rate independently: source rounding can make them disagree. Contract units remain contract units; the declared multiplier is applied in calculations rather than folded into extraction.

Reported basis, unrealized profit, and other supported source calculations retain their stated basis and provenance in typed observation records. Their values can be compared with independent determinations.

Missing, zero, and inapplicable values remain distinguishable. An airdrop without a stated cost does not acquire a zero cost by default. A transaction without a timestamp retains its stated date precision. Schema extensions add named facts and validation; arbitrary `extras` dictionaries cannot carry financial semantics.

Importers declare the source classes and event families they can extract. They report unsupported economic content with its source location. The archive accepts any evidence; a build requiring an unimplemented extractor or event processor fails explicitly.

Manual entries use an authored-source adapter and the same downstream contracts. A legacy ledger can be archived and imported as explicitly lower-fidelity evidence without pretending it came from a bank. Alternatively, adoption can begin at a declared opening inventory while the legacy ledger retains earlier history. Neither route invents provenance. A deferred `beangulp` compatibility adapter may reuse existing importers through this extraction boundary: it retains the original bytes and identifies the importer, but certifies only the facts that importer actually exposes. Missing source status, dates, assertion scope, lot detail, or discarded components stay unavailable; conformance cannot manufacture fidelity from rendered Beancount entries.

### Dates and ordering

Preserve authorized, traded, posted, settled, and value dates independently when stated. A date-only observation is not midnight. Preserve numeric offsets, named zones, and unzoned wall times as distinct representations.

Leg dates override transaction-level dates; otherwise the declared container supplies the value. Do not fill a genuinely absent date from another field or neighbouring record. Normalizing a redundant override must not erase where the source stated it.

Statement membership uses the source's stated date/balance convention in its civil time, not a universal UTC conversion. A date-only statement needs no invented instant. If a calculation requires an instant, require a resolvable offset or zone and pin the time-zone rules; unresolved daylight-saving gaps or overlaps block that calculation. Raw extraction may preserve an unresolved time.

Distinguish two orders:

- **Serialization order:** an arbitrary but stable total key is sufficient.
- **Economic order:** booking, intraday assertions, and dependent events require source sequence, causality, or an explicit decision.

A digest tiebreaker cannot decide whether a same-day acquisition preceded a disposal. Missing order may be harmless for commutative sums and fatal for booking; checks must distinguish those cases. [ADR 0009](../adr/0009-event-participation-and-replay.md) defines recognized participation, atomic book steps, evidenced precedence, and published replay order. The model emits those determinations; readers do not infer them from dates or review flags.

### Numbers and conservation

Financial quantities, amounts, prices, rates, and totals use exact decimal representations, never binary floats. Preserve source-stated scale separately from canonical numeric value. Preserve stated totals rather than replacing them with a rounded per-unit quotient.

Arithmetic has explicit precision, rounding, overflow, and residual allocation rules. Preserve a total of 10 over 3 units as total 10 even when its displayed per-unit cost is rounded. Acquisition consideration, consumed principal, carrying-cost components, gross proceeds, and monetary weights publish exact totals at their owning grains. Allocate residuals deterministically so allocated and remaining totals equal their input total. [The analytical contract](./analysis-boundary.md) defines exact storage and query arithmetic.

Reconciliation over units and transaction balancing over accounting weights are different checks. A split changes units without a cash transfer. An exchange has two commodities whose raw quantities cannot be summed. A disposal at a gain needs explicit balancing treatment; source legs alone need not form a complete ledger transaction.

The model derives balancing legs under declared policy, with provenance distinguishing them from source observations. Each monetary weight records its amount, instrument, and derivation basis; weights balance separately by instrument. For a purchase, the security's acquisition amount balances its cash consideration. For a disposal, matched book cost, proceeds, and an explicit book-result component balance together. A book-result component is an economic inventory determination; jurisdictional gain calculation applies its own matching, currency, and cost rules downstream.

An exchange retains both quantities and its stated conversion consideration. A split uses its quantity ratio and basis allocation rather than a cash-balancing equation. Fees remain separate source components with recorded allocations. These event-specific equations are exercised in [acceptance histories](./acceptance.md#cross-capability-histories).

Tolerance limits a check; it does not authorize modifying evidence. Report signed and accumulated residuals. Only an explicit rounding policy may emit a rounding movement, and an out-of-tolerance failure cannot be repaired by an automatic plug.

## Model resolution

The model is a deterministic computation over the complete supported input set:

```text
extracts + identity decisions -> merge
merge + rules + value decisions -> enrich
enrich + relationship decisions -> link
link + opening inventory + booking policy + lot decisions -> book
```

Extraction creates one artifact per source and bound importer. Merge, enrich, link, and book each consume the full relevant record set. Rendering creates one result per configured sink. Checks attach to the layer and scope they test; evidence views depend on the vault and extracts. The application owns this graph, and adding a bank or account adds instances without a user-defined graph language.

### Merge and correction identity

Three identities serve different purposes:

- Source occurrence: which observation in which retained source.
- Economic record: which event one or more observations describe.
- Content digest: whether a particular representation changed.

Do not make them interchangeable. Equal content does not prove two real-world purchases are one purchase. Changed description or importer formatting does not necessarily mean a new purchase.

Merge combines duplicate observations and retains their provenance. Link relates distinct events or legs, such as the two ends of a transfer. Similar amount/date is a candidate, not sufficient evidence for either operation.

Economic identity is resolved from source observations and identity decisions before enrichment. Each event has one retained canonical anchor under [ADR 0008](../adr/0008-canonical-economic-identity.md). Adding overlapping evidence requires explicit membership without replacing that anchor; source authority selects facts, not identity. Payee, category, and sink-naming changes cannot re-identify a transaction. Merges, splits, and anchor reinterpretations require reviewed survivor/rebinding decisions, never automatic redirects.

Source authority is declared over a particular account, period, fact kind, and balance basis. A statement may replace a rolling window only where its completeness and revision semantics justify that replacement. Preserve compatible details from supporting observations. Conflicting authoritative sources, ambiguous identity, and unresolved corrections block the affected model; never choose by filename, arrival order, or a blanket "monthly wins."

A reissued statement may legitimately retract an earlier item. A later reversal is a new event, not deletion of the original. Canonical event state is `pending`, `recognized`, `cancelled`, or `retracted`; only recognized financial events produce book steps or financial determinations. Recognized does not mean all legs have settled. Review labels cannot change participation. Source statuses and inactive observed legs remain inspectable. A paid fee on a failed transfer is a separate recognized effect linked to the attempt.

Corrections target transactions, legs, lots, actions, instruments, statements, links, or evidence bindings. Each carries guards describing the observation or decision context to which it applies. Re-extraction validates those guards even when the key still exists. Orphaned, ambiguous, or materially changed targets require review.

Journal entries are immutable. A later entry can supersede or retract one, with explicit target, reason, and unambiguous order. An accepted limitation remains visible as unresolved or excepted; it cannot turn an unmatched lot or corrupt artifact into a passing result. [ADR 0001](../adr/0001-correction-identity.md) owns correction identity; the lot-specific extension is [ADR 0004](../adr/0004-lot-selection-rebinding.md).

### Rules, links, and booking

Rules use one schema and evaluation engine. They match the merged observation view in declared order. A typed field registry defines which model fields are assignable and whether the first matching assignment wins or matching values accumulate; rules cannot change source observations or identity. Record-targeted corrections apply last. Splitting a classified movement produces explicitly identified child legs whose totals conserve the original movement.

Field-level provenance records which source, rule, or correction supplied the final value. An explanation replays the fold to show competing matches as well as the winner. Suggestions run outside the build and become inputs only after a human accepts a rule or correction.

Links carry a kind and named participant roles. They can contain more than two records: a strategy may include an opening, several rolls, and a close. Transfer, strategy, trade/settlement, and evidence links have distinct semantics. Prediction outcomes remain separate instruments joined by a declared complete-set relationship and redemption terms. Only a validated inventory-transfer relation moves lots without a disposal; strategy and complete-set membership alone change no balances.

Booking runs once over the complete supported inventory history, not over a filtered report. Opening inventories include their evidence and uncertainty. Reductions require enough units, a supported matching policy, and sufficient economic order. Preserve original acquisition identity and date through transfers, partial moves, and corporate actions.

Booking emits ordered steps, explicit inventory allocations with signed unit/component changes, reference transitions, monetary weights, and fee attributions. Postings remain economic legs, not one row per matched lot. A transfer of several origins records each source and destination slice; a pool reduction records aggregate consumption and contribution lineage rather than fictitious constituent matches. The fee-allocation method is versioned and recorded with its result; source allocations take precedence where explicitly stated. Original acquisitions, disposals, proceeds, fees, and broker allocations remain available for jurisdictional rematching. The [tabular inventory contract](./analysis-boundary.md#inventory-allocations-and-exact-components) owns their fields and conservation rules.

Fee allocation attributes an existing amount to lots; it creates no additional cash or expense movement. The declared book policy determines whether a fee is expensed when paid or included in book cost. A performance calculation may subtract an allocated acquisition fee from a matched trade's return, but it must not create another expense for a fee already posted. Tax capitalization and deductibility remain separate downstream decisions.

Instrument identity is separate from ticker, account path, and holding relationship. Aliases are source-scoped and time-bounded. Derivative symbology can generate identities only under a declared convention, including the contract specification or deliverable needed to avoid collisions. Position declarations identify actual custody/counterparty relationships; a relationship change uses an explicit movement rather than rewriting its history.

Keep instrument identity, account relationship, position direction, and measurement policy separate. A borrowed security obligation, its sale proceeds, and collateral are distinct positions. Reference-measured contracts publish entry/reset/close transitions and attribution of actual settlement cash. Daily final settlement resets the remaining reference; collateral does not. Closing uses the remaining reference without counting earlier settlements again. An issuer-stated lifecycle event can be processed from evidence; simulating unstated events from market paths belongs outside the pipeline.

Corporate actions apply to eligible inventory at their effective point. A later correction to the evidence causes a replay. Separate action terms from their booked applications; readers replay the recorded changes rather than reapplying ratios. Check each action's quantity, consideration, allocation, and rounding equations, retaining predecessor-specific branches and acquisition dates. Applied cash/basis totals survive even when per-unit terms are rounded. A shared action ID relates separately extracted cash consideration so it is applied once.

## Execution and publication

Execute the declared stage graph sequentially. Reuse an artifact only when its execution fingerprint matches and its stored bytes pass integrity verification. Extraction and modelling remain separate artifacts: a rule change reuses extracts, a source change re-extracts affected inputs, and a sink-only configuration change reuses the model. A coarse producer-code fingerprint can invalidate more work safely.

Every artifact envelope records its kind, schema version, producer identity, effective configuration digest, named input references, and payload. The build manifest inventories the complete generation and its checks. Runtime timings and progress messages stay in transient diagnostics rather than deterministic payloads.

An execution fingerprint includes:

```text
H(stage kind and schema,
  producer and dependency content identities,
  relevant configuration,
  named input edges with order and multiplicity)
```

A sorted bag of digests loses input roles and may lose multiplicity. A package version alone misses changed editable code or rebuilt distributions. Include selected plugins, serializer/writer versions, external models, and any runtime data that can affect results. Coarse toolchain invalidation is acceptable initially.

Separate three values:

| Value | Meaning |
| --- | --- |
| Execution fingerprint | Which computation and inputs were requested |
| Byte digest | Integrity of a stored file |
| Logical content digest | Equality of a typed result independent of its container encoding |

Parquet writer upgrades may change bytes without changing table contents. They still invalidate execution. Logical comparison must include schema, nulls, types, decimal semantics, declared row order, and duplicate multiplicity. Always retain byte digests for file integrity.

Canonical serialization specifies map ordering, number encoding, text normalization, encoding, and line endings. Normalize comparison keys deliberately; preserve original source text and never assume all visually similar Unicode strings are equal. Source timestamps are legitimate data. Volatile build timestamps, absolute machine paths, locale, environment state, and random values are not financial inputs.

Build into a private staging location. Validate artifacts, run required checks, and publish a complete generation only after success. Readers consume one generation through its manifest; they must never see a mixture of old and new tables. A failed build leaves the prior published generation unchanged and clearly reports that it is stale relative to the attempted inputs. Diagnostics from the failed attempt are not certified output.

Single-user does not mean simultaneous invocations are impossible. Publication and journal/acquisition writes need a narrow exclusive-writer mechanism or an explicit refusal when another writer is active. This is local integrity, not a distributed scheduler.

`verify` rebuilds without trusting cached derived artifacts and compares against a specified baseline without overwriting it. Repeating the same wrong computation is not independent financial validation, so golden expectations and source checks remain necessary.

Build and booking always use the complete declared input snapshot. Account/date filters select what reports or diagnostic checks show; they cannot select a partial model for publication.

## Quality and output contracts

Required checks cover source/model reconciliation, expected coverage, identity uniqueness, referential integrity, correction applicability, economic ordering, booking completeness, corporate-action continuity, schema conformance, and deterministic replay.

Every finding states the check, scope, observed and expected result, input references, and applicable policy. Distinguish pass, fail, and not evaluable. A missing price makes a valuation unavailable while leaving an otherwise valid quantity record usable.

Not every finding prevents every output. The configuration declares required checks and supported capabilities, and the manifest exposes their status. Structural corruption, ambiguous identity, and invalid arithmetic cannot be waived. Coverage gaps or uncategorized records may be retained visibly in a working record; a consumer requiring complete coverage or categorization must reject it. Do not add a "skip all checks" publication path.

Unused or fully shadowed rules produce warnings. Corrections that reproduce the rule-derived value produce informational findings while retaining the decision. Both checks use the rule-only result and field provenance, so they need no separate mutable review database.

Every sink consumes the same resolved model and may degrade only at its boundary. It cannot select new lots, infer a missing fact, or silently drop an unsupported event. Report its fidelity limitations.

| Output | Contract |
| --- | --- |
| Beancount | Ordinary supported directives, explicit amounts and lot choices, deterministic naming and order, source/correction references, no required `jbt` plugin |
| hledger (deferred) | Independent standard-ledger projection of the same booked model, with explicit capability/fidelity checks and no sink-specific rebooking |
| Tabular | Versioned Parquet tables preserving model semantics and provenance under the analytical contract |
| Summary | Deterministic monthly account/instrument quantities, category flows grouped by instrument, coverage, review status, and changes between builds |
| Evidence view | Generated human-readable links to immutable objects |

Opening positions are explicit declared movements, not automatic padding to make a later balance pass. Beancount closing assertions must be translated to its beginning-of-day convention. Intraday and complete-holdings assertions remain checked upstream when the format cannot express them.

A sink can fold over an ordered stream for representation, but authoritative inventory decisions are already made. IDs on rendered records make a correction traceable back to its input without editing generated files.

Directive ownership is fixed: declarations supply rendered account open/close and commodity directives; observations supply balances and prices; authored or extracted events supply transactions; evidence links supply documents; authored records supply notes. Position and category declarations map separately to ledger paths, so one brokerage account can render asset and liability holdings alongside a fee category without becoming several reconciliation accounts. A sink translates dates and names but creates no independent financial input. Reports that combine instruments into net worth apply explicit valuation and FX policy downstream.

## Software and extension boundaries

Use one Python package with enforced dependency direction:

- Pure domain types and calculations depend on no filesystem, environment, transport, or plugin loader.
- Importer contracts receive explicit source bytes and validated context and return typed observations.
- Model operations own merge, rules, links, and booking; they do not construct adapters.
- Sink contracts receive model views and rendering policy and return output descriptions.
- Application orchestration calls adapters, domain operations, checks, and publication.
- CLI commands parse input and present results; domain code never imports the CLI.

The output contract exposes model concepts directly. The tabular sink does not pass through a ledger-specific intermediate. Import-contract and determinism checks run with the modules they protect. PDF/OCR dependencies remain optional, with engine/model assets included in execution fingerprints.

Built-in and external importers share the same extraction contract and synthetic conformance tests. Python entry points in `jbt.importers` identify installed implementations; explicit source/account bindings select one unique entry point. Missing or duplicate bindings fail. A plugin receives bytes and validated context, returns typed observations, and has no mutable shared lifecycle with other plugins. Its package and dependencies are pinned in the project environment.

Plugins are reviewed executable code. Source files and declaration packs cannot name arbitrary import paths or download code. The importer authoring template and testkit exercise the same contract for public and private packages; public fixtures contain synthetic data.

## Operator workflow

| Task | Operation and durable effect |
| --- | --- |
| Start or adopt a record | `init` creates declared identities and an input layout; legacy adoption records its opening inventory and evidence |
| Preserve evidence | `acquire` copies and verifies bytes, then records their binding |
| Rebuild | `build` computes, checks, and publishes the complete generation |
| Inspect a change | The summary compares financial quantities and findings; `lineage`/`show` resolve IDs to evidence and decisions |
| Review or correct | `review` exposes pending decisions; `correct` accepts a rendered ID and appends a guarded journal entry |
| Find missing statements | `coverage` shows gaps and downloads due under the recorded horizon and cadence |
| Inspect investments | `trades` shows booked matches and strategy groups, preserving cost/proceeds components |
| Verify or diagnose | `verify` performs a clean replay; `doctor` checks input availability, plugin pins, and private-project storage posture |

Financial policy lives in versioned inputs. Display flags change presentation only. Optional suggestion tools propose inputs for review without participating in deterministic builds.

## Evolution and downstream boundary

Schema versions identify contracts. Publish machine-readable schemas alongside source so changes to fields, types, keys, and constraints are reviewable. Rebuild derived artifacts under the new schema and review intentional output changes against independently calculated expectations.

A change to a supported durable input schema uses an explicit conversion that preserves originals, records the mapping, and requires review for ambiguous bindings. Readers never silently upgrade inputs. Historical rebuilds use their retained input/toolchain snapshot; current builds use the current schema.

Backfill is not necessarily additive. An old acquisition, split, replacement statement, or corrected opening position can change later booking and every downstream output. The correct guarantee is unchanged source evidence and unaffected identities, with explainable changes to dependent results and explicit review where decision guards no longer hold.

Analysis consumes artifacts without importing internal Python types or reading another project's vault during a ledger build. [The analysis boundary](./analysis-boundary.md) defines its tables, arithmetic, declarations, and reference models.

One project records one natural or legal person and builds independently. Household and filing-group reports combine those records downstream. Joint accounts, beneficial attribution, and consolidation use explicit correspondence and authority. [Project topology](./project-topology.md) owns deployment and sharing.

Rebuilding restates affected history. Retain the complete input/toolchain snapshot and report used for a filing; statutory closing and amendment workflows operate downstream.

## Contract validation

Before fixing the first schema, use representative fixtures for every source/event family in [acceptance cases](./acceptance.md) and work the cross-capability histories through identity, inventory, checks, and outputs. Prove schema invariants and independent reconstruction of hand-calculated results, plus the actual numeric/artifact boundary. This establishes the shared contract, not correctness of unbuilt parsers or processors.

Before each processor slice, resolve its expected transformations and failure cases; completion requires the processor to produce them through the real boundaries. [Delivery](../plans/delivery.md) owns these gates. Reject hidden inputs, irreversible loss, sink-shaped identity, or generated results copied back as decisions. Extend a typed contract when new evidence requires it, keeping its rationale and expected behavior in the owning document.
