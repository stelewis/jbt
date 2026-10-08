# Architecture

`jbt` implements local financial builds and checked release replay for one explicitly bound, quantity-only OFX cash profile. Acquired evidence and authored declarations produce independently checked shared tables, a validated Beancount projection, and a deterministic summary. Broader financial families have contract and conformance fixtures, not booking processors.

The [target architecture](../design/target-architecture.md) describes the intended pipeline. [Delivery](../plans/delivery.md) distinguishes contract proof, boundary feasibility, and later processor proof.

## Dependency direction

| Boundary | Owns | Dependencies |
| --- | --- | --- |
| `jbt.domain` | Bounded exact arithmetic, explicit money states, civil time, stable identities, canonical encodings, correction guards, cash determinations and independent checks | Python standard library; no IO, environment, plugin loader, or sink |
| `jbt.contracts` | Versioned table catalog, closed JSON schemas, declaration and cross-record validation | Domain values and jsonschema; packaged schema data |
| `jbt.artifacts` | Explicit Parquet types, snapshot assembly and pinned reading, integrity, execution envelopes, logical content digests, scoped Beancount projection | Domain and contracts; PyArrow at the file boundary |
| `jbt.importers` | Bounded OFX bytes to source observations and explicit unsupported-input failures | Shared extraction contract; no acquisition, classification, or booking |
| `jbt.storage` | Writer exclusion, immutable evidence and input records, durable generation installation and current-reference replacement | Local filesystem operations and artifact integrity |
| `jbt.runtime` and CLI | Observe execution facts, capture financial inputs, execute the fixed graph, control cache and output policy, publish and verify uncached | Explicitly selected storage, importer, domain, and artifact boundaries |
| Independent conformance consumer | Read pinned files, validate/replay published determinations, exercise consumer policy | DuckDB and independently implemented arithmetic; no `jbt` imports |

Import exact owning modules. Package initializers do not load native adapters or re-export their APIs. The consumer shares the published specification, not the producer's validator, serializer, arithmetic, or financial choices.

## Exactness and identity

