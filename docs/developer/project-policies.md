# Project Policies

Project policies enforced by automation.

## GitHub Actions policy

Repository workflows do not use `pull_request_target`, `workflow_run`, or `issue_comment` to execute CI against pull requests. Pull-request validation stays on the unprivileged `pull_request` trigger, and workflows that need issue mutation keep that write scope isolated from checkout and upstream inspection steps.

### External actions must be SHA pinned

Policy file: `.github/workflows/pinned-actions-policy.yml`

All external `uses:` references in workflows and composite actions must pin to a full 40-character commit SHA.

Allowed exceptions:

- local actions (`./...`)
- `docker://` references

Why this matters:

- protects workflows from mutable tag drift
- keeps CI reproducible and auditable
- reduces supply-chain risk

## Pre-commit hook policy

### External pre-commit hooks must be frozen to SHAs

Policy file: `.github/workflows/frozen-pre-commit-policy.yml`

All external pre-commit hooks in `.pre-commit-config.yaml` must pin `rev:` to a full 40-character commit SHA.

Allowed exceptions:

- local hooks (`repo: local`)

Why this matters:

- protects local quality and secret-scanning automation from mutable tag drift
- keeps hook execution reproducible across contributor machines and CI
- aligns hook trust with the same supply-chain bar as GitHub Actions

## Dependabot coverage policy

GitHub Actions dependency updates must cover both workflow files and local composite actions.

Required coverage:

- `directory: "/"` or `directories: ["/"]` to cover `.github/workflows`
- `directory: "/.github/actions/*"` or an equivalent `directories` entry to cover local composite actions
- a single `github-actions` update block that owns the whole GitHub Actions surface

Enforcement:

- CI tests verify the Dependabot coverage contract

Why this matters:

- prevents local actions from drifting outside automated updates
- keeps workflow and composite-action maintenance in one explicit policy surface

## Frozen pin drift visibility

Policy file: `.github/workflows/pinned-external-dependency-drift.yml`

Pinned GitHub Action refs and frozen pre-commit hook revs must be reviewed for upstream drift on a schedule, even when they are already commit pinned.

Enforcement:

- the scheduled workflow writes a summary, opens or refreshes one tracking issue when drift is detected, and fails so stale pins stay visible
- read-only drift inspection and issue mutation run in separate jobs so `issues: write` is not present during checkout or upstream release lookups

Why this matters:

- commit pinning prevents mutable ref drift but does not keep versions current
- scheduled review catches stale frozen refs that live outside lockfiles
- one tracking issue keeps maintenance visible without scattering ad hoc reminders

## Security disclosure policy

Potential vulnerabilities must be reported privately using the process in `SECURITY.md`.
