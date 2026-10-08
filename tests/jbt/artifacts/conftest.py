import pytest

from jbt.artifacts.execution import ExecutionFacts, Producer
from jbt.artifacts.integrity import byte_digest
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.snapshot import BuildInput, SnapshotInput
from jbt.contracts.catalog import catalog
from jbt.domain.canonical import encode_json


@pytest.fixture
def settings() -> WriterSettings:
    return WriterSettings(
        compression=Compression.ZSTD,
        row_group_size=100,
        dictionary=False,
        statistics=True,
    )


@pytest.fixture
def empty_record() -> SnapshotInput:
    configuration = encode_json(
        {
            "schema_version": 1,
            "as_of": "2026-01-31",
            "source_authority": [],
            "rendering": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "declaration_source_order": [],
        },
        integer_strings=False,
    )
    definitions = encode_json(
        {"schema_version": 1, "definitions": {}}, integer_strings=False
    )
    return SnapshotInput(
        tables={table.name: [] for table in catalog() if table.name != "build"},
        builds=[
            BuildInput(
                "E",
                "synthetic-producer",
                "2026-01-31",
                "a" * 64,
            ),
        ],
        metadata={
            "artifacts": [],
            "registries": [],
            "configuration": {
                "path": "configuration.json",
                "byte_digest": byte_digest(configuration),
            },
            "derivation_definitions": {
                "path": "derivations.json",
                "byte_digest": byte_digest(definitions),
            },
            "effective_declarations": [],
            "bindings": [],
            "recovery_set": {
                "vault_objects": [],
                "accession_records": [],
                "authored_history": [],
                "dependencies": [],
                "external_assets": [],
                "runtime": [
                    {
                        "runtime_id": "synthetic-runtime",
                        "version": "1",
                        "platform": "synthetic-platform",
                        "path": "runtime.bin",
                        "byte_digest": byte_digest(b"synthetic-runtime"),
                    },
                ],
            },
            "inputs": [],
            "toolchain": [
                {
                    "component": "synthetic-producer",
                    "version": "1",
                    "content_digest": byte_digest(b"synthetic-producer"),
                },
            ],
            "derivations": [],
            "capabilities": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "checks": [],
        },
        resources={
            "configuration.json": configuration,
            "derivations.json": definitions,
        },
        execution=ExecutionFacts(
            Producer("synthetic-producer", "1", byte_digest(b"synthetic-producer")),
            byte_digest(b"synthetic-runtime"),
        ),
        writer_version="synthetic",
    )
