# Architecture

`jbt` has a contract implementation and a synthetic conformance harness, not an implemented financial build pipeline. Its foundations validate exact values, identities, declarations, financial rows, and complete static artifacts. Hand-authored expected records exercise those boundaries without pretending that a parser, merge engine, or booking processor produced them.

The [target architecture](../design/target-architecture.md) describes the intended pipeline. [Delivery](../plans/delivery.md) distinguishes contract proof, boundary feasibility, and later processor proof.

## Dependency direction

| Boundary | Owns | Dependencies |
| --- | --- | --- |
| `jbt.domain` | Bounded exact arithmetic, explicit money states, civil time, stable identities, canonical encodings, correction guards | Python standard library; no IO, environment, plugin loader, or sink |
| `jbt.contracts` | Versioned table catalog, closed JSON schemas, declaration and cross-record validation | Domain values and jsonschema; packaged schema data |
| `jbt.artifacts` | Explicit Parquet types, static snapshot assembly, integrity, logical content digests, scoped Beancount projection | Domain and contracts; PyArrow at the file boundary |
| Independent conformance consumer | Read pinned files, validate/replay published determinations, exercise consumer policy | DuckDB and independently implemented arithmetic; no `jbt` imports |

Import exact owning modules. Package initializers do not load native adapters or re-export their APIs. The consumer shares the published specification, not the producer's validator, serializer, arithmetic, or financial choices.

## Exactness and identity

Finite financial values retain a canonical coefficient, nonnegative scale, and separately stated source precision. Arithmetic admits bounded intermediates before calculation, retains rational values until an explicit rounding point, and reports unavailable components separately from known zero and inapplicable values. The [numeric contract](../design/analysis-boundary.md#exact-decimals-and-query-arithmetic) owns capacity and rounding semantics.

Economic identity uses domain-separated canonical anchors and stable child keys. Source authority, changed values, display labels, and replay sequence do not choose identities. Explicit membership and reviewed merge/split transitions are inputs; the foundations do not discover duplicates or silently follow retired identities when applying corrections.

Generated identity framing and wire JSON serve different purposes. Declaration revision digests cover canonical typed JSON; row/guard and manifest encodings preserve typed meaning while rendering integer fields as canonical decimal strings. Schema JSON retains native integer constraints. Callers select the encoding explicitly; decoders do not accept alternate encodings as compatibility conveniences.

## Static artifact assembly

`assemble_snapshot` receives the complete non-build table set, explicit entity build facts, retained interpretation resources, and writer settings. It validates rows and metadata and writes into a caller-owned empty directory:

1. Serialize the schema resources and write non-build Parquet tables, including typed empty tables.
2. Record byte and logical table digests, retained inputs, configuration, checks, and execution settings in the manifest.
3. Hash the manifest and write `build.parquet` with that digest.
4. Write the descriptor last, covering all payload bytes without a checksum cycle.

The caller pins the descriptor checksum outside the artifact. Readers select that descriptor once; directory discovery is not snapshot selection. Byte integrity is not semantic validity: conformance tests also alter rows and recompute mechanical checksums to exercise independent schema and relationship failures.

Financial-content identity includes every retained semantic schema, not only Parquet column definitions. Execution identity separately includes the actual writer settings and toolchain. Fresh-process tests vary input-map enumeration, hash seed, working directory, and locale while requiring identical snapshot bytes under the same toolchain.

The static assembler does not implement exclusive writers, acquisition recovery, durable renames, or atomic replacement of a published generation. Those require the later execution slice. A returned static artifact is not evidence that a private financial project can be restored or that an interrupted runtime publisher is safe.

## Sinks and consumers

Parquet preserves the shared model directly, not a ledger-shaped intermediate. Beancount is a scoped projection of already determined weights and inventory slices. It cannot pick lots, invent basis, infer missing dates, or insert a rounding plug. Unsupported representations raise named sink limitations while leaving the upstream record unchanged.

The ledger conformance tests cover explicit signed lots, finite-cost action removals/readditions, transfers, disposal gains, mixed consideration, notes, declaration lifetimes, and supported unit assertions. Reference/pool inventory and transactions with differing posting dates remain outside this projection; it does not manufacture inventory costs or clearing legs. Real-loader probes distinguish negative market-price directives, which are supported, from negative posting-price annotations, which Beancount rejects. An unvalued exchange cannot be made balanced by inventing a price.

The real Beancount loader demonstrates a consequential limit: total cost 10 over three units becomes a finite rounded unit cost. The exact source total and partial allocations therefore cannot be certified by accepting Beancount's inferred tolerance. This case remains exact in the tabular contract and explicitly unsupported by the scoped ledger projection.

The independent reader folds recorded steps atomically and reconstructs inventory, component lineage, quantity positions, references, and scoped assertions. It does not apply action ratios again or calculate another settlement. Consumer-specific valuation, ownership, flow perimeter, or matching choices are explicit inputs to separate conformance examples, not new upstream determinations.

## Contract conformance

Run the [contract workflow](./tools/local-workflows.md#contract-conformance) to test the implemented boundaries. Fixture expectations are independently calculated; mechanically expanding aliases or decimal columns does not create a financial oracle.

The [coverage index](../../tests/integration/step_0/fixtures/index.json) records the version-1 structural proofs and each later processor responsibility. The ordinary suite requires every indexed obligation to be met, every table to have a nonempty specimen, and every complete case to pass real snapshot publication and independent replay. It also checks prior/replacement/retracted correction snapshots and feeds verified artifacts into explicit ownership, authority, valuation, FX, performance, and alternate-matching examples.

This establishes delivery Step 0: representation, value/identity rules, and real boundary feasibility. It does not certify unimplemented parsers, booking or action processors, durable publication, recovery execution, or jurisdictional models. Changes to a frozen schema's meaning require an explicit schema revision and corresponding consumer support, not a compatibility decoder.
