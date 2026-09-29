# Documentation Standards

Write for an intelligent reader starting with fresh context. Give them what they need to make correct decisions without reconstructing a conversation. Keep the surface small, accurate, and easy to maintain; optimize for understanding, not word count alone.

## Reader contract

Lead with the answer or responsibility. Use plain English and precise domain terms. Preserve distinctions that affect behavior, identity, ownership, arithmetic, or failure handling.

Describe the intended system directly. Put unresolved decisions beside the contract they affect, naming the evidence needed and the work they block. Keep non-obvious rationale and rejected alternatives when they prevent a plausible mistake.

Remove reviewer narration, drafting history, and generic disclaimers or warnings. Rewrite obsolete explanations instead of appending caveats.

## Ownership and lifetime

| Location | Owns |
| --- | --- |
| `docs/user` | Product use and operational workflows |
| `docs/developer` | Contribution guidance and implemented architecture and contracts |
| `docs/adr` | Decisions, their material alternatives, and consequences |
| `docs/design` | Intended architecture, contracts, acceptance behavior, and unresolved design questions |
| `docs/plans` | Implementation sequence and evidence gates |

Each page should answer a concrete reader question. Give each contract one canonical owner; do not repeat its explanation across pages. Link when it saves a search, not just because another page is related. Split by reader task or responsibility, and consolidate splits that force needless navigation.

Keep intended contracts and acceptance cases available while planned work depends on them. As features ship, move enduring implemented contracts to developer references, updating links in the same change. Retire design docs and completed plans only after their still-relevant meaning has an owner; do not keep competing specifications.

## Examples

Use the smallest synthetic example that distinguishes correct behavior from a plausible mistake. State inputs and policy, exact expected outputs or failure, and the invariant demonstrated. Include a counterexample when the happy path could pass a wrong implementation.

Label projections and omitted fields so partial rows are not mistaken for valid fixtures. Prefer durable relationships over internal types or incidental class names. The [contract-first testing workflow](./tests.md#contract-first-slices) owns how examples become independently checked tests.

## Maintenance

Document contracts and workflows, not facts readily inferred from code, temporary status snapshots, issue numbers, or internal event lists that are not public contracts. Revisit pages when the behavior they describe changes; remove stale details rather than preserving them as history.

When shortening a contract, account for its requirements, rationale, failure modes, and decisive examples: retain them, move them to a named owner, or deliberately change them. Keep review evidence out of the finished page. Check that a fresh reader can still derive the required outcome and diagnose the counterexample; search adjacent owners for contradictions and broken links.

Do not hard-wrap Markdown source. Run repository-owned documentation checks; do not add tooling merely to measure prose.
