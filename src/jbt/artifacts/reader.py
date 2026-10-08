"""Read one pinned generation through packaged publication contracts."""

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
from jsonschema import ValidationError

from jbt.artifacts.canonical import table_digest
from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    IntegrityCode,
    byte_digest,
    decode_document,
    read_payload,
    require_digest,
    require_inventory,
)
from jbt.artifacts.parquet import arrow_schema
from jbt.artifacts.snapshot import (
    _check_interpretation,
    _financial_digest,
    _schema_resources,
    _validate_artifact_envelopes,
    _validate_derivation_definitions,
)
from jbt.contracts.catalog import catalog, tabular_schema
from jbt.contracts.primitives import ContractError
from jbt.contracts.publication import (
    validate_configuration,
    validate_descriptor,
    validate_manifest,
    validate_registries,
)
from jbt.contracts.schemas import validate_document
from jbt.contracts.validation import validate_source_sequence_directions
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

MAXIMUM_PAYLOAD_BYTES = 64 * 1024 * 1024
MAXIMUM_INTEGER_LENGTH = 20


@dataclass(frozen=True)
class PinnedSnapshot:
    """Verified rows, metadata, and canonical non-tabular payload bytes."""

    descriptor_digest: str
    tables: dict[str, list[dict[str, object]]]
    manifest: dict
    configuration: dict
    outputs: dict[str, bytes]


