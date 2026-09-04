# jbt

`jbt` is an early-stage, dbt-like data-ingestion pipeline for Plain Text Accounting. It is being designed as a reproducible build system for financial records: archived financial sources and versioned human decisions are turned into reproducible ledgers and other disposable outputs.

The project is in pre-alpha development and is not ready for use with real financial records. The first release contains only foundational deterministic artifact utilities. It does not yet provide the `jbt` command or an ingestion pipeline.

## Current scope

The initial package provides internal core primitives for:

- canonical serialization without binary floating point;
- deterministic SHA-256 content digests; and
- versioned artifact envelopes.

These foundations are intentionally small. User-facing formats and APIs will be added only as the design and synthetic corpus establish their contracts.

## Development

The project is developed with Python 3.14 and supports Python 3.12 or later. With [uv](https://docs.astral.sh/uv/) installed:

```console
uv sync --all-groups
uv run pytest
uv run ruff check .
uv build
uv run twine check dist/*
```

## Security

Do not use real financial records in bug reports or public test cases. See [SECURITY.md](SECURITY.md) for private vulnerability reporting guidance.

## License

Licensed under the Apache License, Version 2.0.
