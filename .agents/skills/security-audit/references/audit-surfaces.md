# Security Audit Surfaces

## Runtime boundaries

- authentication, authorization, tenant separation, and privilege transitions
- CLI, HTTP, file, archive, environment, database, and generated input validation
- command construction, subprocess arguments, and shell invocation
- path traversal, symlink escape, unsafe extraction, and writes outside intended roots
- injection, unsafe deserialization, server-side request forgery, and output encoding
- secret handling in code, logs, errors, snapshots, fixtures, and telemetry
- cryptographic purpose, key lifecycle, randomness, and verification

## Dependencies and configuration

- necessity, ownership, release hygiene, install scripts, binaries, and transitive cost
- lock files, integrity controls, update coverage, and abandoned packages
- insecure defaults, debug behavior, exposed services, and fail-open configuration
- trust placed in generated artifacts or mutable external resources

## CI, hooks, build, and release

- token permissions, secret scope, environment approvals, and trigger trust
- untrusted expression interpolation and script injection
- third-party action provenance and immutable pinning
- artifact provenance, archive handling, caches, and cross-workflow trust
- local hooks and setup tools that download or execute code
- publication authority, signing, tags, and release credential lifetime
- self-hosted runner persistence and exposure to untrusted changes

## Agent and automation surfaces

- tool permissions, automatic approval, filesystem reach, and network reach
- prompt injection through repositories, issues, pull requests, docs, and web content
- skills, hooks, plugins, and MCP servers with hidden side effects
- commands or code assembled from model-generated or fetched text
- access to environment variables, credentials, user files, and paths outside the repository

For every in-scope category, record a finding or state why no material issue was established. Do not silently skip a trust surface.
