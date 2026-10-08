# CI And Automation

CI and project automation contracts that contributors should keep in sync with local commands.

## CI jobs

The CI workflow enforces:

- commit message policy through Commitizen
- project hygiene checks through selected `prek` hooks
- Markdown spelling through the pinned CSpell hook
- formatting and lint through Ruff
- type checking through `uv check` and Ty
- test quality checks through `tq`
- one ordinary suite and one installed end-to-end replay suite through `pytest`, on macOS and Linux
- distribution builds, packaged resource checks, and the installed CLI
- security checks through Betterleaks, Bandit, and `uv audit`

## Workflow shape

The [CI workflow](../../../.github/workflows/ci.yml) runs on pushes and pull requests to `main`, and through manual dispatch. Its test matrix runs on macOS and Linux with twenty minutes per platform. Superseded CI runs are cancelled.

Checkout steps in repository workflows set `persist-credentials: false` unless a later step actually needs authenticated Git operations. That keeps the runner from leaving `GITHUB_TOKEN` material in the checked-out repository config when jobs execute untrusted branch contents during normal pull-request CI.

The `.github/workflows/copilot-setup-steps.yml` workflow prebuilds the GitHub Copilot coding-agent environment. It reruns automatically when the workflow itself, the shared `.github/actions/setup-python-uv` action, or locked Python dependency manifests change, so the Copilot environment contract is validated alongside CI dependency changes.

CI, publication builds, and the Copilot environment share [Python/uv setup](../../../.github/actions/setup-python-uv/action.yml). The action selects the newest uv release satisfying `tool.uv.required-version`, then selects Python and installs locked dependencies with `uv sync --locked --python "$PYTHON_VERSION"`. uv can use an existing interpreter or provision one. Dependabot maintains the action pin, not the uv feature floor; `uv run --locked` checks lockfile consistency before executing.

Each test-matrix job runs the ordinary pytest suite, including contract conformance, then builds one wheel/source pair. The [archive checker](../../../.github/scripts/check_release.py) checks metadata and complete package payloads without installation. The job passes that same wheel to `uv run --locked pytest -q -m e2e --release-wheel "$wheel"` for isolated installed acceptance: independent financial results, cold/warm determinism, missing-evidence refusal, and uncached verification after reinstallation. Editable tests cannot establish that packaged resources and the console entry point work.

CI and publication use `uv build --no-sources` so distributions do not rely on local `tool.uv.sources` overrides. [Release standards](../standards/releases.md) owns the additional publication gate and the tested-artifact handoff.

## Dependency automation

Dependabot is configured for three update surfaces:

- GitHub Actions workflows and local composite actions
- pre-commit hook revisions
- Python dependencies managed through `uv`

All Dependabot commit messages use the `chore(scope)` convention so commit policy checks continue to pass.

External `uses:` refs are pinned to full commit SHAs with inline version comments so Dependabot can update them cleanly. External pre-commit hooks are frozen to SHAs with `# frozen:` comments. Betterleaks narrowly filters those generated pin lines and the two exact synthetic accession digests in the cash example; it does not exempt arbitrary example data or identifiers.

## Contributor expectation

If you change local commands, hooks, or dependency surfaces, update CI and these docs in the same change.
