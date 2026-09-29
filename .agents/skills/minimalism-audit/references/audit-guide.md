# Minimalism Audit Guide

## Priority order

1. Always-loaded repository and user instructions.
2. Skill names and descriptions loaded for discovery.
3. Frequently activated skills, agents, and tool definitions.
4. Repeated standards, templates, and reference documents.
5. Comments and docstrings.

## What to inspect

### Instructions

- generic engineering advice unrelated to repository-specific constraints
- task workflows embedded in always-on context
- repeated philosophy and validation commands
- broad file globs for rules that apply narrowly

### Skills, prompts, and agents

- overlapping activation descriptions
- prompt files that should be skills or are obsolete for the active host
- skills that merely restate an obvious task
- separate agents without a real tool, model, or isolation requirement
- host-specific tool wrappers that should be a capability or MCP integration
- unreferenced resources and deep reference chains

### Documentation and templates

- several files owning the same rule
- plans or status notes presented as enduring documentation
- implementation narration, exhaustive navigation, and stale examples
- mirrored files that drift because generation or installation should replace copying

### Code explanation

- comments narrating visible control flow
- docstrings repeating signatures, types, or names
- prose compensating for unclear identifiers or structure

## Remediation patterns

- **Duplicate guidance:** keep one owner; delete the rest or retain a short boundary-specific consequence.
- **Broad instructions:** retain repository-wide policy and move task workflows into on-demand skills.
- **Bloated entrypoint:** keep dispatch and decisions in `SKILL.md`; move detailed checklists to one-level references.
- **Prompt proliferation:** merge around a stable workflow or remove tasks that normal prompting already expresses.
- **Template drift:** distribute from one canonical source rather than maintaining mirrors.
- **Explanatory code noise:** improve names or structure, then remove redundant prose.

After each change, verify that the same task remains possible and that no canonical guidance was lost.
