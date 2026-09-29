# Contributing to jbt

Thanks for taking the time to help improve this project! Whether you're fixing a bug, improving documentation, adding a feature, or suggesting an idea, all contributions are welcome and appreciated.

To keep this project clean and easy to maintain, we aim for consistent workflows, high code quality, and clear documentation. The guide below is the quickest path to a successful contribution.

By contributing, you agree that your work is licensed under Apache-2.0.

## Getting started

1. Fork and clone this repository to your GitHub account.
2. Create a short, descriptive branch such as `feature/add-cli-command`, `fix/parser-bug`, or `docs/update-guide`.
3. Install the development environment and tools:

   ```sh
   uv sync --locked
   uv run --locked prek install
   ```

4. Make your changes and run the full quality gate locally:

   ```sh
   uv run --locked ruff format && uv run --locked ruff check --fix && uv check --locked && uv run --locked tq check && uv run --locked pytest -q
   ```

   CI runs the same checks on pull requests.

5. If you plan to use the signed local fast-forward exception instead of the default squash merge, clean up and sign your commit history before opening a PR. See the [Git workflow docs](docs/developer/standards/git.md) for the merge policy.

6. Open a pull request targeting `main` and request a review.

## Development documentation

Repository conventions are kept in `docs/developer`:

- **Code standards** - formatting, typing, packaging, and import rules.
- **Git workflow** - branch naming, commit message conventions, and PR guidelines.
- **Testing standards** - how to write tests, run them, and keep them modular.
- **Developer tools** - local commands, pre-commit hooks, CI checks, and automation.

Read or search the documents before starting larger changes; links in the [Developer docs index](docs/developer/index.md).

## Issues and pull requests

- **Search first.** Before opening a new issue, look through existing issues to avoid duplicates.
- **Issue types.** Use the Bug report, Feature request, and Question forms where they fit; use a blank issue if the topic does not fit cleanly into a form.
- **Pull requests.** Keep PRs small and focused. Target `main`. Every PR must pass all checks and receive at least one approving review before merging.
- **Commit messages.** Use [Conventional Commits](https://www.conventionalcommits.org/). A `commit-msg` hook enforces this; run `uv run --locked cz commit` if you want help formatting messages.

## Code style and tooling

- Formatting and linting are handled by [ruff](https://docs.astral.sh/ruff/).
- Type checking uses [ty](https://docs.astral.sh/ty/).
- Pre-commit hooks are managed by [prek](https://prek.j178.dev/).
- Tests run with `pytest`; test quality is checked with `uv run --locked tq check`.

## Documentation

Documentation in this repository is treated as first-class. See `docs/developer/standards/docs.md` for the durable rules.

- Keep docs useful, stable, concise, and small.
- Prefer one reference doc per concept and avoid duplication.
- Document contracts and workflows rather than implementation trivia.

If you update code with user-facing behavior, update the corresponding documentation and tests.

## Test data

Commit synthetic, portable fixtures only. Never commit real or lightly anonymized financial data.

## Support and communication

If you need help or have questions, open an issue with the Question template. For suspected vulnerabilities, follow `SECURITY.md` rather than creating a public issue.

Thanks again for contributing to `jbt`! We appreciate your effort in making the project better for everyone.
