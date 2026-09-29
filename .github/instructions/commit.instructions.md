---
name: Commit messages
description: Commit construction and Conventional Commit rules.
applyTo: "commit-message"
---

- Keep commits atomic and coherent.
- Use Conventional Commit format: `<type>(<optional-scope>): <subject>`.
- Use a scope only when it adds meaningful context.
- Use imperative mood and describe the outcome, not the filenames.
- Keep the subject and body concise.
- Use `Closes`, `Fixes`, or `Resolves` when the commit fully resolves an issue; use a plain issue reference otherwise.
- Do not hard-wrap any line in the commit message.
- Do not add `Co-authored-by` or sign-off trailers.
- Use one of: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, `ci`, `perf`, `style`, `revert`.
