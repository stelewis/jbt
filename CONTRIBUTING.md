# Contributing

`jbt` is in pre-alpha development. Small, focused changes with tests are welcome. Open an issue before proposing a new public API, source format, plugin surface, or CLI command so its contract can be established first.

## Test data

All committed fixtures and examples must be synthetic and portable. Do not submit real or lightly anonymized financial records. Fixtures must not contain personal names, account identifiers, credentials, local absolute paths, hostnames, or institution data copied from a real record.

## Checks

Run the local quality gates before opening a pull request:

```console
uv sync --all-groups
uv run ruff check .
uv run pytest
uv build
uv run twine check dist/*
```

By submitting a contribution, you agree that it is licensed under the Apache License, Version 2.0.
