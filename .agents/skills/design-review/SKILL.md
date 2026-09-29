---
description: Reviews and improves code, tests, configuration, and automation for coherent ownership, strict boundaries, and low accidental complexity. Use for architecture reviews, root-cause refactors, simplification, YAGNI checks, or removal of legacy and duplicate paths.
license: MIT
metadata:
    github-path: skills/design-review
    github-pinned: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-ref: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-repo: https://github.com/stelewis/agent-skills
    github-tree-sha: 47e9f3dad3c6e01ba04f29e6a75d034aa2abe54a
name: design-review
---
# Design Review

Review design quality rather than syntax or style. The goal is a smaller, clearer system whose contracts and ownership are easier to explain.

## Workflow

1. Establish the scope, intended behavior, and whether the user wants findings, fixes, or both.
2. Read repository instructions and the smallest set of architecture and standards documents that govern the scope.
3. Trace the owning boundary, data flow, side effects, and tests before judging local code.
4. Apply the lenses in [references/review-guide.md](references/review-guide.md). Record only issues supported by concrete evidence.
5. Rank findings by correctness risk, blast radius, and ongoing maintenance cost.
6. Choose a root-cause response: delete, consolidate, split responsibilities, move parsing or defaults to a boundary, make dependencies explicit, or make invalid states unrepresentable.
7. When fixes are in scope, update all affected callers, tests, docs, and automation. Do not leave a parallel legacy path.
8. Run the narrowest repository-owned checks that cover the changed contract.

## Decisions

- Optimize for the smallest coherent design, not the smallest diff.
- Prefer one canonical owner and one vocabulary for each concept.
- Keep core logic deterministic and side effects at explicit boundaries.
- Reject abstractions that only forward calls, rename concepts, or preserve obsolete compatibility.
- Do not demand a pattern merely because it is conventional. A new layer must remove more complexity than it adds.
- Distinguish a real public compatibility obligation from code that merely happens to exist.

For proposed issues or plans, first determine whether the current system already meets the need, whether the need is evidenced, and what the minimum viable design is. Recommend closing or narrowing work that has no justified outcome.

Use a security-focused review for exploitable trust-boundary problems. Use a minimalism review when the dominant cost is prompt, instruction, or documentation surface.

## Output

State:

- scope and assumptions
- findings in priority order, each with location, evidence, impact, and root-cause action
- fixes applied
- validation performed
- unresolved risks or follow-up work

If no material issue is found, say so and identify what was not validated.
