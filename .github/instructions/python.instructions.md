---
name: Python
description: Python source conventions for clear APIs, documentation, imports, and refactoring.
applyTo: "**/*.py"
---

- Prefer clear names, types, and structure over explanatory comments or docstrings.
- Document only non-obvious contracts, side effects, invariants, units, and failure modes.
- Keep necessary docstrings concise, Google-style, and wrapped to 72 characters.
- Do not restate signatures, types, or visible control flow.
- Use modern annotation syntax supported by the project's minimum Python version.
- Use keyword-only parameters when they improve call-site clarity.
- Import symbols from their owning modules rather than package re-export hubs.
- Prefer root-cause refactors over convenience patches.
