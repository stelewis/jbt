---
description: Curates release-ready changelog entries from repository evidence and generated notes. Use when drafting, reviewing, or tightening a changelog or release section, especially when a changelog generator produces noisy commit-level output.
license: MIT
metadata:
    github-path: skills/changelog-curation
    github-pinned: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-ref: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-repo: https://github.com/stelewis/agent-skills
    github-tree-sha: 365c70d67d767a3d39d7e9708be3984631c01211
name: changelog-curation
---
# Changelog Curation

Produce a useful release narrative, not a dump of commit subjects.

## Workflow

1. Read the repository's versioning, release, and changelog conventions.
2. Determine the release range and target section. Do not guess a version or date when the repository has not established one.
3. Inspect the commits and changed files in that range.
4. If the repository owns a changelog generator, run its non-mutating or dry-run mode and treat the output as evidence, not final prose.
5. Group changes by user or operator impact. Collapse commits that implement one outcome.
6. Edit the canonical changelog directly unless the user requested draft text only.
7. Check every entry against the diff or other primary evidence.

## Keep

- behavior and public contract changes
- security and dependency posture changes
- contributor, CI, release, or operational workflow changes
- fixes whose effect is not clear from the commit subject
- removals or migrations that change what users can rely on

## Omit

- mechanical churn, intermediate refactors, and generated-file noise
- duplicate bullets for facets of the same outcome
- implementation detail without reader impact
- claims that cannot be supported by repository evidence

Follow the repository's existing format. If none exists, use the smallest structure that communicates the release clearly; do not impose a changelog framework solely for this task.

Write entries in release-note voice, with consistent tense and precise impact. Preserve breaking-change and migration information even when brevity would otherwise remove it.

## Completion

Report:

- the release range and evidence used
- the sections or entries changed
- omitted or consolidated material worth calling out
- any uncertainty caused by incomplete history or missing release metadata
