# Local Workflows

Core contributor commands for day-to-day work.

## Environment setup

- `uv sync --locked --python 3.14`
- `uv run --locked prek install`

Create or update the developer environment from the committed, reviewed `uv.lock` without silently re-resolving dependencies. Supported Python is 3.14 or later; `uv` may select an installed interpreter or provision one.

Use `uv run --locked jbt build --root <synthetic-root> --project project.json` to try application changes. An editable checkout records a development producer and bypasses persistent stage cache, even when clean. Keep source unchanged during a command.

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

These tests also run in the ordinary pytest suite. Keep synthetic financial expectations independent of the algorithms being tested; the [implemented architecture](../architecture.md) explains the evidence requirements.

The [contract dependencies](./contract-dependencies.md) include native libraries for real Parquet, SQL/UDF, and Beancount checks. Dependency provisioning and advisory checks may access the network; the financial tests must not fetch files, install extensions, or contact services.

## Installed CLI tests

Ordinary tests exercise the editable checkout. Run installed acceptance separately:

```bash
uv run --locked pytest -q -m e2e
```

This builds a wheel from the working checkout, including uncommitted application edits, and installs it in private temporary tool environments. It exercises acquisition, builds, and historical verification after reinstallation without changing your own tool installation. The explicit marker overrides the default exclusion of `e2e`.

To test an existing wheel without rebuilding:

```bash
uv run --locked pytest -q -m e2e --release-wheel "$wheel"
```

Set `wheel` to the distribution path. Provisioning may contact package indexes and OSV; do not disable security checks to simulate an empty offline advisory cache. See [CI and automation](./ci.md) for hosted jobs and [release standards](../standards/releases.md) for a full publication rehearsal.

## Full quality gate

- `uv run --locked ruff format --check && uv run --locked ruff check && uv check --locked && uv run --locked tq check && uv run --locked pytest -q && uv run --locked pytest -q -m e2e`

Run this combined check before opening or updating a pull request.

## Pre-commit hooks

Install `pre-commit` and `commit-msg` hooks through one command:

- `uv run --locked prek install`

The default hook set covers hygiene checks, Markdown spelling, lockfile updates, Ruff formatting and linting, secret scanning, commit message policy, type checking, and Bandit.

Run all hooks locally with `uv run --locked prek run -a`.

Run spelling alone with `uv run --locked prek run cspell --all-files`, or use `--files <changed Markdown paths>` for a focused edit. The hook checks tracked Markdown, including future documentation pages. Correct prose rather than adding dictionary exceptions; add reviewed project terms to `cspell.json` only for genuine terminology. The VS Code Code Spell Checker extension can use the same config for immediate feedback, but is optional.

When rotating external hook revisions, use a frozen update flow so `.pre-commit-config.yaml` stays SHA pinned. See [Pin maintenance](./pin-maintenance.md).

## Security and dependency audit

- `uv run --locked bandit --configfile pyproject.toml -r src`
- `uv audit --locked`

These commands are also enforced in CI. `uv audit` checks all groups and extras in the locked resolution, including dependencies that are not installed on the current platform. In addition, `tool.uv.audit.malware-check` makes every uv sync query OSV for known malware in the locked resolution and stop before installing a match. This check relies on published advisories, so it complements rather than replaces dependency review.

The project opts into the released but unstable `uv check`, `uv audit`, and malware-check interfaces through `tool.uv.preview-features`; remove each feature name after Astral declares that feature stable.
