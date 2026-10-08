import json
import subprocess
import sys
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.execution import (
    ExecutionFacts,
    InputEdge,
    Producer,
    execution_fingerprint,
    stage_envelope,
)
from jbt.artifacts.integrity import byte_digest, decode_document, read_payload
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.reader import read_snapshot
from jbt.artifacts.snapshot import (
    OutputPayload,
    SnapshotInput,
    assemble_snapshot,
)
from jbt.contracts.catalog import catalog
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import configuration_schema
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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
    assert (tmp_path / "tabular_schema.json").is_file()
    assert not (tmp_path / "schema.json").exists()
    schemas = manifest["schemas"]
    assert isinstance(schemas, list)
    assert any(
        isinstance(entry, dict)
        and entry.get("schema_id") == "tabular_schema"
        and entry.get("path") == "tabular_schema.json"
        for entry in schemas
    )


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


def test_rendering_schema_constraints_do_not_change_financial_identity(
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
    schema["properties"]["rendering"]["items"]["properties"]["positive_name"][
        "const"
    ] = "synthetic"
    monkeypatch.setattr("jbt.artifacts.snapshot.configuration_schema", lambda: schema)
    result = assemble_snapshot(second, empty_record, settings)
    assert baseline.financial_digest == result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest


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
        execution=ExecutionFacts(
            Producer("another-producer", "2", byte_digest(b"another-producer")),
            empty_record.execution.execution_digest,
        ),
    )
    result = assemble_snapshot(second, changed, settings)
    assert baseline.financial_digest == result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest
    assert baseline.descriptor_digest != result.descriptor_digest


