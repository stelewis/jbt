---
description: Rewrites patchwork, stream-of-consciousness documentation into coherent reader-first pages and prevents incremental cruft from spreading across related files. Use when creating, editing, pruning, or sweeping docs that have accumulated appended caveats, duplication, stale transitions, or scattered ownership.
license: MIT
metadata:
    github-path: skills/doc-editing
    github-pinned: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-ref: 470c509674e569d69d75a8c5e21c2d22da44e395
    github-repo: https://github.com/stelewis/agent-skills
    github-tree-sha: 73a519a37d382fe95c4c3b978470c392801d7c57
name: doc-editing
---
# Documentation Editing

Treat documentation as a product surface, not an edit log. A maintenance change should leave each affected page reading as though it were written coherently today.

## Workflow

1. Define the audience, question the document must answer, and requested mode: create, edit, audit, prune, or sweep.
2. Read repository documentation standards and identify the canonical owner for the topic.
3. Read each affected page in full before editing it. Search adjacent docs for competing explanations rather than patching every file that mentions the changed concept.
4. Classify each document:
   - **Enduring guide or reference:** current contracts, concepts, and workflows.
   - **ADR:** decision, context, alternatives, and consequences.
   - **Plan or design document:** temporary intent, sequencing, and unresolved decisions.
   - **Changelog:** release impact supported by history.
5. Define the desired final outline before applying changes. Verify factual claims against code, configuration, tests, or other primary sources.
6. Use [references/anti-patchwork.md](references/anti-patchwork.md) to decide whether to rewrite a section or page instead of appending another local patch.
7. For an audit, classify each file as keep, rewrite or consolidate, or remove.
8. Move a shared rule to one owner, retain only page-specific consequences elsewhere, and link only when it saves meaningful search.
9. Delete obsolete files when removal is in scope; do not leave redirects or tombstones unless readers depend on the old path.
10. Reread every changed page from top to bottom, then run the narrowest repository-owned Markdown, link, or docs build check.

## Editing rules

- Lead with the contract, workflow, or answer.
- Keep non-obvious invariants, rationale, failure modes, and operator actions.
- Rewrite surrounding prose when a new fact changes the page's structure; do not bolt a caveat onto an obsolete explanation.
- Remove TODOs, milestone framing, change narration, repeated disclaimers, defensive repetition, and prose that restates code or filenames.
- Do not update every related file. Update the canonical owner and only the places whose reader task materially changes.
- Keep implementation detail only when the audience needs it to use, operate, or extend the system.
- Do not convert uncertainty into fact. Surface unresolved claims explicitly.
- Preserve repository terminology and style rather than imposing a generic documentation framework.

## Completion

For direct edits, summarize the owning question, material changes, moved or removed content, and validation. For audit-only work, provide file-specific decisions and the best long-term destination for content that should move.
