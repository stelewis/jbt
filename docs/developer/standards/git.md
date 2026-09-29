# Git Workflow Standards

Use this workflow to keep `main` stable and changes easy to review.

## Goals

- **Stable trunk**: `main` is always green and shippable.
- **Linear history**: readable blame/log; avoid merge-commit noise.
- **Verified provenance**: commits on `main` must preserve GitHub verified signature status.
- **Fast integration**: resolve conflicts early and reduce long-lived divergence.
- **Small refactor surface area**: integrate in small slices; keep changes easy to review.

## Branch Model

- **Trunk-based development**: `main` is the trunk.
- **No direct pushes to trunk**: all changes land via pull requests.
- **Short-lived branches**: create branches per change; delete after merge.

## Branch Naming

Use a short, descriptive name with a clear prefix:

- `feature/<topic>`
- `fix/<topic>`
- `refactor/<topic>`
- `chore/<topic>`
- `docs/<topic>`

## Keeping Branches Current

- Rebase your branch onto `main` early and often.
- Prefer resolving conflicts on your branch (before review) rather than on `main`.
- Avoid large “integration” PRs created by letting a branch drift.

## Pull Requests

- Every change uses a PR.
- Keep PRs small and focused (one intent; one reason to change).
- Trunk is protected by required checks and reviews.

## Merge Strategy

Use one of these approaches:

- **Squash merge onto `main` from the GitHub web UI**: default approach. It produces one meaningful, verified commit per PR and keeps `main` linear without rewriting the branch commits into unsigned commits.
- **Signed local rebase and fast-forward onto `main`**: exception for PRs where preserving individual commits on `main` is more valuable than the simpler squash history. The maintainer rebases locally, signs the rewritten commits with GPG or SSH signing, verifies every commit, and pushes with fast-forward semantics.

Do not use GitHub's web-based **Rebase and merge** for `main`. GitHub creates new commits during that operation; those rewritten commits do not preserve the original cryptographic signatures and can leave unverified commits in protected history.

Do not merge PRs with merge commits into `main`.

## Commit Hygiene

- Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages.
- Commit messages are validated with [Commitizen](https://commitizen-tools.github.io/commitizen/) in both local `commit-msg` hooks and CI commit-range checks.
- Before merge, rewrite local history via interactive rebase as needed (fixup/squash) so commits are intentional.
- Commits on `main` must be meaningful and verified; e.g., avoid “wip”, “oops”, or “try again”.
- Local rebase or fast-forward integration must use commit signing. Verify the final commit range before pushing.

## Feature Flags

- Merge incomplete work behind feature flags when it helps keep branches short.
- Never break trunk to “finish later”.

## Repository Settings

- `main` is protected:
  - requires PRs
  - requires status checks
  - requires at least one review
  - requires linear history
  - requires signed commits
  - disallows force-pushes
- Enable squash merge.
- Disable merge commits.
- Disable GitHub web rebase merge unless maintainers explicitly operate a signed local fast-forward workflow and repository administrators accept the risk that the web UI cannot enforce it.
- Tag releases with annotated tags on `main`.
