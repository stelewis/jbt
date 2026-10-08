# Contract Boundary Dependencies

The contract-conformance work uses one Parquet writer and an independent query engine. Dependency versions are resolved in `uv.lock`; changes require the [supply-chain review](../standards/supply-chain-security.md), the real boundary tests, and a locked dependency audit.

| Dependency | Owner and license | Role and trust cost |
| --- | --- | --- |
| PyArrow | Apache Arrow / Apache Software Foundation; Apache-2.0 | Runtime Parquet encoding with explicit schemas. Native C++ distribution, with no additional mandatory Python dependencies. Avoid pandas and inferred financial types. |
| jsonschema | Python JSON Schema organization; MIT | Runtime Draft 2020-12 structural validation. Its required tree is attrs, jsonschema-specifications, referencing, and rpds-py; the last includes native code. Do not enable format extras or network schema retrieval. Relational financial checks remain owned by `jbt`. |
| DuckDB | DuckDB Foundation; MIT | Development-only independent Parquet reader and numeric interoperability checks. Native engine with no mandatory Python dependencies. Disable automatic extension installation/loading and external access; allow only the specific local fixture directory when reading Parquet. |
| NumPy | NumPy project; BSD-3-Clause with bundled permissive licenses | Development-only prerequisite exposed by the real DuckDB Python UDF registration probe. Native distribution with no mandatory Python dependencies. Financial UDF arguments/results still use strings and integer scales, never NumPy or binary floating-point arithmetic. |
| Beancount | Martin Blais / Beancount; GPL-2.0-only | Runtime validation of the enabled ledger output through its real loader, imported only at the output boundary. Required Python dependencies are click, python-dateutil, regex, and dateutil's six dependency. It validates a projection; it does not choose upstream financial facts. |

The selected releases provide CPython 3.14 wheels for macOS and Linux on x86-64 and ARM64. Wheel availability is installation evidence, not a claim that all platforms have been tested. Native parser/reader dependencies process untrusted data and must remain patched; developer/CI versions are locked and reviewed. Reputation and a clean advisory scan do not prove absence of defects.

Provisioning and `uv`'s dependency-security checks may access package registries and OSV. That is separate from the offline financial computation contract. Do not disable the malware check merely to make `uv run --offline` work with an empty advisory cache.

Use the standard commands:

```bash
uv sync --locked
uv audit --locked
uv run --locked pytest -q tests/integration/conformance
```

No database server, dbt installation, Arrow flight service, remote filesystem, schema server, or Beancount plugin is required. A consumer reads versioned files rather than importing producer implementation code.
