# Versioning

Policy for versioning user-visible contracts.

## Contract surfaces

This policy covers documented user-visible behavior such as:

- CLI commands and flags.
- Configuration keys and validation behavior.
- Public API contracts.
- Exit-code or diagnostic behavior.

## Versioning model

The package starts at `0.1.0a1` and uses Semantic Versioning.

Until `1.0`, we adopt this compatibility model:

- patch releases for non-breaking fixes and additive changes that preserve existing documented behavior
- minor releases for intentional breaking changes or contract-affecting changes

After `1.0`, we use standard SemVer major versions for breaking changes.

## Change classification

Choose a patch release for:

- bug fixes that preserve documented contract intent
- additive behavior that is inert by default
- documentation clarifications with no runtime contract change

Choose a minor release for:

- removing or renaming stable CLI flags, config keys, or public API symbols
- changing documented behavior in a breaking way
- changing exit-code or diagnostic semantics in a breaking way

## Workflow linkage

This project uses Commitizen with Conventional Commits and changelog generation templates. Keep version bumps, changelog updates, and contract-documentation changes aligned in the same change.

If a public contract changes, update the relevant user or developer docs in the same pull request.
