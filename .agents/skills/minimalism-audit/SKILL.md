---
description: Audits and reduces context bloat, duplication, and stale explanatory surface across instructions, skills, agents, prompts, MCP configuration, docs, templates, comments, and docstrings. Use to lower always-loaded context cost or simplify an overgrown customization system.
license: MIT
metadata:
    github-path: skills/minimalism-audit
    github-pinned: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-ref: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-repo: https://github.com/stelewis/agent-skills
    github-tree-sha: a3ec46acc45004534fb8e941928e1404da97b75f
name: minimalism-audit
---
# Minimalism Audit

Reduce context and maintenance cost without deleting contracts that cannot be recovered cheaply from code or structure.

## Workflow

1. Define the scope and whether the user wants findings, edits, or both.
2. Inventory the surfaces in [references/audit-guide.md](references/audit-guide.md).
3. Review the highest-cost surfaces first: always-loaded instructions, discovery metadata, and frequently activated customizations.
4. For each item, ask:
   - Is it non-obvious and durable?
   - Can a capable agent infer it from code, types, names, or repository structure?
   - Does this location own the rule?
   - Would deletion materially reduce correctness?
5. Choose one action: keep, shorten, narrow scope, consolidate, move detail behind progressive disclosure, replace with a canonical dependency, or delete.
6. Apply direct edits when requested. Update links and remove abandoned duplicates in the same change.
7. Recheck discovery, references, and repository-owned validation after structural changes.

## Principles

- Context is a shared runtime budget.
- Prefer one strong rule over repeated reminders.
- Keep descriptions precise because every skill description is discovery tax.
- Keep entrypoints short; move genuinely useful detail to one-level references.
- Delete generic advice, decorative examples, and prose that restates visible structure.
- Do not move verbosity sideways into another file.
- Do not sacrifice a security boundary, invariant, exact output contract, or non-obvious failure mode for token count.

## Output

Report scope, findings by context and maintenance impact, changes applied, measured reductions where practical, validation, and any important information deliberately retained.
