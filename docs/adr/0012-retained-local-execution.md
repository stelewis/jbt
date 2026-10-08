---
id: 0012
title: "Published Releases, Checked Replay, and Local Publication"
status: accepted
date: 2026-10-01
tags: [execution, recovery, publication]
supersedes: null
superseded_by: null
---

## Context

A user must be able to recover a financial build after losing the original working files and Python installation. Recovery needs the exact inputs, a known application release, and a saved result to check against. Reinstalling software alone does not prove that it produces the same financial result.

Publication also needs a clear commit point: readers must see one complete generation, never a mixture of old and new files. Missing evidence must cause failure, not disappear from a newly scanned inventory.

## Decision

### Separate software installation from financial processing

`uv` installs software; `jbt` captures inputs, computes results, publishes generations, and verifies replay. `jbt` never installs or repairs its environment. Provisioning may use the network; financial processing denies supported Python socket and DNS operations. An attempted operation prevents publication or verification success even if a component catches the denial. This is not a native-code sandbox.

The installed package name and published version identify the production producer. That requires [once-only release publication](../developer/standards/releases.md): different code must have a different version. Standard package metadata is sufficient under trusted provisioning; it does not authenticate an untrusted wheel or detect a rewritten installation. Source commits are optional provenance, not another producer identity.

Index and archive installs are release producers. Directory and VCS installs, editable or not, are development producers. Development runs never read or write persistent stage cache and do not hash mutable source. Installations and development source must remain unchanged during a command.

Record Python implementation/version, platform/architecture, and normalized names and reported versions of every visible distribution. Sort the inventory and reject missing metadata or duplicate normalized names without importing discovered packages. Release-cache identity includes all these facts, the producer, stage/schema, relevant configuration, and named ordered inputs. Cache reuse is deliberately stricter than financial replay.

### Retain the exact financial selection

Under one project-wide advisory writer lock, acquisition copies evidence without deleting the source, installs verified content-addressed bytes, and durably records the acquisition context. A retry key recovers the same request; a new accession records an intentional new acquisition.

Before computation, retain the exact authored bytes, selected committed accessions, execution record, and explicit comparison pin. Keep captured failed-attempt selections. Recovery uses that expected inventory; missing evidence or receipts fail even with a warm cache. Financial roots retain inputs and results, not interpreters, packages, copied source, or the repository lock.

### Publish only complete immutable generations

Hold the writer lock through input capture, computation, and publication. Validate and synchronize every output, install a complete immutable generation on the same filesystem, then atomically replace the current reference. Readers pin one descriptor and verify its exact file inventory.

The project-wide lock serializes local writers and avoids distributed coordination the current workload does not need.

Before reference replacement, failure leaves the old generation selected. After replacement but before the reference directory is synchronized, failure means uncertain acknowledgement: the new complete generation may be selected. It does not guarantee rollback. Preserve prior generations and irreplaceable evidence; builds do not garbage-collect them. This contract covers tested local filesystems, not arbitrary network mounts or hardware power loss.

### Make replay and comparison explicit

Verification takes an explicit descriptor pin and requires intact retained inputs and the same published release version. Development code cannot substitute, even with a matching version label.

Validate the baseline's bytes, inventory, and schemas, then rebuild privately without persistent cache reads or writes. Compare financial meaning and configured deterministic ledger/summary output, not whole manifests or Parquet bytes. The [financial comparison contract](../design/analysis-boundary.md#build-metadata-and-evolution) includes typed rows, precision, lineage, effective declarations, derivations, and findings; it excludes build metadata, environment facts, encoding, recovery locations, rendering choices, and repeated acquisition context from financial-content identity.

Supported Python, platform, and dependency differences are reported, not automatically rejected. Source-commit provenance is not a matching requirement. A located failure leaves baseline/current bytes unchanged. Success proves equality in the verifier's observed environment, not every future installation or independent financial correctness.

A summary comparison is explicitly absent or names a pinned prior generation. The pin supplies comparison data only: do not replay its earlier comparison chain. Changing it can change the summary without changing financial determinations or financial-content identity.

## Consequences

- Recovery no longer needs the original inbox, working project file, checkout, package cache, or old installation. It still needs the saved financial recovery set and an available release that runs in a supported environment.
- Writers are serialized. Retained data consumes storage; disposable cache corruption is reported and recomputed, while retained-input corruption fails.
- Independent financial checks remain necessary. The [cash proof](../developer/ofx-profile.md) reconciles separate source points but cannot certify complete account coverage.
- Exact historical dependency reconstruction is not promised. A demonstrated archival obligation would require a separate design and supply-chain review.

## Alternatives considered

### Copy a runnable environment or publish a downstream lock recipe

This adds platform, storage, and availability obligations without proving financial equality.

### Add source/build hashes as production identity

This does not enforce once-only publication or authenticate an installation. Hashing mutable development source cannot establish a stable execution.

### Publish files separately or copy across filesystems

A reader could see an incomplete or mixed generation.

### Recover from cache or scan surviving inputs

This could hide lost evidence and change the expected recovery set.

### Compare against current state or replay all prior baselines

The former introduces hidden history dependence; the latter adds unnecessary recursive recovery.

## Related

- [Implemented architecture](../developer/architecture.md)
- [Synthetic cash workflow](../user/quickstart.md)