def _wire_value(  # noqa: C901, PLR0912 - schema alternatives and boolean nodes
    value: object, node: object, schema: dict
) -> object:
    if node is False:
        msg = "wire value rejected by schema"
        raise ValueError(msg)
    if node is True:
        return value
    if not isinstance(node, dict):
        msg = "invalid schema node"
        raise TypeError(msg)
    reference = node.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            msg = "only local schema references are allowed"
            raise ValueError(msg)
        target = schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return _wire_value(value, target, schema)
    variants = node.get("oneOf", node.get("anyOf"))
    if variants is not None:
        for variant in variants:
            try:
                decoded = _wire_value(value, variant, schema)
                validate_document(
                    decoded,
                    variant,
                    reference_scope=schema,
                )
            except ValidationError, ValueError, KeyError:
                continue
            return decoded
        msg = "wire value matches no schema variant"
        raise ValueError(msg)
    if node.get("type") == "integer" or type(node.get("const")) is int:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"0|-?[1-9][0-9]*", value) is None
            or len(value) > MAXIMUM_INTEGER_LENGTH
        ):
            msg = "invalid wire integer"
            raise ValueError(msg)
        return int(value)
    if isinstance(value, dict):
        properties = node.get("properties", {})
        return {
            key: _wire_value(item, properties.get(key, {}), schema)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if "prefixItems" in node:
            return [
                _wire_value(
                    item,
                    node["prefixItems"][index]
                    if index < len(node["prefixItems"])
                    else node.get("items", {}),
                    schema,
                )
                for index, item in enumerate(value)
            ]
        return [_wire_value(item, node.get("items", {}), schema) for item in value]
    return value


def decode_typed_document(
    payload: bytes, schema: dict, *, integer_strings: bool = True
) -> dict:
    """Decode schema-typed wire integers and require canonical complete JSON."""
    raw = decode_document(payload, "<document>")
    decoded = _wire_value(raw, schema, schema) if integer_strings else raw
    try:
        validate_document(decoded, schema)
    except ValidationError as error:
        code = "artifact_schema"
        raise ContractError(code, "<document>") from error
    if payload != encode_json(decoded, integer_strings=integer_strings):
        msg = "noncanonical artifact document"
        raise ValueError(msg)
    if not isinstance(decoded, dict):
        msg = "artifact document must be an object"
        raise TypeError(msg)
    return decoded


def _read_tables(
    root: Path, descriptor: dict, manifest: dict
) -> dict[str, list[dict[str, object]]]:
    digests = {item["path"]: item["byte_digest"] for item in descriptor["payloads"]}
    entries = {item["name"]: item for item in manifest["tables"]}
    schemas = {table["name"]: table for table in tabular_schema()["tables"]}
    rows: dict[str, list[dict[str, object]]] = {}
    for table in catalog():
        filename = f"{table.name}.parquet"
        payload = read_payload(
            root, filename, digests[filename], maximum_bytes=MAXIMUM_PAYLOAD_BYTES
        )
        relation = pq.read_table(pa.BufferReader(payload))
        if relation.schema != arrow_schema(table):
            code = "parquet_physical_schema"
            raise ContractError(code, filename)
        data = relation.to_pylist()
        rows[table.name] = [
            {
                key: value.isoformat() if type(value) is date else value
                for key, value in row.items()
            }
            for row in data
        ]
        if table.name != "build":
            entry = entries[table.name]
            if entry["row_count"] != len(data) or entry[
                "logical_digest"
            ] != table_digest(
                schemas[table.name]["columns"],
                rows[table.name],
                schema_version=1,
            ):
                code = "table_logical_digest"
                raise ContractError(code, filename)
    return rows


def _verify_schemas(root: Path, manifest: dict, expected: Mapping[str, dict]) -> None:
    schemas = {entry["schema_id"]: entry for entry in manifest["schemas"]}
    if set(schemas) != set(expected):
        raise ArtifactIntegrityError(IntegrityCode.INVENTORY, "<schemas>")
    for name, schema in expected.items():
        entry = schemas[name]
        path = f"{name}.json"
        actual = read_payload(
            root, path, entry["byte_digest"], maximum_bytes=MAXIMUM_PAYLOAD_BYTES
        )
        if (
            entry["schema_version"] != 1
            or entry["path"] != path
            or actual != encode_json(schema, integer_strings=False)
        ):
            code = "unapproved_schema"
            raise ContractError(code, path)


def _read_configuration_and_registries(
    manifest: dict,
    rows: dict[str, list[dict[str, object]]],
    payloads: dict[str, bytes],
    schemas: Mapping[str, dict],
) -> dict:
    configuration_ref = manifest["configuration"]
    configuration = decode_typed_document(
        payloads[configuration_ref["path"]],
        schemas["configuration_schema"],
        integer_strings=False,
    )
    validate_configuration(configuration, rows)
    if configuration["outputs"] != manifest["outputs"]:
        code = "configuration_outputs"
        raise ContractError(code, configuration_ref["path"])
    registries: list[dict] = []
    for entry in manifest["registries"]:
        path = entry["path"]
        registry = decode_typed_document(
            payloads[path], schemas["registry_schema"], integer_strings=False
        )
        validate_registries(registry)
        registries.append(registry)
        if entry["schema_id"] != "registry_schema" or entry[
            "content_digest"
        ] != byte_digest(encode_json(registry, integer_strings=True)):
            code = "registry_logical_digest"
            raise ContractError(code, path)
    validate_source_sequence_directions(rows, registries)
    return configuration


def _read_artifacts(
    manifest: dict, payloads: dict[str, bytes], schemas: Mapping[str, dict]
) -> None:
    for entry in manifest["artifacts"]:
        path = entry["path"]
        _check_interpretation(entry, payloads[path], schemas)


def _verify_financial_digest(
    manifest: dict,
    rows: dict[str, list[dict[str, object]]],
    payloads: dict[str, bytes],
    schemas: Mapping[str, dict],
) -> None:
    entities: list[tuple[str, str]] = []
    for row in rows["build"]:
        entity_id, as_of = row["entity_id"], row["as_of"]
        if not isinstance(entity_id, str) or not isinstance(as_of, str):
            code = "build_identity"
            raise ContractError(code, "build.parquet")
        entities.append((entity_id, as_of))
    digest = _financial_digest(
        schemas, manifest["tables"], entities, manifest, payloads
    )
    if digest != manifest["logical_content_digest"]:
        code = "financial_logical_digest"
        raise ContractError(code, "manifest.json")


def read_snapshot(
    root: Path,
    descriptor_digest: str,
    *,
    extra_schemas: Mapping[str, dict] | None = None,
) -> PinnedSnapshot:
    """Verify exact pinned inventory, typed tables, and logical publication."""
    expected_schemas = _schema_resources(extra_schemas or {})
    descriptor = decode_typed_document(
        read_payload(
            root,
            "descriptor.json",
            descriptor_digest,
            maximum_bytes=MAXIMUM_PAYLOAD_BYTES,
        ),
        expected_schemas["descriptor_schema"],
    )
    manifest_path = descriptor["manifest_path"]
    manifest = decode_typed_document(
        read_payload(
            root,
            manifest_path,
            descriptor["manifest_digest"],
            maximum_bytes=MAXIMUM_PAYLOAD_BYTES,
        ),
        expected_schemas["manifest_schema"],
    )
    validate_descriptor(descriptor, manifest)
    declared = {entry["path"]: entry for entry in descriptor["payloads"]}
    require_inventory(
        root,
        frozenset({*declared, "descriptor.json", manifest_path}),
    )
    _verify_schemas(root, manifest, expected_schemas)
    table_paths = {f"{table.name}.parquet" for table in catalog()}
    payloads = {
        path: read_payload(
            root, path, entry["byte_digest"], maximum_bytes=MAXIMUM_PAYLOAD_BYTES
        )
        for path, entry in declared.items()
        if path not in table_paths
    }
    rows = _read_tables(root, descriptor, manifest)
    validate_manifest(manifest, rows)
    build_digest = descriptor["manifest_digest"]
    schemas = {entry["schema_id"]: entry for entry in manifest["schemas"]}
    if any(
        row["manifest_digest"] != build_digest
        or row["schema_digest"] != schemas["tabular_schema"]["byte_digest"]
        for row in rows["build"]
    ):
        code = "build_manifest_pin"
        raise ContractError(code, "build.parquet")
    configuration = _read_configuration_and_registries(
        manifest, rows, payloads, expected_schemas
    )
    _validate_derivation_definitions(manifest, payloads, expected_schemas)
    _read_artifacts(manifest, payloads, expected_schemas)
    _validate_artifact_envelopes(manifest, payloads)
    _verify_financial_digest(manifest, rows, payloads, expected_schemas)
    return PinnedSnapshot(descriptor_digest, rows, manifest, configuration, payloads)


def read_pinned(
    project_root: Path,
    descriptor_digest: str,
    *,
    extra_schemas: Mapping[str, dict] | None = None,
) -> PinnedSnapshot:
    """Read a named immutable generation without selecting mutable current."""
    require_digest(descriptor_digest)
    if project_root.is_symlink() or (project_root / "generations").is_symlink():
        raise ArtifactIntegrityError(IntegrityCode.SYMLINK, "<generations>")
    return read_snapshot(
        project_root / "generations" / descriptor_digest,
        descriptor_digest,
        extra_schemas=extra_schemas,
    )
