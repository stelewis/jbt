# Package Initialization

Importing `package.module` executes `package/__init__.py`. Keep package initialization cheap and unsurprising.

## Find

- re-exports and `__all__` barrels
- imports of wiring, services, clients, CLIs, or frameworks
- environment, logging, network, registration, or singleton side effects
- callers importing a symbol from a package rather than its owning module

## Refactor

1. Identify the module that truly owns each exported symbol.
2. Update callers to import that module directly.
3. Remove re-exports and side-effect imports from `__init__.py`.
4. If a stable facade is a real public contract, create an explicitly named facade module rather than using package initialization.
5. Test imports that previously triggered cycles or side effects.

Do not retain package-level aliases solely for compatibility unless the repository has an explicit supported public API obligation.
