from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator

from jbt.artifacts.execution import stage_envelope
from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.artifacts.parquet import WriterSettings, write_table
from jbt.artifacts.reader import decode_typed_document, read_snapshot
from jbt.artifacts.snapshot import OutputPayload, SnapshotInput, assemble_snapshot
from jbt.contracts import schemas
from jbt.contracts.catalog import catalog
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from pathlib import Path


def test_production_reader_revalidates_pinned_empty_snapshot(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    result = assemble_snapshot(tmp_path, empty_record, settings)
    snapshot = read_snapshot(tmp_path, result.descriptor_digest)
    assert snapshot.descriptor_digest == result.descriptor_digest
    assert snapshot.tables["build"][0]["manifest_digest"] == result.manifest_digest
    assert snapshot.manifest["logical_content_digest"] == result.financial_digest
    assert snapshot.configuration["as_of"] == "2026-01-31"
    descriptor = decode_document(
        (tmp_path / "descriptor.json").read_bytes(), "descriptor.json"
    )
    payload_entries = descriptor["payloads"]
    assert isinstance(payload_entries, list)
    expected_outputs = {}
    for entry in payload_entries:
        assert isinstance(entry, dict)
        path = entry["path"]
        assert isinstance(path, str)
        if not path.endswith(".parquet"):
            expected_outputs[path] = (tmp_path / path).read_bytes()
    assert snapshot.outputs == expected_outputs
    assert "configuration.json" in snapshot.outputs
    assert "tabular_schema.json" in snapshot.outputs


def test_wire_alternatives_do_not_repeat_enclosing_schema_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            f"number_{index}": {"type": "integer", "const": index}
            for index in range(40)
        },
        "type": "object",
        "properties": {
            f"field_{index}": {
                "anyOf": [{"$ref": f"#/$defs/number_{index}"}, {"type": "null"}]
            }
            for index in range(40)
        },
        "additionalProperties": False,
    }
    document = {f"field_{index}": index for index in range(40)}
    original = Draft202012Validator.check_schema
    checked_roots = []

    def check_schema(candidate: dict) -> None:
        if "$defs" in candidate:
            checked_roots.append(candidate)
        original(candidate)

    monkeypatch.setattr(Draft202012Validator, "check_schema", check_schema)
    schemas._validator.cache_clear()  # noqa: SLF001 - isolate compilation counts
    assert (
        decode_typed_document(encode_json(document, integer_strings=True), schema)
        == document
    )
    assert len(checked_roots) <= 1


