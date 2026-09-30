import json
import subprocess
import sys
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import byte_digest, decode_document, read_payload
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.snapshot import (
    BuildInput,
    SnapshotInput,
    assemble_snapshot,
    execution_fingerprint,
)
from jbt.contracts.catalog import catalog
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import configuration_schema
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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
    return SnapshotInput(
        tables={table.name: [] for table in catalog() if table.name != "build"},
        builds=[
            BuildInput(
                "E",
                "synthetic-producer",
                "2026-01-31",
                "a" * 64,
                is_dirty=False,
            ),
        ],
        metadata={
            "artifacts": [],
            "registries": [],
            "configuration": {
                "path": "configuration.json",
                "byte_digest": byte_digest(configuration),
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
        resources={"configuration.json": configuration},
    )


def test_complete_empty_snapshot_is_pinned_without_digest_cycle(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    result = assemble_snapshot(tmp_path, empty_record, settings)
    descriptor = decode_document(
        read_payload(
            tmp_path,
            "descriptor.json",
            result.descriptor_digest,
            maximum_bytes=100_000,
        ),
        "descriptor.json",
    )
    assert descriptor["manifest_digest"] == result.manifest_digest
    manifest_bytes = read_payload(
        tmp_path,
        "manifest.json",
        result.manifest_digest,
        maximum_bytes=100_000,
    )
    manifest = decode_document(manifest_bytes, "manifest.json")
    assert result.manifest_digest.encode() not in manifest_bytes
    assert manifest["logical_content_digest"] == result.financial_digest
    assert len(list(tmp_path.glob("*.parquet"))) == len(catalog())
    assert manifest["schema_version"] == "1"


def test_writer_encoding_changes_bytes_not_financial_content(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    one = assemble_snapshot(first, empty_record, settings)
    two = assemble_snapshot(
        second,
        empty_record,
        WriterSettings(
            compression=Compression.NONE,
            row_group_size=1,
            dictionary=True,
            statistics=False,
        ),
    )
    assert one.financial_digest == two.financial_digest
    assert one.manifest_digest != two.manifest_digest
    assert one.descriptor_digest != two.descriptor_digest
    first_manifest = decode_document(
        (first / "manifest.json").read_bytes(), "manifest.json"
    )
    second_manifest = decode_document(
        (second / "manifest.json").read_bytes(), "manifest.json"
    )
    first_entities, second_entities = (
        first_manifest["entities"],
        second_manifest["entities"],
    )
    assert isinstance(first_entities, list)
    assert isinstance(second_entities, list)
    assert (
        first_entities[0]["execution_fingerprint"]
        != second_entities[0]["execution_fingerprint"]
    )


def test_nonempty_directory_is_not_overwritten(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    previous = tmp_path / "descriptor.json"
    previous.write_bytes(b"previous")
    with pytest.raises(ValueError, match="empty ordinary"):
        assemble_snapshot(tmp_path, empty_record, settings)
    assert previous.read_bytes() == b"previous"


def test_retained_schema_constraints_change_financial_identity(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    schema = configuration_schema()
    schema["properties"]["as_of"]["const"] = "2026-01-31"
    monkeypatch.setattr(
        "jbt.artifacts.snapshot.configuration_schema",
        lambda: schema,
    )
    changed = assemble_snapshot(second, empty_record, settings)
    assert baseline.financial_digest != changed.financial_digest
    first_manifest = json.loads((first / "manifest.json").read_bytes())
    second_manifest = json.loads((second / "manifest.json").read_bytes())
    assert (
        first_manifest["entities"][0]["execution_fingerprint"]
        != (second_manifest["entities"][0]["execution_fingerprint"])
    )
    assert (first / "transactions.parquet").read_bytes() == (
        second / "transactions.parquet"
    ).read_bytes()


def test_execution_only_facts_do_not_change_financial_identity(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    changed = replace(
        empty_record,
        builds=[
            replace(
                empty_record.builds[0],
                producer_version="another-producer",
                input_fingerprint="b" * 64,
                is_dirty=True,
            ),
        ],
        metadata={
            **empty_record.metadata,
            "toolchain": [
                {
                    "component": "another-producer",
                    "version": "2",
                    "content_digest": byte_digest(b"another-producer"),
                },
            ],
        },
    )
    result = assemble_snapshot(second, changed, settings)
    assert baseline.financial_digest == result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest
    assert baseline.descriptor_digest != result.descriptor_digest


def test_source_bindings_change_financial_identity(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    changed = replace(
        empty_record,
        metadata={
            **empty_record.metadata,
            "bindings": [
                {
                    "entity_id": "E",
                    "source_scope_id": "synthetic-source",
                    "importer_id": "synthetic-importer",
                    "account_mappings": [],
                }
            ],
        },
    )
    result = assemble_snapshot(second, changed, settings)
    assert baseline.financial_digest != result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest


def test_binding_enumeration_does_not_change_financial_identity(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    bindings = [
        {
            "entity_id": "E",
            "source_scope_id": scope,
            "importer_id": "synthetic-importer",
            "account_mappings": [],
        }
        for scope in ("source-a", "source-b")
    ]
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(
        first,
        replace(
            empty_record,
            metadata={**empty_record.metadata, "bindings": bindings},
        ),
        settings,
    )
    reordered = assemble_snapshot(
        second,
        replace(
            empty_record,
            metadata={**empty_record.metadata, "bindings": list(reversed(bindings))},
        ),
        settings,
    )
    assert baseline.financial_digest == reordered.financial_digest
    assert baseline.manifest_digest != reordered.manifest_digest


def test_missing_required_check_prevents_descriptor(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    metadata = dict(empty_record.metadata)
    metadata["outputs"] = [{"sink": "tabular", "required_checks": ["missing"]}]
    resources = dict(empty_record.resources)
    configuration = decode_document(
        resources["configuration.json"], "configuration.json"
    )
    configuration["outputs"] = metadata["outputs"]
    resources["configuration.json"] = encode_json(configuration, integer_strings=False)
    metadata["configuration"] = {
        "path": "configuration.json",
        "byte_digest": byte_digest(resources["configuration.json"]),
    }
    inputs = SnapshotInput(
        empty_record.tables,
        empty_record.builds,
        metadata,
        resources,
    )
    with pytest.raises(ContractError, match="required_check_missing"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert not (tmp_path / "descriptor.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("registries", [42]), ("checks", None), ("unknown", [])],
)
def test_invalid_metadata_fails_before_writing(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    field: str,
    value: object,
) -> None:
    metadata = dict(empty_record.metadata)
    metadata[field] = value
    inputs = SnapshotInput(
        empty_record.tables,
        empty_record.builds,
        metadata,
        empty_record.resources,
    )
    with pytest.raises(ContractError, match="snapshot_metadata"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


def test_extra_unlisted_resource_cannot_be_certified(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    inputs = SnapshotInput(
        empty_record.tables,
        empty_record.builds,
        empty_record.metadata,
        {**empty_record.resources, "extra.json": b"{}\n"},
    )
    with pytest.raises(ContractError, match="snapshot_resource_inventory"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "changed",
    [
        [("right", "a" * 64), ("left", "b" * 64)],
        [("left", "b" * 64), ("right", "a" * 64)],
        [("left", "a" * 64), ("right", "b" * 64), ("right", "b" * 64)],
    ],
)
def test_execution_identity_preserves_roles_order_and_multiplicity(
    settings: WriterSettings,
    changed: Sequence[tuple[str, str]],
) -> None:
    baseline = execution_fingerprint(
        stage="tabular",
        inputs=[("left", "a" * 64), ("right", "b" * 64)],
        settings=settings,
        producer_digest="c" * 64,
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        inputs=changed,
        settings=settings,
        producer_digest="c" * 64,
    )


def test_writer_configuration_and_code_change_execution_identity(
    settings: WriterSettings,
) -> None:
    baseline = execution_fingerprint(
        stage="tabular",
        inputs=[],
        settings=settings,
        producer_digest="a" * 64,
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        inputs=[],
        settings=settings,
        producer_digest="b" * 64,
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        inputs=[],
        settings=WriterSettings(
            compression=Compression.NONE,
            row_group_size=50,
            dictionary=True,
            statistics=False,
        ),
        producer_digest="a" * 64,
    )


def test_fresh_processes_preserve_snapshot_bytes(
    tmp_path: Path,
    empty_record: SnapshotInput,
) -> None:
    script = """
import json
import locale
import sys
from pathlib import Path
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.snapshot import BuildInput, SnapshotInput, assemble_snapshot

locale.setlocale(locale.LC_ALL, "")
document = json.load(sys.stdin)
inputs = SnapshotInput(
    tables=document["tables"],
    builds=[BuildInput(**row) for row in document["builds"]],
    metadata=document["metadata"],
    resources={name: text.encode() for name, text in document["resources"].items()},
)
settings = WriterSettings(
    compression=Compression.ZSTD, row_group_size=100,
    dictionary=False, statistics=True,
)
assemble_snapshot(Path.cwd(), inputs, settings)
"""
    document = {
        "tables": empty_record.tables,
        "builds": [asdict(build) for build in empty_record.builds],
        "metadata": empty_record.metadata,
        "resources": {
            name: payload.decode() for name, payload in empty_record.resources.items()
        },
    }
    snapshots = []
    table_items = list(empty_record.tables.items())
    for seed, locale_name in [("1", "C"), ("917", "C.UTF-8")]:
        destination = tmp_path / seed
        destination.mkdir()
        document = dict(reversed(list(document.items())))
        table_items.reverse()
        document["tables"] = dict(table_items)
        subprocess.run(  # noqa: S603 - fixed Python program and synthetic JSON input
            [sys.executable, "-P", "-c", script],
            input=json.dumps(document),
            text=True,
            capture_output=True,
            check=True,
            cwd=destination,
            env={"PYTHONHASHSEED": seed, "LC_ALL": locale_name, "PYTHONUTF8": "1"},
            timeout=30,
        )
        snapshots.append(
            {path.name: path.read_bytes() for path in destination.iterdir()},
        )
    assert snapshots[0] == snapshots[1]
