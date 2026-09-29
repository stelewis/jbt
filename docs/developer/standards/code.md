# Code Standards

Use these standards to keep code correct, maintainable, and easy to evolve.

Goals:

- **Correctness first**: prevent silent failures and ambiguous behavior.
- **Design excellence**: optimize for long-term maintainability, extensibility, and clarity.
- **Small surface area**: minimize refactor blast-radius; avoid speculative abstraction.

## Principles

- **SOLID / separation of concerns**: each unit has one reason to change; policies don’t depend on details.
- **Explicitness / strictness**: make dependencies and contracts visible; fail fast at boundaries.
- **Cleanliness**: prefer simple shapes, clear naming, and small functions.
- **Minimalism (YAGNI)**: implement today’s requirement cleanly; do not pre-build optional futures.
- **Extensibility without fragility**: enable new features by adding new code paths, not by editing many unrelated ones.

## Architecture Rules

- **Composition root**: construct the object graph in one place (CLI entrypoint / app bootstrap). No hidden construction inside domain logic.
- **Dependency injection**: pass dependencies explicitly (constructors / factory functions), not via global state or implicit defaults.
- **Boundaries are strict**: adapters convert formats; they do not guess intent or silently coerce.
- **Domain stays pure**: core logic should not know about IO, filesystem, environment variables, subprocesses, or external SDKs.
- **Requests go down, results come up**: interface and orchestration layers send validated commands inward; domain layers return decisions, derived data, and results outward. Lower layers do not call back into higher layers for policy or side effects.
- **Layering is one-way**: interface, CLI, and adapter layers may depend on domain modules, but domain modules must not depend on transport, persistence, VCS, or UI concerns.
- **Orchestration stays thin**: workflow code coordinates boundary calls and domain operations, but business rules and validation logic stay in well-named domain units.

## Security

- **Validate and fail closed**: reject invalid CLI, config, filesystem, archive, and environment input at the boundary.
- **Constrain side effects**: validate paths, keep filesystem and subprocess use at the edges, and pass explicit argv to child processes.
- **Defend against traversal and boundary escape**: explicitly consider `..`, symlink, absolute-path, and root-escape cases.
- **Protect secrets**: never hardcode, commit, log, or snapshot live credentials.
- **Keep diagnostics safe**: logs and errors should be actionable without leaking sensitive data.
- **Structured process execution**: pass explicit argument arrays and validated inputs to subprocesses.

## Antipatterns to Avoid

- **Re-export hubs in package `__init__.py` files**: creates unstable import graphs and hides ownership.
- **Catch-all error handling that loses signal**: swallowing exceptions or returning sentinel values in critical loops.
- **Stringly-typed identifiers / closed vocabularies**: raw strings drifting through core logic for IDs, enums, and state.
- **Defaults in function signatures that hide behavior**: implicit deps or "magic" config weaken contracts and tests.
- **Silent defaults in runtime models/config**: defaulting missing/invalid fields instead of failing fast (defaults belong in the composition root or boundary config).
- **Compatibility coercion**: do not auto-upgrade legacy shapes at runtime; fix the boundary inputs.
- **"Forever fixtures" mindset**: fixtures are not a compatibility promise; when schemas change, regenerate/update fixtures.
- **Blind lint-rule compliance**: do not contort otherwise clear code to satisfy linting heuristics; align with the rule intent and use scoped exceptions when needed.

## Preferred Patterns

- **Fail-fast contracts**: validate inputs at boundaries; raise actionable errors with file/line context.
- **Strong internal types**: use dedicated types for identifiers and vocabularies; keep conversion at edges.
- **Explicit imports**: prefer importing exact module paths; keep dependency graphs readable and cycle-resistant.
- **Schema evolution by version bump**: change runtime schemas intentionally and update fixtures/tests accordingly.
- **Structured error variants**: errors crossing module or service boundaries should be a closed set of named variants with structured context (Rust-`enum`-style). In Python, prefer distinct exception or result types over free-form strings and catch-all exceptions.
- **No runtime migrations / backward compatibility**: if a schema or contract changes, break intentionally and update the callers/fixtures rather than carrying adapters in core code.
- **Narrow interfaces**: depend on small protocols/ABCs that model *what you need*, not the full dependency.
- **Local reasoning**: keep functions small and side-effect-free where possible; push side effects to the edges.
- **Derived data flows outward**: the core returns authoritative decisions and records; projections, views, and exports are rebuilt from that authoritative state at the edges rather than mutating it.
- **Clear naming**: choose names that express intent and domain meaning.
  - Nouns for types, verbs for actions: classes/types are nouns; functions/methods are verbs.
  - Booleans as predicates: use `is_*`, `has_*`, `can_*`, `should_*`.
  - Collections plural: name collection variables in plural (e.g., `orders`).
  - Prefer specific names over generic ones (`order_id` > `id`; `runner_config` > `config`).
  - Use one canonical name per concept; avoid synonym drift (e.g., `slug` vs `id`).

## Review Checklist

- Are dependencies constructed in a composition root, not inside core logic?
- Are boundary adapters strict (no silent coercion), with good error messages?
- Do dependencies only point inward, with no domain import of higher-layer transport or orchestration modules?
- Are requests driven into the domain and results returned outward, rather than lower layers reaching upward for side effects?
- Are defaults explicit and located at the edges (not silently applied inside models/core logic)?
- If schemas/contracts changed, did we bump/update callers and fixtures instead of adding runtime compatibility?
- Is the code minimal (no unused abstractions), and the refactor surface area contained?
