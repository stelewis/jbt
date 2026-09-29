---
description: Refactors Python architecture around strong internal types, explicit inputs, cheap package imports, and observable failures. Use when Python code relies on stringly typed values, risky signature defaults, __init__.py re-export hubs, catch-all exceptions, or silent input coercion.
license: MIT
metadata:
    github-path: skills/python-code-quality
    github-pinned: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-ref: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-repo: https://github.com/stelewis/agent-skills
    github-tree-sha: 7f7cefed003030d5b1f17667660a1b7aa7cbe3c3
name: python-code-quality
---
# Python Code Quality

Use this skill for structural Python cleanup, not cosmetic linting.

## Workflow

1. Locate the smell and the architectural seam it crosses: input parsing, dependency wiring, import graph, or error surface.
2. Read the relevant guide:
   - [references/strong-types.md](references/strong-types.md)
   - [references/signature-defaults.md](references/signature-defaults.md)
   - [references/init-imports.md](references/init-imports.md)
   - [references/catch-all-errors.md](references/catch-all-errors.md)
3. Fix the seam rather than its symptoms.
4. Update every affected caller, serializer, test, and documented contract. Do not retain a parallel weak API.
5. Run the repository's formatter, linter, type checker, and narrowest relevant tests using repository-owned commands.

## Rules

- Parse and validate untrusted primitives once at a boundary.
- Keep core inputs typed, required, and explicit.
- Construct dependencies at composition roots.
- Import from owning modules; keep package initialization cheap.
- Make unexpected states and failures observable.
- Prefer a focused type or error module when it creates clear ownership; do not create a generic utility bucket.

Report the smell, root-cause change, affected contracts, tests changed, and validation status.
