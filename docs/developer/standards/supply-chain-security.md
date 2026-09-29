# Supply-Chain Security

Every dependency, GitHub Action, hook, build tool, AI-agent tool, and release step expands the trust boundary. The default posture is strict: prefer less code, fewer dependencies, fewer permissions, and fewer automation surfaces.

## Threat model

- Untrusted inputs e.g., CLI arguments, configuration, environment variables, third-party content fetched from the web or external APIs.
- Supply-chain surfaces e.g., dependencies, GitHub Actions, hooks, build tools, release tooling, and any AI-agent skills, prompts, or MCP servers.
- Vulnerabilities that enable unintended code execution, privilege escalation, secret exposure, path traversal, tampering, or trust expansion.
- Vulnerabilities that exploit AI models through prompt injection, credential exposure, risky downloads, or unverifiable dependencies.

## Default rules

- Treat every new dependency or automation surface as a security decision.
- Prefer mainstream, well-maintained tools with clear ownership and a long track record.
- Prefer first-party or widely trusted GitHub Actions.
- Reject low-trust, low-rigor, or weakly maintained packages and actions by default.
- Prefer owning a small amount of clear local code over adding a marginal dependency.
- Remove unused dependencies, workflows, hooks, and credentials promptly.
- Require MFA for privileged maintainers and keep repository permissions on least privilege.
- Protect main and release branches with required review and status checks.
- If the project adds agent skills, prompts, or MCP servers, review them as supply-chain inputs too.

## Required review for new trust relationships

Before adding or materially changing a dependency, GitHub Action, hook, or release tool, review:

- necessity: why existing code or approved tooling is insufficient
- provenance: owner, maintainer continuity, release hygiene, and adoption
- transitive impact: dependency tree size, install scripts, binary delivery, and lockfile changes
- permissions: repository, token, filesystem, and network access the tool receives
- maintenance fit: update cadence, support posture, and compatibility with existing tooling

If the trust case is weak, do not add it.

## Project expectations

- Keep dependency updates automated and reviewable.
- Keep Actions and local actions covered by Dependabot.
- Keep secret scanning, dependency auditing, and security linting enabled in local hooks and CI.
- Keep static analysis and security checks early in the workflow so issues are found before release.
- Keep workflow permissions minimal and explicit.
- Prefer explicit version pinning, immutable references where available, and small, auditable workflow graphs.
- Do not introduce optional tooling surfaces without a clear payoff.

## Exceptions

- Exceptions should be rare.
- If an exception is approved, document the accepted risk, compensating controls, and exit path.
- If the exception creates a durable trust or automation posture, capture it in an ADR as well as the pull request.

## Existing enforcement

- Dependabot covers Python dependencies, pre-commit hooks, GitHub workflows, and local composite actions.
{% if enable_codeql -%}
- Dependency review runs on pull requests that change package manifests, lockfiles, or workflow dependencies.
{% endif -%}
- Betterleaks scans staged changes before commit and repository history in CI. GitHub secret scanning and push protection should also be enabled where available: their remote history scanning, provider validation, and revocation workflows complement rather than replace the local and CI controls.
- uv checks the locked resolution for known malware before every dependency sync. Bandit and lock-native `uv audit` checks run in CI.
- Lockfiles and minimal workflow permissions are part of the review surface.

## Pull request bar

Any pull request that adds or materially changes an external dependency or automation surface should explain:

- why it is needed
- what alternatives were rejected
- why the chosen tool is trustworthy
- what transitive and permission impact it adds
- what validation was run

Passing scanners is necessary, not sufficient. Reputation, maintenance history, ownership, and security posture are important as well.
