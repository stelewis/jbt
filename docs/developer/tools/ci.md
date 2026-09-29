# CI And Automation

CI and project automation contracts that contributors should keep in sync with local commands.

## CI jobs

The CI workflow enforces:

- commit message policy through Commitizen
- project hygiene checks through selected `prek` hooks
- formatting and lint through Ruff
- type checking through `uv check` and Ty
- test quality checks through `tq`
- unit tests through `pytest`
- distribution builds
- optional integration tests on manual dispatch
- security checks through Betterleaks, Bandit, and `uv audit`

## Workflow shape

The CI workflow lives in `.github/workflows/ci.yml` and runs on pushes and pull requests to `main`, with a manual `workflow_dispatch` path for integration tests.

Checkout steps in repository workflows set `persist-credentials: false` unless a later step actually needs authenticated Git operations. That keeps the runner from leaving `GITHUB_TOKEN` material in the checked-out repository config when jobs execute untrusted branch contents during normal pull-request CI.

The `.github/workflows/copilot-setup-steps.yml` workflow prebuilds the GitHub Copilot coding-agent environment. It reruns automatically when the workflow itself, the shared `.github/actions/setup-python-uv` action, or locked Python dependency manifests change, so the Copilot environment contract is validated alongside CI dependency changes.

Python and `uv` setup are centralized in `.github/actions/setup-python-uv`. The setup action selects the newest release satisfying the `tool.uv.required-version` feature floor from `pyproject.toml`; Dependabot does not maintain this setting. Jobs install the committed `uv.lock` with `uv sync --locked`, and each `uv run --locked` rechecks the project dependency lock before executing.

CI and release builds use `uv build --no-sources` so build dependencies do not rely on local `tool.uv.sources` overrides.

## Dependency automation

Dependabot is configured for three update surfaces:

- GitHub Actions workflows and local composite actions
- pre-commit hook revisions
- Python dependencies managed through `uv`

All Dependabot commit messages use the `chore(scope)` convention so commit policy checks continue to pass.

External `uses:` refs are pinned to full commit SHAs with inline version comments so Dependabot can update them cleanly. External pre-commit hooks are frozen to SHAs with `# frozen:` comments. Betterleaks narrowly filters those generated pin lines in `.betterleaks.toml`.

## Contributor expectation

If you change local commands, hooks, or dependency surfaces, update CI and these docs in the same change.
