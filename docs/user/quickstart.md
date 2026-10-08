# Build a Reconciled Cash Record

This pre-alpha example starts with invented OFX evidence, not already booked transactions. It preserves an independent opening point of 100 USD, recognizes a 20 USD credit and a 5 USD debit, and checks the resulting 115 USD balance through source reconciliation, model reconciliation, and a real Beancount loader.

Use a local filesystem on macOS or Linux and supported Python 3.14 or later. Install a published release with `uv`; no Git checkout or project dependency lock is required. Provisioning may fetch packages, Python, or advisory data before `jbt` starts. `jbt` itself does not install dependencies or make automatic network requests. Keep real financial records outside any public checkout.

## Install and acquire the example

Replace `<release>` with the selected published version:

```bash
uv tool install 'jbt==<release>'
```

Save the synthetic [project declaration](../../examples/cash/project.json), [prior OFX evidence](../../tests/jbt/importers/fixtures_synthetic/prior.ofx), and [main OFX evidence](../../tests/jbt/importers/fixtures_synthetic/main.ofx) as `project.json`, `prior.ofx`, and `main.ofx` in a local example directory. From that directory, create the financial root and copy the authored project:

```bash
ROOT="$PWD/cash-record"
mkdir "$ROOT"
cp project.json "$ROOT/project.json"
```

The selected root must not be a symlink. The authored project declares its account, quantity position, commodity, categories, explicit source bindings, January horizon, rules, opening movement, and output requirements. Its receipt IDs correspond to the exact synthetic files and acquisition arguments below. These are repeatable example facts, not timestamps to reuse for real acquisitions.

```bash
jbt acquire --root "$ROOT" prior.ofx --retry-key prior.ofx --acquired-at 2026-02-02T00:00:00Z --source-scope bank-feed --importer ofx
jbt acquire --root "$ROOT" main.ofx --retry-key main.ofx --acquired-at 2026-02-02T00:00:00Z --source-scope bank-feed --importer ofx
```

Acquisition copies evidence; it never moves or deletes the originals. Repeat the same request with the same retry key to recover an interrupted attempt. Use a different key for an intentional new accession. Reusing a key with changed bytes or acquisition context fails.

## Build and inspect

```bash
jbt build --root "$ROOT" --project project.json > "$ROOT/build.json"
```

The project filename is relative to the selected root; absolute paths, traversal, and symlinked descendants are rejected. The result identifies an immutable generation and descriptor digest. That generation contains the complete shared Parquet table set, extraction and model artifacts, financial findings, the ledger, and the summary. Its provenance records the immutable published release version, source commit when available, and observed Python, platform, and visible distribution versions. The current reference changes only after the entire generation has been validated and installed.

To request a change summary against a prior generation, add `--comparison-baseline <descriptor-digest>` to `build`. Without that option, comparison is absent rather than selected from current state. The prior generation supplies comparison data; its own earlier comparison history is not recursively rebuilt.

The January summary should report:

| Opening | Credits | Debits | Closing |
| --- | --- | --- | --- |
| 100 USD | 20 USD | -5 USD | 115 USD |

The authored initialization is dated December 31, before the independently observed January 1 midnight point. The February 1 midnight point checks January's closing quantity. Neither point certifies complete source coverage: OFX supplies search bounds, not a complete-statement guarantee. Coverage is explicitly `not_evaluable`; this is a reconciled working record, not a complete-account attestation. The [supported OFX profile](../developer/ofx-profile.md) explains the admitted timestamps and rejected ambiguities.

Run the same release build again to reuse verified stages. Corrupt derived cache entries are reported and recomputed. Missing or corrupt retained financial inputs fail even with a warm cache. A failed required financial check cannot replace the current generation.

## Verify from retained inputs

Copy `descriptor_digest` from the build's JSON result into the variable below. Pin the generation explicitly rather than following a moving current reference:

```bash
DESCRIPTOR_DIGEST="<descriptor_digest from build.json>"
jbt verify --root "$ROOT" --baseline "$DESCRIPTOR_DIGEST"
```

Verification requires the recorded published release version, loads the generation's historical financial selection, validates its exact retained inventory, and rebuilds without persistent cache reads or writes. It compares financial tables, findings, precision, lineage, and deterministic ledger/summary output, not whole manifests or physical Parquet bytes. Neither success nor failure replaces the baseline or current reference. Success establishes reproducibility in the observed supported environment, not independent financial truth.

Preserve the complete financial root when restoring it: source objects, intents and receipts, exact retained authored selections, the pinned generation, and any explicitly selected comparison generation. The inbox, original authored working file, checkout, package cache, and old installation are not recovery inputs. Reinstall the recorded release normally with `uv` before running verification.

Supported Python/platform and compatible dependency versions may differ from the original run; verification reports those facts and succeeds only if financial results match. Source-commit provenance is optional, not an additional release-matching requirement. An unavailable release, unsupported environment, different release version, or financial mismatch fails explicitly. `jbt` does not repair the installation. Development builds from editable source are useful for synthetic experiments but bypass persistent cache and cannot verify a published-release baseline.

## Limits and failures

- This processor admits one explicitly bound, quantity-only cash account. It does not infer treatment from the sign of an amount; unmatched category rules fail.
- Overlapping movement sources, revisions, pending transactions, unsupported economic fields, and ambiguous boundary-day movements are rejected, not skipped.
- Date-only balances do not establish midnight points. Supported comparisons require explicit UTC boundary evidence and an explicitly declared UTC account.
- Another active writer causes refusal rather than concurrent mutation. Retry after that writer finishes.
- Supported Python socket and DNS attempts are denied during processing and prevent publication even if caught. This guard is not a native-code sandbox.
- A failure after current-reference replacement but before durability acknowledgement is uncertain acknowledgement: inspect the complete selected generation before retrying. It is not a promise that the old pointer remains selected.

The [implemented architecture](../developer/architecture.md) owns the execution boundaries and durability contract. Later financial families remain separate delivery work.
