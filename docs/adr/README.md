# Architecture Decision Records

Use Architecture Decision Records (ADRs) to capture important engineering decisions, context, and consequences.

ADRs are enduring reference documents. Prefer stable contracts and boundaries over transient implementation details.

ADR files are discoverable by filename; no separate index is needed at this stage.

## Where ADRs live

ADRs live in `docs/adr/`.

## Naming

- Use the next available 4-digit numeric ID.
- Name files as: `NNNN-short-title.md`.

## Status workflow

ADRs follow this workflow:

- `draft` → `proposed` → `rejected`
- `draft` → `proposed` → `accepted` → `deprecated` or `superseded`

Notes:

- `draft`: under active authoring; not ready for team review.
- `proposed`: ready for review and feedback; not yet authoritative.
- `accepted`: approved and should be treated as the current decision.
- `rejected`: explicitly not going ahead (keep for history).
- `deprecated`: still true historically, but no longer recommended going forward.
- `superseded`: replaced by a newer ADR; use `superseded_by`.

## Creating a new ADR

1. Copy the template in `docs/adr/0000-template.md`.
2. Pick the next available ID and rename the file.
3. Fill in the YAML front matter.
4. Write the ADR sections.

Guidelines:

- Avoid issue/PR numbers and sprint-specific context.
- Avoid naming internal modules/classes unless they are part of a long-lived public contract.
- ADRs are read more often than they’re written: Context → Decision → Consequences → Alternatives puts the outcome and its impact before the rejected options.

## Metadata

ADRs include YAML front matter so the set can be indexed and validated later.

Required fields:

- `id`
- `title`
- `status`
- `date`

Optional fields:

- `tags`
- `supersedes`
- `superseded_by`