def test_production_reader_rejects_corrupt_payload(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    result = assemble_snapshot(tmp_path, empty_record, settings)
    (tmp_path / "accounts.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="artifact digest"):
        read_snapshot(tmp_path, result.descriptor_digest)


def test_builtin_model_schema_covers_complete_shared_table_wrapper(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    model = encode_json(
        {
            "schema_version": 1,
            "tables": {name: [] for name in empty_record.tables},
            "checks": [],
            "derivations": [],
        },
        integer_strings=False,
    )
    record = replace(
        empty_record,
        output_payloads=(
            OutputPayload(
                "model",
                "model.json",
                model,
                byte_digest(model),
                schema_id="model_artifact_schema",
            ),
        ),
    )
    result = assemble_snapshot(tmp_path, record, settings)
    assert (
        read_snapshot(tmp_path, result.descriptor_digest).outputs["model.json"] == model
    )


def test_reader_uses_inventory_not_extension_to_select_financial_tables(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    content = b"opaque retained evidence"
    record = replace(
        empty_record,
        output_payloads=(
            OutputPayload(
                "evidence", "evidence.parquet", content, byte_digest(content)
            ),
        ),
    )
    result = assemble_snapshot(tmp_path, record, settings)
    snapshot = read_snapshot(tmp_path, result.descriptor_digest)
    assert snapshot.outputs["evidence.parquet"] == content
    assert snapshot.manifest["logical_content_digest"] == result.financial_digest


@pytest.mark.parametrize(
    ("fault", "failure"),
    [
        ("payload_path", ("stage_envelope_payload", "ledger-envelope.json")),
        ("payload_schema", ("stage_envelope_payload", "ledger-envelope.json")),
        ("required_schema", ("envelope_schema", "ledger-envelope.json")),
        ("unknown_schema", ("unknown_artifact_schema", "ledger.beancount")),
        ("invalid_document", ("artifact_schema", "ledger-envelope.json")),
    ],
)
def test_reader_rejects_rehashed_invalid_envelope_claims_safely(
    tmp_path: Path,
    settings: WriterSettings,
    empty_record: SnapshotInput,
    fault: str,
    failure: tuple[str, str],
) -> None:
    content = b"opaque ledger"
    envelope = stage_envelope(
        stage="ledger",
        schema_id="text/x-beancount",
        configuration_digest="c" * 64,
        inputs=(),
        facts=empty_record.execution,
        path="ledger.beancount",
        byte_digest_value=byte_digest(content),
        logical_digest=byte_digest(content),
    )
    payload = encode_json(envelope, integer_strings=False)
    record = replace(
        empty_record,
        output_payloads=(
            OutputPayload("ledger", "ledger.beancount", content, byte_digest(content)),
            OutputPayload(
                "envelope",
                "ledger-envelope.json",
                payload,
                byte_digest(payload),
                "envelope_schema",
            ),
        ),
    )
    result = assemble_snapshot(tmp_path, record, settings)
    snapshot = read_snapshot(tmp_path, result.descriptor_digest)
    reference = envelope["payload"]
    assert isinstance(reference, dict)
    if fault == "payload_path":
        reference["path"] = "missing.beancount"
    elif fault == "payload_schema":
        reference["schema_id"] = "sensitive-unregistered-schema"
    elif fault == "invalid_document":
        envelope.pop("execution_digest")
    altered = encode_json(envelope, integer_strings=False)
    (tmp_path / "ledger-envelope.json").write_bytes(altered)
    manifest = snapshot.manifest
    entry = next(
        entry for entry in manifest["artifacts"] if entry["kind"] == "envelope"
    )
    if fault == "required_schema":
        entry["schema_id"] = "model_artifact_schema"
    elif fault == "unknown_schema":
        ledger_entry = next(
            entry for entry in manifest["artifacts"] if entry["kind"] == "ledger"
        )
        ledger_entry["schema_id"] = "sensitive-unregistered-schema"
    entry["byte_digest"] = byte_digest(altered)
    entry["logical_digest"] = byte_digest(altered)
    manifest_bytes = encode_json(manifest, integer_strings=True)
    (tmp_path / "manifest.json").write_bytes(manifest_bytes)
    build_row = snapshot.tables["build"][0]
    build_row["manifest_digest"] = byte_digest(manifest_bytes)
    build_table = next(table for table in catalog() if table.name == "build")
    write_table(tmp_path / "build.parquet", build_table, [build_row], settings)
    descriptor = decode_document(
        (tmp_path / "descriptor.json").read_bytes(), "descriptor.json"
    )
    descriptor["manifest_digest"] = byte_digest(manifest_bytes)
    entries = descriptor["payloads"]
    assert isinstance(entries, list)
    for item in entries:
        assert isinstance(item, dict)
        if item["path"] in {"ledger-envelope.json", "build.parquet"}:
            item["byte_digest"] = byte_digest((tmp_path / item["path"]).read_bytes())
    descriptor_bytes = encode_json(descriptor, integer_strings=False)
    (tmp_path / "descriptor.json").write_bytes(descriptor_bytes)
    with pytest.raises(ContractError) as error:
        read_snapshot(tmp_path, byte_digest(descriptor_bytes))
    assert (error.value.code, error.value.location) == failure
    assert "sensitive-unregistered-schema" not in str(error.value)


def test_retained_derivation_definitions_are_semantic_and_pinned(
    tmp_path: Path, settings: WriterSettings, empty_record: SnapshotInput
) -> None:
    def with_definition(rule: str, *, matching_digest: bool = True) -> SnapshotInput:
        definition = {"version": 1, "rule": rule}
        definitions = encode_json(
            {"schema_version": 1, "definitions": {"synthetic-v1": definition}},
            integer_strings=False,
        )
        metadata = {
            **empty_record.metadata,
            "derivation_definitions": {
                "path": "derivations.json",
                "byte_digest": byte_digest(definitions),
            },
            "derivations": [
                {
                    "derivation_id": "synthetic-v1",
                    "algorithm": "synthetic",
                    "version": "1",
                    "declaration_ids": [],
                    "units": "units",
                    "content_digest": byte_digest(
                        encode_json(
                            definition
                            if matching_digest
                            else {"version": 1, "rule": "wrong"},
                            integer_strings=False,
                        )
                    ),
                }
            ],
        }
        return replace(
            empty_record,
            metadata=metadata,
            resources={**empty_record.resources, "derivations.json": definitions},
        )

    first, second, corrupt = (
        tmp_path / "first",
        tmp_path / "second",
        tmp_path / "corrupt",
    )
    for directory in (first, second, corrupt):
        directory.mkdir()
    baseline = assemble_snapshot(first, with_definition("authoritative"), settings)
    changed = assemble_snapshot(second, with_definition("different"), settings)
    assert baseline.financial_digest != changed.financial_digest
    pinned = read_snapshot(first, baseline.descriptor_digest)
    assert "derivations.json" in pinned.outputs
    with pytest.raises(ContractError, match="derivation_definition_digest"):
        assemble_snapshot(
            corrupt, with_definition("authoritative", matching_digest=False), settings
        )

    manifest = decode_document((first / "manifest.json").read_bytes(), "manifest.json")
    derivations = manifest["derivations"]
    assert isinstance(derivations, list)
    assert isinstance(derivations[0], dict)
    derivations[0]["content_digest"] = "0" * 64
    manifest_bytes = encode_json(manifest, integer_strings=False)
    (first / "manifest.json").write_bytes(manifest_bytes)
    build_row = pinned.tables["build"][0]
    build_row["manifest_digest"] = byte_digest(manifest_bytes)
    build_table = next(table for table in catalog() if table.name == "build")
    write_table(first / "build.parquet", build_table, [build_row], settings)
    descriptor = decode_document(
        (first / "descriptor.json").read_bytes(), "descriptor.json"
    )
    descriptor["manifest_digest"] = byte_digest(manifest_bytes)
    entries = descriptor["payloads"]
    assert isinstance(entries, list)
    build_entry = next(
        entry
        for entry in entries
        if isinstance(entry, dict) and entry["path"] == "build.parquet"
    )
    build_entry["byte_digest"] = byte_digest((first / "build.parquet").read_bytes())
    descriptor_bytes = encode_json(descriptor, integer_strings=False)
    (first / "descriptor.json").write_bytes(descriptor_bytes)
    with pytest.raises(ContractError, match="derivation_definition_digest"):
        read_snapshot(first, byte_digest(descriptor_bytes))
