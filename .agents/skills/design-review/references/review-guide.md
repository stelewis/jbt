# Design Review Guide

## Review lenses

### Ownership and structure

- mixed responsibilities in one module, class, function, workflow, or configuration file
- duplicate implementations or sources of truth
- wrappers and re-export hubs that hide the real owner
- dead features, flags, adapters, and compatibility branches
- configuration options that no supported workflow needs

### Boundaries and data flow

- parsing, validation, normalization, or defaulting inside core logic
- environment reads, global state, import-time work, or hidden construction
- transport, storage, or SDK types leaking into domain logic
- malformed input accepted through best-effort coercion
- errors that lose origin, context, or observability

### Contracts and tests

- tests coupled to implementation detail rather than behavior
- fixtures that bypass real boundaries or construct invalid states
- stale tests preserving removed semantics
- missing end-to-end coverage across a changed public or operational path
- documentation that still describes the previous contract

### Tooling and automation

- multiple scripts or workflows owning the same task
- silent fallback or success-shaped failure
- local and CI paths with materially different behavior
- complex orchestration without a single discoverable entrypoint

## Root-cause patterns

### Delete hollow layers

Inline forwarding layers and move the remaining contract to its true owner.

### Split mixed responsibilities

Separate boundary parsing, deterministic logic, and side effects. Split only where each resulting unit has a clear owner.

### Move defaults to edges

Make core inputs explicit. Apply defaults in CLI, configuration, HTTP, or composition boundaries.

### Replace mode switches

When flags or strings select unrelated behaviors, use explicit types, functions, or modules and remove unsupported combinations.

### Remove legacy paths

Delete obsolete adapters and fallbacks, then update callers and tests in the same change. Do not hide the old path behind another abstraction.

### Consolidate automation

Keep one repository-owned route for each build, validation, release, or generation task. Remove duplicate commands and undocumented variants.
