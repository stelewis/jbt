# Developer Tools

## Guides

- [Local workflows](./local-workflows.md)
- [CI and automation](./ci.md)
- [Pin maintenance](./pin-maintenance.md)

## Core commands

Setup:

- `uv sync --locked`
- `uv run --locked prek install`

Development:

- `uv run --locked ruff format`
- `uv run --locked ruff check --fix`
- `uv check --locked`
- `uv run --locked tq check`
- `uv run --locked pytest -q`
- `uv run --locked prek run -a`
