# Testing Standards

Test each contract at the narrowest boundary that can establish its behavior. Keep failures specific, expectations independently calculated, and test ownership discoverable.

## Contract-first slices

Before implementing a slice, name its owning contract and independently calculate expected records, outputs, and failures for small synthetic inputs. Include ambiguity, missing input, and boundary cases that could invalidate the design.

Distinguish three kinds of evidence:

- **Contract proof:** establish shared identities, relationships, and distinctions needed by the next consumer. Implement detailed representations and validation when a processor needs them.
- **Boundary feasibility:** use the actual writer, reader, arithmetic, or filesystem primitive when its behavior decides the contract. A JSON round trip cannot prove Parquet types, SQL arithmetic, or durable publication.
- **Processor proof:** require the processor to produce the independently calculated expectations through the affected boundaries, reusing contract fixtures.

Hand-authored results prove a reader contract, not a processor's calculations. Keep financial verification independent of the producer without duplicating its entire validation implementation. If evidence exposes a flawed contract, revise it and its independent expectations together. Specifications define requirements; executable tests establish that the implementation meets them.

## Test scope and ownership

Test transformations and consumer policy directly on small typed inputs or validated rows. A direct test may combine pure collaborators; crossing module boundaries does not by itself require a full build.

Use full builds when the contract concerns orchestration, publication, custody, or replay. Each expensive workflow test must catch a failure that cheaper model, command, storage, or reader tests cannot. Exercise serialization on representative financial shapes rather than publishing every arithmetic fixture.

### Unit tests

Mirror the owning source module by default:

- Source: `src/<project>/<path>/<module>.py`
- Test: `tests/<project>/<path>/test_<module>.py`

Prove shared behavior once at its actual owner, not in every caller. A source module does not require a separate suite if its contract is already proved elsewhere; an import-only smoke test is not a substitute for behavioral assertions.

Split distinct responsibilities with stable `test_<module>_<qualifier>.py` names, using the qualifiers allowed by the [test configuration](../../../pyproject.toml). Relocate or consolidate tests when ownership changes rather than duplicating a suite because a module was split.

### Integration and installed acceptance

- Put workflow tests that exercise interactions across real boundaries in `tests/integration/`, mark them with `pytest.mark.integration`, and use workflow-oriented names.
- Put installed-package acceptance tests in `tests/e2e/` with `pytest.mark.e2e`. This suite proves packaged resources, entry points, release provenance, cache reuse, and relocated checked replay. Ordinary tests own detailed financial and correction scenarios.

Installed-package tests are excluded by default because provisioning may access the network. Build and install once per session, reusing the environment across these tests, or supply the final wheel with `--release-wheel`. See [local workflows](../tools/local-workflows.md) for commands and provisioning.

## Fixtures and expected results

Invent public fixtures from scratch using contract boundaries, public specifications, and observed failure patterns. Never copy private code, records, screenshots, paths, or source-repository references into them; renaming real records is not sufficient anonymization. Synthetic cases establish the stated behavior, not exhaustive real-world coverage.

- Share reusable fixtures through `conftest.py` at the narrowest appropriate level.
- Keep fixture files beside their tests, small, versioned, and stably ordered.
- **Synthetic** describes invented inputs; **golden** describes independently reviewed expected results. Golden tests remain offline and deterministic.
- Assert meaningful records or fields rather than freezing entire snapshots. Explain semantic changes to golden results; never derive expected financial results solely from the implementation under test.

## Test quality

- Protect an owned contract or observed failure, not incidental text, layout, forwarding functions, or command ordering.
- Parameterize distinct semantics rather than equivalent syntax or Cartesian combinations.
- Prove structural validation once, with representative boundary failures, rather than repeating missing/extra-field matrices for every variant and decoder.
- Use the markers registered in [`pyproject.toml`](../../../pyproject.toml).

## Validation workflow

Run tests for the changed contract and relevant integration workflow tests together during development. Run the complete default suite at integration boundaries. Also run the installed-package acceptance suite when execution, packaging, custody, or replay changes.

Measure with `--durations=10`; reduce repeated construction and scenarios before adding parallelism or excluding tests. Scaling tests assert bounded work or evidence size, not elapsed-time thresholds. At integration boundaries, report the test selector and default-suite elapsed time; explain material increases in terms of the contracts they protect.

### Test-quality checks

Run layout and size checks with:

```bash
uv run --locked tq check
```

The [`tq` configuration](../../../pyproject.toml) treats misplaced or orphaned suites as errors and missing module mappings as informational.

Record reviewed exceptions in the target's `accepted_findings`:

- `mapping-missing-test`: exact source path relative to the package directory; rationale naming the proof owner.
- `test-file-too-large`: exact path relative to `test_root`; rationale and `max_non_blank_lines` set to the reviewed size.

Remove unused acceptances and update rationales when proof ownership moves. Review cohesion, assertion quality, and runtime cost directly.