def test_execution_record_changes_do_not_change_financial_identity(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    document = {
        "schema_version": 1,
        "producer": {
            "kind": "development",
            "name": "jbt",
            "version": "1",
            "source_commit": None,
        },
        "runtime": {
            "implementation": "cpython",
            "version": "3.14",
            "platform": "synthetic",
            "architecture": "synthetic",
        },
        "distributions": [],
    }

    def record(distributions: list[dict[str, str]]) -> SnapshotInput:
        content = encode_json(
            {**document, "distributions": distributions}, integer_strings=False
        )
        return replace(
            empty_record,
            execution=replace(
                empty_record.execution, execution_digest=byte_digest(content)
            ),
            output_payloads=[
                OutputPayload(
                    "execution",
                    "execution.json",
                    content,
                    byte_digest(content),
                    "execution_record_schema",
                )
            ],
        )

    baseline = assemble_snapshot(first, record([]), settings)
    result = assemble_snapshot(
        second, record([{"name": "extra", "version": "2"}]), settings
    )
    assert baseline.financial_digest == result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest
    for directory, publication in ((first, baseline), (second, result)):
        snapshot = read_snapshot(directory, publication.descriptor_digest)
        assert "execution.json" in snapshot.outputs
        assert (
            decode_document(snapshot.outputs["execution.json"], "execution.json")[
                "schema_version"
            ]
            == 1
        )
        assert snapshot.manifest["logical_content_digest"] == baseline.financial_digest


def test_opaque_outputs_are_inventoried_without_affecting_financial_content(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    ledger = b"2026-01-31 Synthetic ledger\n"
    summary = b"not even JSON"
    result = assemble_snapshot(
        second,
        replace(
            empty_record,
            output_payloads=(
                OutputPayload(
                    "ledger", "ledger.beancount", ledger, byte_digest(ledger)
                ),
                OutputPayload("summary", "summary.json", summary, byte_digest(summary)),
            ),
        ),
        settings,
    )
    assert result.financial_digest == baseline.financial_digest
    manifest = decode_document((second / "manifest.json").read_bytes(), "manifest.json")
    descriptor = decode_document(
        (second / "descriptor.json").read_bytes(), "descriptor.json"
    )
    artifacts = manifest["artifacts"]
    payloads = descriptor["payloads"]
    assert isinstance(artifacts, list)
    assert isinstance(payloads, list)
    assert {entry["path"] for entry in artifacts if isinstance(entry, dict)} == {
        "ledger.beancount",
        "summary.json",
    }
    assert {entry["path"] for entry in payloads if isinstance(entry, dict)} >= {
        "ledger.beancount",
        "summary.json",
    }


def test_output_selection_is_not_financial_semantics(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    configuration = decode_document(
        empty_record.resources["configuration.json"], "configuration.json"
    )
    outputs = [{"sink": "summary", "required_checks": []}]
    configuration["outputs"] = outputs
    payload = encode_json(configuration, integer_strings=False)
    changed = replace(
        empty_record,
        metadata={
            **empty_record.metadata,
            "outputs": outputs,
            "configuration": {
                "path": "configuration.json",
                "byte_digest": byte_digest(payload),
            },
        },
        resources={**empty_record.resources, "configuration.json": payload},
    )
    result = assemble_snapshot(second, changed, settings)
    assert baseline.financial_digest == result.financial_digest


def test_declaration_source_order_changes_financial_content(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = assemble_snapshot(first, empty_record, settings)
    configuration = decode_document(
        empty_record.resources["configuration.json"], "configuration.json"
    )
    configuration["declaration_source_order"] = ["synthetic-input"]
    payload = encode_json(configuration, integer_strings=False)
    changed = replace(
        empty_record,
        metadata={
            **empty_record.metadata,
            "configuration": {
                "path": "configuration.json",
                "byte_digest": byte_digest(payload),
            },
        },
        resources={**empty_record.resources, "configuration.json": payload},
    )
    result = assemble_snapshot(second, changed, settings)
    assert baseline.financial_digest != result.financial_digest
    assert baseline.manifest_digest != result.manifest_digest
    assert baseline.descriptor_digest != result.descriptor_digest


def _extract_payload(
    reason: str,
    *,
    source_scope_id: str = "scope",
    importer_id: str = "synthetic-v1",
) -> bytes:
    return encode_json(
        {
            "schema_version": 1,
            "source_scope_id": source_scope_id,
            "source_blob_digest": "sha256:synthetic",
            "importer_id": importer_id,
            "source_scope": {
                "source_class": "document",
                "institution": None,
                "period_start": None,
                "period_end": None,
                "completeness": "unstated",
                "source_accounts": [],
                "revisions": [],
            },
            "records": [],
            "unsupported_content": [
                {
                    "source_section": "section",
                    "record_locator": "record:1",
                    "event_family": "unknown",
                    "reason": reason,
                }
            ],
        },
        integer_strings=False,
    )


def _extract_output(path: str, payload: bytes) -> OutputPayload:
    return OutputPayload(
        "extract", path, payload, byte_digest(payload), "extraction_schema"
    )


def test_retained_extract_changes_financial_provenance(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    def with_extract(reason: str) -> SnapshotInput:
        return replace(
            empty_record,
            output_payloads=(
                _extract_output("extract.json", _extract_payload(reason)),
            ),
        )

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    left = assemble_snapshot(first, with_extract("unsupported_contract"), settings)
    right = assemble_snapshot(second, with_extract("unknown_source_content"), settings)
    assert left.financial_digest != right.financial_digest


def test_duplicate_extract_packaging_is_not_new_financial_evidence(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    payload = _extract_payload("unsupported_contract")
    original = _extract_output("extract-0.json", payload)
    repeated = _extract_output("extract-1.json", payload)
    alternate_scope = _extract_output(
        "extract-1.json",
        _extract_payload("unsupported_contract", source_scope_id="another-scope"),
    )
    alternate_importer = _extract_output(
        "extract-1.json",
        _extract_payload("unsupported_contract", importer_id="other-importer"),
    )
    recovery_set = empty_record.metadata["recovery_set"]
    assert isinstance(recovery_set, dict)
    recovery = {
        **recovery_set,
        "external_assets": [
            {
                "asset_id": "retry-receipt",
                "path": "retained/receipt",
                "byte_digest": "b" * 64,
            }
        ],
    }
    cases = (
        ("baseline", (original,), empty_record.metadata),
        ("duplicate", (original, repeated), empty_record.metadata),
        (
            "recovery",
            (original,),
            {**empty_record.metadata, "recovery_set": recovery},
        ),
        ("distinct-scope", (original, alternate_scope), empty_record.metadata),
        ("distinct-importer", (original, alternate_importer), empty_record.metadata),
    )
    results = {}
    for name, artifacts, metadata in cases:
        destination = tmp_path / name
        destination.mkdir()
        results[name] = assemble_snapshot(
            destination,
            replace(empty_record, metadata=metadata, output_payloads=artifacts),
            settings,
        )
    baseline = results["baseline"]
    assert results["duplicate"].financial_digest == baseline.financial_digest
    assert results["recovery"].financial_digest == baseline.financial_digest
    assert results["duplicate"].descriptor_digest != baseline.descriptor_digest
    assert results["recovery"].descriptor_digest != baseline.descriptor_digest
    assert results["distinct-scope"].financial_digest != baseline.financial_digest
    assert results["distinct-importer"].financial_digest != baseline.financial_digest
    duplicate_manifest = decode_document(
        (tmp_path / "duplicate" / "manifest.json").read_bytes(), "manifest.json"
    )
    artifacts = duplicate_manifest["artifacts"]
    assert isinstance(artifacts, list)
    paths = set()
    for entry in artifacts:
        assert isinstance(entry, dict)
        paths.add(entry["path"])
    assert paths == {
        "extract-0.json",
        "extract-1.json",
    }


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
    inputs = replace(empty_record, metadata=metadata, resources=resources)
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
    inputs = replace(empty_record, metadata=metadata)
    with pytest.raises(ContractError, match="snapshot_metadata"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


def test_extra_unlisted_resource_cannot_be_certified(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    inputs = replace(
        empty_record, resources={**empty_record.resources, "extra.json": b"{}\n"}
    )
    with pytest.raises(ContractError, match="snapshot_resource_inventory"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("resource", ["configuration.json", "derivations.json"])
def test_noncanonical_resources_fail_before_writing(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    resource: str,
) -> None:
    payload = b" " + empty_record.resources[resource]
    reference = (
        "configuration"
        if resource == "configuration.json"
        else "derivation_definitions"
    )
    inputs = replace(
        empty_record,
        metadata={
            **empty_record.metadata,
            reference: {"path": resource, "byte_digest": byte_digest(payload)},
        },
        resources={**empty_record.resources, resource: payload},
    )
    with pytest.raises(ContractError, match="artifact_document_encoding"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("retained", [False, True])
def test_noncanonical_schema_typed_artifact_fails_before_writing(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    *,
    retained: bool,
) -> None:
    payload = b" " + _extract_payload("unsupported_contract")
    output = _extract_output("extract.json", payload)
    inputs = replace(empty_record, output_payloads=(output,))
    if retained:
        inputs = replace(
            empty_record,
            metadata={
                **empty_record.metadata,
                "artifacts": [
                    {
                        "kind": output.kind,
                        "path": output.path,
                        "byte_digest": byte_digest(payload),
                        "logical_digest": output.logical_digest,
                        "schema_id": output.schema_id,
                    }
                ],
            },
            resources={**empty_record.resources, output.path: payload},
        )
    with pytest.raises(ContractError, match="artifact_document_encoding"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


def test_retained_artifact_logical_digest_is_verified_before_writing(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
) -> None:
    payload = _extract_payload("unsupported_contract")
    inputs = replace(
        empty_record,
        metadata={
            **empty_record.metadata,
            "artifacts": [
                {
                    "kind": "extract",
                    "path": "extract.json",
                    "byte_digest": byte_digest(payload),
                    "logical_digest": "0" * 64,
                    "schema_id": "extraction_schema",
                }
            ],
        },
        resources={**empty_record.resources, "extract.json": payload},
    )
    with pytest.raises(ContractError, match="artifact_logical_digest"):
        assemble_snapshot(tmp_path, inputs, settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "missing.json"),
        ("byte_digest", "0" * 64),
        ("logical_digest", "0" * 64),
        ("schema_id", "model_artifact_schema"),
        ("kind", "model"),
        ("inputs", "duplicate"),
    ],
)
def test_stage_envelope_payload_claims_are_verified_before_writing(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    field: str,
    value: str,
) -> None:
    content = _extract_payload("unsupported_contract")
    output = _extract_output("extract.json", content)
    envelope = stage_envelope(
        stage="extract",
        schema_id="extraction_schema",
        configuration_digest="c" * 64,
        inputs=(InputEdge("source", 0, "source", "d" * 64),),
        facts=empty_record.execution,
        path=output.path,
        byte_digest_value=byte_digest(content),
        logical_digest=byte_digest(content),
    )
    reference = envelope["payload"]
    assert isinstance(reference, dict)
    expected_error = "stage_envelope_payload"
    if field == "kind":
        envelope[field] = value
    elif field == "inputs":
        edges = envelope[field]
        assert isinstance(edges, list)
        edges.append(edges[0])
        expected_error = "stage_envelope_input_ordinals"
    else:
        reference[field] = value
    payload = encode_json(envelope, integer_strings=False)
    inputs = replace(
        empty_record,
        output_payloads=(
            output,
            OutputPayload(
                "envelope",
                "extract-envelope.json",
                payload,
                byte_digest(payload),
                "envelope_schema",
            ),
        ),
    )
    with pytest.raises(ContractError, match=expected_error):
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
    changed: Sequence[tuple[str, str]],
) -> None:
    baseline = execution_fingerprint(
        stage="tabular",
        schema_id="tabular_schema",
        configuration_digest="d" * 64,
        inputs=(
            InputEdge("left", 0, "resource", "a" * 64),
            InputEdge("right", 0, "resource", "b" * 64),
        ),
        facts=ExecutionFacts(Producer("producer", "1", "c" * 64), "e" * 64),
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        schema_id="tabular_schema",
        configuration_digest="d" * 64,
        inputs=tuple(
            InputEdge(role, index, "resource", digest)
            for index, (role, digest) in enumerate(changed)
        ),
        facts=ExecutionFacts(Producer("producer", "1", "c" * 64), "e" * 64),
    )


def test_writer_configuration_and_code_change_execution_identity() -> None:
    baseline = execution_fingerprint(
        stage="tabular",
        schema_id="tabular_schema",
        configuration_digest="d" * 64,
        inputs=(),
        facts=ExecutionFacts(Producer("producer", "1", "a" * 64), "e" * 64),
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        schema_id="tabular_schema",
        configuration_digest="d" * 64,
        inputs=(),
        facts=ExecutionFacts(Producer("producer", "1", "b" * 64), "e" * 64),
    )
    assert baseline != execution_fingerprint(
        stage="tabular",
        schema_id="tabular_schema",
        configuration_digest="f" * 64,
        inputs=(),
        facts=ExecutionFacts(Producer("producer", "1", "a" * 64), "e" * 64),
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
from jbt.artifacts.execution import ExecutionFacts, Producer
from jbt.artifacts.snapshot import BuildInput, SnapshotInput, assemble_snapshot

locale.setlocale(locale.LC_ALL, "")
document = json.load(sys.stdin)
inputs = SnapshotInput(
    tables=document["tables"],
    builds=[BuildInput(**row) for row in document["builds"]],
    metadata=document["metadata"],
    resources={name: text.encode() for name, text in document["resources"].items()},
    execution=ExecutionFacts(
        Producer(**document["execution"]["producer"]),
        document["execution"]["execution_digest"],
    ),
    writer_version=document["writer_version"],
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
        "execution": asdict(empty_record.execution),
        "writer_version": empty_record.writer_version,
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
