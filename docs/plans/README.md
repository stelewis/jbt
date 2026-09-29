# Plans

Use this folder for implementation plans: short, execution-ready documents that translate architecture decisions and goals into concrete tasks and verification steps.

Implementation plans should be actionable and time-bounded. Prefer stable contracts, service boundaries, and verification criteria over transient coding details.

Remove plans or refactor them into enduring developer docs once the feature is implemented. They are not long-lived artifacts.

## Where plans live

Plans live in this folder: `docs/plans/`.

## Naming

- Prefer kebab-case filenames: `<kebab-case-feature>.md`.
- If a plan is explicitly tied to a numbered delivery step, an optional numeric prefix is acceptable.

## Creating a new plan

1. Copy the template in `docs/plans/0000-template.md`.
2. Rename it to your plan name.
3. Fill in the YAML front matter.
4. Write the plan sections.

Guidelines:

- Keep the TL;DR concrete: what changes, where, and why.
- Link to relevant ADRs (in `docs/adr/`) when a plan is downstream of an architectural decision.
- Prefer short prose and tables; use small input/output examples when they make a contract or exit check clearer.

## Metadata

Plans include YAML front matter so the set can be indexed and validated later.

Required fields:

- `title`
- `date_created`
