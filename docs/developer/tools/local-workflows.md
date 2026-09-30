# Local Workflows

Core contributor commands for day-to-day work.

## Environment setup

- `uv sync --locked`
- `uv run --locked prek install`

Use `uv sync --locked` to create or update the project environment from the committed, reviewed `uv.lock` without silently re-resolving dependencies.

## Fast edit loop

- `uv run --locked ruff format`
- `uv run --locked ruff check --fix`
- `uv check --locked`
- `uv run --locked pytest -q`

Use these commands during normal development before running the full quality gate.

## Contract conformance

Run the pure foundations, schema validators, artifact boundaries, and independent consumer together:

```bash
uv run --locked pytest -q tests/jbt/domain tests/jbt/contracts tests/jbt/artifacts tests/integration/conformance
```

These tests also run in the ordinary pytest suite; they are not behind the default-excluded `e2e` marker. The [implemented architecture](../architecture.md) explains what their evidence does and does not establish. Keep synthetic financial expectations independent of the algorithms being tested. The coverage gate requires zero unproven indexed obligations and a nonempty specimen for every table; every indexed case is published and independently verified rather than sampling only cash examples.

The [contract dependencies](./contract-dependencies.md) include native libraries for real Parquet, SQL/UDF, and Beancount checks. Dependency provisioning and advisory checks may access the network; the financial tests must not fetch files, install extensions, or contact services.

The ordinary CI test job allows ten minutes for these real boundary checks. The package job builds both distributions, compares their schema resources, and exercises the Parquet writer imported directly from the built wheel in isolated Python; editable-checkout imports are not packaging evidence.

## Full quality gate

- `uv run --locked ruff format && uv run --locked ruff check --fix && uv check --locked && uv run --locked tq check && uv run --locked pytest -q`

Run this combined check before opening or updating a pull request.

## Pre-commit hooks

Install `pre-commit` and `commit-msg` hooks through one command:

- `uv run --locked prek install`

The default hook set covers hygiene checks, lockfile updates, Ruff formatting and linting, secret scanning, commit message policy, type checking, and Bandit.

Run all hooks locally with `uv run --locked prek run -a`.

When rotating external hook revisions, use a frozen update flow so `.pre-commit-config.yaml` stays SHA pinned. See [Pin maintenance](./pin-maintenance.md).

## Security and dependency audit

- `uv run --locked bandit --configfile pyproject.toml -r src`
- `uv audit --locked`

These commands are also enforced in CI. `uv audit` checks all groups and extras in the locked resolution, including dependencies that are not installed on the current platform. In addition, `tool.uv.audit.malware-check` makes every uv sync query OSV for known malware in the locked resolution and stop before installing a match. This check relies on published advisories, so it complements rather than replaces dependency review.

The project opts into the released but unstable `uv check`, `uv audit`, and malware-check interfaces through `tool.uv.preview-features`; remove each feature name after Astral declares that feature stable.