Finite financial values retain a canonical coefficient, nonnegative scale, and separately stated source precision. Arithmetic admits bounded intermediates before calculation, retains rational values until an explicit rounding point, and reports unavailable components separately from known zero and inapplicable values. The [numeric contract](../design/analysis-boundary.md#exact-decimals-and-query-arithmetic) owns capacity and rounding semantics.

Economic identity uses domain-separated canonical anchors and stable child keys. Source authority, changed values, display labels, and replay sequence do not choose identities. Explicit membership and reviewed merge/split transitions are inputs; the foundations do not discover duplicates or silently follow retired identities when applying corrections.

Generated identity framing and wire JSON serve different purposes. Declaration revision digests cover canonical typed JSON; row/guard and manifest encodings preserve typed meaning while rendering integer fields as canonical decimal strings. Schema JSON retains native integer constraints. Callers select the encoding explicitly; decoders do not accept alternate encodings as compatibility conveniences.

## Installed execution and retained financial inputs

Acquisition is outside the financial graph. Under one project-wide writer lock it preserves an immutable intent, copies and checks the original evidence, installs a content-addressed object, and commits its accession context. A retry key identifies the request, not an economic occurrence. Repeated accessions can share bytes without losing their distinct acquisition facts.

Users provision a published release with ordinary `uv tool install 'jbt==<release>'`. Standard installed distribution metadata identifies the production package name/version under the immutable, once-only [release policy](./standards/releases.md). The immutable release tag retains its source-commit provenance; release automation builds ordinary distributions, tests them, and publishes the same bytes without injecting a custom stamp. Index and archive installs trust their provisioning source, not runtime authentication. Directory and VCS source installs are development, including non-editable installs. No user Git checkout, cleanliness check, installation recipe, downstream lock, or wheel-hash proof is required. Python 3.14 or later is supported without a managed-runtime layout requirement.

The application observes producer identity, Python implementation/version, platform/architecture, and the names and reported versions of every installed distribution visible to metadata discovery. Names follow standard package-name normalization; records sort canonically, and missing metadata or duplicate normalized names fail. Discovery does not import discovered packages, load plugins, hash source or installed trees, invoke a package manager, or access the network.

Application processing denies supported Python socket and DNS operations before financial reads and heavy processing imports. Recorded attempts prevent publication authorization and successful verification even when a component catches the denial. This is not a native-code sandbox. External `uv` provisioning may fetch packages, Python, or advisory data before application startup; `jbt` itself never installs or repairs its environment.

The application holds writer exclusion from input capture through publication. Its retained selection names every expected accession, source object, authored project digest, observed execution record, and explicit summary comparison pin. Missing retained financial inputs fail even with a warm cache. Financial roots contain no copied interpreter, dependency tree, source tree, or development lock. The captured project uses the shared declaration language and explicit source registries; the builtin field registry comes from the installed contract implementation rather than an author-supplied copy.

The fixed sequential graph has substantive stages: extraction, source checks, model determinations and model checks, enabled ledger and summary outputs, and complete artifact assembly. Release stage identity includes all observed execution facts, schema, relevant configuration, and named ordered input edges. Even an unrelated visible distribution changes cache identity. Validate cache envelope claims and bounded payload bytes before reuse, report corruption before recomputing, and reject unsafe paths. Development producers, including clean editable checkouts, perform no persistent cache reads or writes and do not hash mutable source. Financial checks are not configuration-supplied success flags.

UTC source semantics, locale-independent financial parsing/formatting, and canonical ordering keep ambient timezone, locale, and hash iteration out of financial meaning and cache identity. An installation must stay quiescent during a command; developers must likewise avoid concurrent source edits. Same-version installed-file tampering is outside this trusted-provisioning contract.

The cash proof uses independently stated UTC boundary points, an authored prior-day initialization, and explicitly classified external movements. Source reconciliation never reads booked quantities. Model reconciliation and occurrence accounting detect an omitted debit even if each surviving transaction remains balanced. OFX search bounds do not establish complete source coverage; the supported working record retains that limitation as `not_evaluable`. The [source profile](./ofx-profile.md) owns the temporal evidence and rejection rules.

## Artifact assembly and publication

`assemble_snapshot` receives the complete non-build table set, explicit entity and execution facts, retained interpretation resources, opaque output payloads, and writer settings. Assembly and pinned reading enforce the same contract: schema-typed JSON is canonical, retained artifact logical digests are checked, and published stage envelopes resolve to actual payloads with matching schema and digests and unique input ordinals. Payload roles come from the manifest, not filename extensions. Assembly validates the complete candidate and writes into a caller-owned empty directory:

1. Serialize the schema resources and write non-build Parquet tables, including typed empty tables.
2. Record byte and logical table digests, retained inputs, configuration, checks, and execution settings in the manifest.
3. Hash the manifest and write `build.parquet` with that digest.
4. Write the descriptor last, covering all payload bytes without a checksum cycle.

Storage validates and synchronizes the complete candidate, installs its immutable generation without replacing an existing one, and atomically replaces a small current reference. It acknowledges durability only after synchronizing that reference's directory. Readers pin a descriptor once and never mix files from different generations.

A failure before current-reference replacement leaves the old generation selected. A failure after replacement but before durability acknowledgement is explicitly uncertain acknowledgement; the selected generation is still complete. Fault injection establishes these application boundaries, not arbitrary hardware power-loss guarantees. Source objects, prior generations, and failed-attempt input selections are not garbage-collected.

Financial-content identity includes semantic schemas, effective declarations, interpretation registries, bindings, retained derivation semantics, findings, and typed financial rows. Rendering settings, physical encoding, recovery locations, and summary comparison context are execution concerns. Byte integrity is not semantic validity: the independent conformance reader also exercises altered rows with recomputed mechanical checksums.

`verify --root <root> --baseline <descriptor-digest>` requires the baseline's published release version, not development code with a matching version label. It validates the historical recovery selection, physical integrity, schemas, and exact inventory, then rebuilds in isolation with no persistent cache reads or writes. Source-commit provenance and supported Python/platform/dependency differences are descriptive, not replay compatibility gates.

Replay compares typed financial tables, precision, lineage, effective declarations, derivation definitions, findings, financial-content identity, and deterministic ledger/summary output. Build rows, observed runtime facts, execution fingerprints, recovery locations, physical Parquet bytes, and mechanically dependent manifest/descriptor IDs are operational differences, not financial mismatches. Located contract, domain, importer, and sink failures include the recorded and observed environments in replay diagnostics; the CLI reports named failures without financial payloads or a traceback. Replay does not compare whole manifests or change the baseline/current reference. Success proves replay in the observed supported environment, not every future installation.

`build --root <root> --project project.json` takes an optional `--comparison-baseline <descriptor-digest>`. An absent pin means no comparison, not whichever generation is current. An explicit summary baseline is comparison data only; its earlier comparison chain is not recursively replayed.

## Sinks and consumers

Parquet preserves the shared model directly, not a ledger-shaped intermediate. Beancount is a scoped projection of already determined weights and inventory slices. It cannot pick lots, invent basis, infer missing dates, or insert a rounding plug. Unsupported representations raise named sink limitations while leaving the upstream record unchanged.

The ledger conformance tests cover explicit signed lots, finite-cost action removals/readditions, transfers, disposal gains, mixed consideration, notes, declaration lifetimes, and supported unit assertions. Reference/pool inventory and transactions with differing posting dates remain outside this projection; it does not manufacture inventory costs or clearing legs. Real-loader probes distinguish negative market-price directives, which are supported, from negative posting-price annotations, which Beancount rejects. An unvalued exchange cannot be made balanced by inventing a price.

The real Beancount loader demonstrates a consequential limit: total cost 10 over three units becomes a finite rounded unit cost. The exact source total and partial allocations therefore cannot be certified by accepting Beancount's inferred tolerance. This case remains exact in the tabular contract and explicitly unsupported by the scoped ledger projection.

The independent reader folds recorded steps atomically and reconstructs inventory, component lineage, quantity positions, references, and scoped assertions. It does not apply action ratios again or calculate another settlement. Consumer-specific valuation, ownership, flow perimeter, or matching choices are explicit inputs to separate conformance examples, not new upstream determinations.

## Contract conformance

Run the [contract workflow](./tools/local-workflows.md#contract-conformance) to test the implemented boundaries. Fixture expectations are independently calculated; mechanically expanding aliases or decimal columns does not create a financial oracle.

The [coverage index](../../tests/integration/conformance/fixtures/index.json) records the version-1 structural proofs and each later processor responsibility. The ordinary suite requires every indexed obligation to be met, every table to have a nonempty specimen, and every complete case to pass real snapshot publication and independent replay. It also checks prior/replacement/retracted correction snapshots and feeds verified artifacts into explicit ownership, authority, valuation, FX, performance, and alternate-matching examples.

The ordinary suite establishes cross-domain representation and boundary behavior, including local storage faults and cache integrity. The installed end-to-end suite exercises acquisition, release CLI execution, independent cash outputs, cold/warm determinism, missing evidence, and uncached replay after ordinary reinstallation. Neither suite certifies unimplemented merging, inventory or action processors, bank completeness, or jurisdictional models. The version-1 schema remains a pre-alpha contract; revise it with independent expectations when evidence warrants.
