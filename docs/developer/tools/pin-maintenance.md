# Pin Maintenance

Maintain frozen external refs as an explicit supply-chain workflow, not as incidental cleanup.

This guide covers:

- external GitHub Action refs in `.github/workflows/**` and `.github/actions/**`
- frozen pre-commit hook revs in `.pre-commit-config.yaml`
- scheduled drift reporting for those pinned refs

## Enforcement and visibility

This project uses three separate controls so frozen refs stay both strict and maintainable:

- `pinned-actions-policy.yml` fails if an external `uses:` ref is not pinned to a full commit SHA
- `frozen-pre-commit-policy.yml` fails if an external pre-commit hook `rev:` is not a full commit SHA
- `pinned-external-dependency-drift.yml` runs on a schedule, reports when pinned GitHub Action or pre-commit refs lag the latest upstream SemVer release tag, opens or refreshes a tracking issue on scheduled runs, and fails so drift stays visible

Dependabot remains the default update path for both GitHub Actions and pre-commit surfaces. Use manual rotation when you need an urgent update or when the drift issue reports a stale frozen ref.

## GitHub Actions rotation

Review the upstream release first. Frozen SHAs are only useful when the release behind the SHA is acceptable.

1. Read the release notes and confirm the source repository still meets the project trust bar.
2. Resolve the exact release tag to a commit SHA.
3. Update the `uses:` ref and the trailing version comment together.
4. If `.github/dependabot.yml` changed, keep the GitHub Actions coverage contract intact.
5. Let the pinned-actions workflow and CI validate the result.

Preferred edit shape:

- `uses: owner/repo@0123456789abcdef0123456789abcdef01234567 # v1.2.3`

Do not pin to moving tags such as `@v4` or branch names. Keep the human-readable version comment so later reviews do not need to reverse-resolve the SHA by hand.

## Pre-commit rotation

Use a frozen autoupdate flow so the file stays commit pinned.

1. Update the hook revs with `uv run --locked prek autoupdate --freeze`.
2. Review the hook changes and upstream release notes.
3. Run the relevant hooks locally with `uv run --locked prek run -a`.
4. Let the frozen-pre-commit policy workflow validate that every external hook remains commit pinned.

Preferred edit shape:

- `rev: 0123456789abcdef0123456789abcdef01234567  # frozen: v1.2.3`

Do not replace the frozen SHA with a tag. The version comment is documentation only; the SHA is the actual control.

## Responding to drift issues

The scheduled drift workflow opens or refreshes a single tracking issue titled `chore: review frozen external pins` when it detects lagging action or pre-commit refs.

When that issue appears:

1. Prefer the existing Dependabot PR if it already covers the reported dependency.
2. Rotate remaining stale refs manually using the steps above.
3. Re-run or wait for the drift workflow after merge so it can close the issue automatically.

If the workflow cannot resolve an upstream SemVer release tag, or cannot derive a supported GitHub remote for a pre-commit repo entry, treat that as a manual review task. Either update the dependency source, or document why the upstream release surface does not fit the repository's frozen-pin maintenance model.

## Review checklist

- Was the source repository reviewed as a dependency admission decision, not just as a version bump?
- Is the new ref pinned to a full 40-character commit SHA?
- Does the human-readable version comment match the intended upstream release?
- If `.github/dependabot.yml` changed, does the Dependabot coverage contract still hold?
- If `.pre-commit-config.yaml` changed, does every external hook still use the frozen `rev:` line shape filtered by `.betterleaks.toml`?
