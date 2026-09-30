# Testing Standards

Treat the test suite as a first-class part of the codebase.

Keep tests:

- **Discoverable**: easy to find the test for a module.
- **Focused**: small surface area, minimal cross-module coupling.
- **Actionable**: failures point to one contract, not "the system".
- **Maintainable**: tests refactor with the code (SOLID applies here too).

## Contract-first slices

Before implementing a slice, name its owning contract and resolve small synthetic inputs to independently calculated expected records, outputs, and failures. Include ambiguity, missing input, and the boundary conditions that could invalidate the design, not just a happy path.

Distinguish three kinds of evidence:

- **Contract proof:** versioned schemas and complete fixtures validate identities, relationships, invariants, and what an independent reader can reconstruct. Exercise shared representations across the intended domains before relying on them for durable publication; do not implement every producer merely to construct its expected output.
- **Boundary feasibility:** use the actual writer, reader, arithmetic, or filesystem primitive when its behavior decides the contract. A mock or JSON-only round trip does not prove Parquet types, SQL arithmetic, or durable publication.
- **Processor proof:** establish the selected slice's expected transformations before writing its processor, then require that processor to produce them through the real boundaries. Reuse contract fixtures rather than maintaining a parallel example system.

Hand-authored allocation or action results can prove a reader contract, but cannot prove that a future booking or action processor calculates them correctly. Accepted ADRs record decisions; passing executable checks establish readiness. A failing feasibility check reopens the owning decision, not the correctness requirement.

Existing systems and observed failures can supply scenarios, not authoritative expected behavior. Reconstruct public fixtures from scratch with invented identities, dates, amounts, and text, preserving only the relevant structural relationships. Do not copy private code, records, screenshots, paths, or source-repository references into public artifacts. Renaming identifiers in a real statement is not sufficient anonymization.

## Structure Rules

### One test module per source module (minimum)

For each source module there must be *at least one* corresponding test module.

Required mapping:

- Source: `src/<project>/<path>/<module>.py`
- Test: `tests/<project>/<path>/test_<module>.py`

This is intentionally strict. It prevents "mystery tests" and keeps the suite aligned as the architecture evolves.

A smoke test can establish importability, but does not satisfy a contract or slice-completion gate.

### Splitting tests (qualifiers)

If a test suite is too large for one file, split into multiple files by concern. Use the naming convention:

- `test_<module>_<qualifier>.py`

Add a qualifier only when it improves clarity and separation of concerns. Qualifiers should be stable and describe a single responsibility.

Avoid qualifiers that encode implementation details or ephemeral refactors.

Note: pytest runs with `--import-mode=importlib` (see `pyproject.toml`) to avoid import name clashes across similarly named tests.

### Single-target rule (unit tests)

By default, a unit test module targets exactly one source module: the one implied by its path/name.

This means:

- Do not keep a monolithic test file after splitting a large source module into smaller modules.
- Do not "reach across" and assert behavior that belongs to other modules.

If you need behavior across modules, the contract usually belongs in a higher-level integration test.

### Integration tests

Integration tests are allowed when they validate real workflows. These may span multiple modules by design.

Place integration tests in `tests/integration/` to separate them from unit tests. This keeps the unit test suite focused and discoverable.

Rules:

- Mark integration tests with `pytest.mark.integration`.
- Keep integration test names workflow-oriented (e.g. `test_ingest_cli.py`).

### End-to-end tests

If needed, place in `tests/e2e/` and mark with `pytest.mark.e2e`.

### Golden / snapshot fixture tests

Golden (aka “snapshot”) tests are allowed when they validate a correctness-critical contract that is hard to express as small unit assertions.

- **Synthetic** describes the input's origin; **golden** describes an independently reviewed expected result. A synthetic case can have golden outputs.
- Derive cases from contract boundaries, public format specifications, and observed failure patterns. Synthetic conformance establishes the stated cases, not exhaustive real-world coverage.
- Never generate an expected financial result solely with the implementation being tested. Calculate small expectations independently and explain intentional changes.

Rules:

- Must be fully offline and deterministic (no network, no wall-clock time).
- Keep fixtures small and reviewable (prefer JSONL/JSON; stable ordering; version fields).
- Avoid “assert the entire snapshot equals a blob” unless you have a strong reason.

Placement:

- Keep golden tests in `tests/<project>/...` near the subsystem they validate.
- Store fixture files adjacent to the test module (e.g. `fixtures/golden` or `fixtures/synthetic`).

### Excluded Tests

Some marked tests may be excluded to keep `pytest` fast and deterministic. Run them explicitly when needed:

```bash
uv run --locked pytest -m e2e
```

## Test Quality Standards

### Avoid these anti-patterns

- **Monolithic unit tests**: one test module covering many unrelated modules.
- **Cross-module unit tests**: tests with no clear single target.
- **Duplicated coverage**: multiple suites asserting the same contract.
- **Redundant tests**: re-testing behavior already validated elsewhere.
- **Structure mismatches**: test location doesn’t mirror the source module.
- **Misnamed tests**: name implies a different target than what it covers.
- **Orphaned tests**: tests primarily covering code that no longer exists.
- **Vacuous tests**: tests that pass without meaningfully exercising behavior.
- **Change-detector tests**: assertions that freeze incidental text, layout, or command ordering without protecting an owned contract.
- **Duplicated integration scenarios**: repeated expensive setup for behavior already established by another case.
- **Very large test modules**: tests that try to cover too much in one suite.

Use this practical refactor rule:

- If you split `foo.py` into `foo/alpha.py` and `foo/beta.py`, the tests should split too. Keeping `test_foo.py` as a grab-bag is the failure mode to avoid.

### Use pytest markers

Use markers to communicate intent and enable selective runs.

Use the project markers registered in `pyproject.toml` if the test is one of:

- `pytest.mark.e2e`: end-to-end tests (full user workflows).
- `pytest.mark.golden`: deterministic golden/snapshot fixture tests (offline).
- `pytest.mark.integration`: multi-module workflow tests.
- `pytest.mark.regression`: tests added for a fixed bug/regression.
- `pytest.mark.slow`: tests that may be skipped in fast CI jobs.
- `pytest.mark.smoke`: quick, minimal tests (including placeholders).

### Fixtures

Place reusable fixtures in `conftest.py` at appropriate levels to share them across test modules, avoid duplication, and reduce refactor surface.

## Workflow

Run the automated test-quality checker with:

```bash
uv run --locked tq check
```
