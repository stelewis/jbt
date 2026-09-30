"""Assemble complete static snapshots without claiming runtime durability."""

import sys
from dataclasses import dataclass
from importlib.metadata import version
from typing import TYPE_CHECKING, TypedDict

from jsonschema import ValidationError

from jbt.artifacts.canonical import table_digest
from jbt.artifacts.integrity import (
    byte_digest,
    decode_document,
    require_digest,
    require_filename,
)
from jbt.artifacts.parquet import WriterSettings, write_table
from jbt.contracts.catalog import catalog, schema_document
from jbt.contracts.primitives import ContractError
from jbt.contracts.publication import (
    validate_configuration,
    validate_descriptor,
    validate_manifest,
    validate_registries,
)
from jbt.contracts.schemas import (
    configuration_schema,
    declaration_schema,
    descriptor_schema,
    extraction_schema,
    manifest_schema,
    registry_schema,
    validate_document,
)
from jbt.contracts.validation import (
    validate_source_sequence_directions,
    validate_tables,
)
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@dataclass(frozen=True)
class BuildInput:
    """Execution facts supplied by the caller, not the wall clock."""

    entity_id: str
    producer_version: str
    as_of: str
    input_fingerprint: str
    is_dirty: bool


@dataclass(frozen=True)
class SnapshotResult:
    """Externally pin the descriptor digest to select this artifact."""

    descriptor_digest: str
    manifest_digest: str
    financial_digest: str


@dataclass(frozen=True)
class SnapshotInput:
    """Complete financial records and retained interpretation inputs."""

    tables: Mapping[str, list[dict[str, object]]]
    builds: Sequence[BuildInput]
    metadata: Mapping[str, object]
    resources: Mapping[str, bytes]


class TableArtifact(TypedDict):
    """Manifest identity and physical location of one logical relation."""

    name: str
    path: str
    byte_digest: str
    logical_digest: str
    row_count: int


def execution_fingerprint(
    *,
    stage: str,
    inputs: Sequence[tuple[str, str]],
    settings: WriterSettings,
    producer_digest: str,
) -> str:
    """Retain ordered named edges and multiplicity in execution identity."""
    require_digest(producer_digest)
    for _, digest in inputs:
        require_digest(digest)
    return byte_digest(
        encode_json(
            {
                "stage": stage,
                "producer_digest": producer_digest,
                "inputs": [[role, digest] for role, digest in inputs],
                "writer": _writer_document(settings),
                "python": sys.version,
            },
            integer_strings=True,
        ),
    )


def _writer_document(settings: WriterSettings) -> dict[str, object]:
    return {
        "library": "pyarrow",
        "version": version("pyarrow"),
        "compression": settings.compression.value.lower(),
        "row_group_size": settings.row_group_size,
        "dictionary": settings.dictionary,
        "statistics": settings.statistics,
        "data_page_version": "1.0",
    }


def _schema_resources() -> dict[str, dict]:
    return {
        "schema": schema_document(),
        "declaration_schema": declaration_schema(),
        "manifest_schema": manifest_schema(),
        "descriptor_schema": descriptor_schema(),
        "configuration_schema": configuration_schema(),
        "registry_schema": registry_schema(),
        "extraction_schema": extraction_schema(),
    }


def _build_rows(
    builds: Sequence[BuildInput],
    schema_digest: str,
    schema_fingerprint: str,
    settings: WriterSettings,
    producer_digest: str,
) -> list[dict[str, object]]:
    return [
        {
            "entity_id": build.entity_id,
            "manifest_digest": "0" * 64,
            "producer_version": build.producer_version,
            "schema_version": 1,
            "schema_digest": schema_digest,
            "as_of": build.as_of,
            "execution_fingerprint": execution_fingerprint(
                stage="tabular",
                inputs=[
                    ("retained_inputs", build.input_fingerprint),
                    ("schemas", schema_fingerprint),
                ],
                settings=settings,
                producer_digest=producer_digest,
            ),
            "is_dirty": build.is_dirty,
        }
        for build in builds
    ]


def _resource_payloads(
    resources: Mapping[str, bytes],
    schemas: Mapping[str, dict],
) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    payloads = dict(resources)
    schema_entries: list[dict[str, object]] = []
    for name, document in schemas.items():
        filename = f"{name}.json"
        if filename in payloads:
            msg = "caller resources cannot override published schemas"
            raise ValueError(msg)
        payload = encode_json(document, integer_strings=False)
        payloads[filename] = payload
        schema_entries.append(
            {
                "schema_id": name,
                "schema_version": 1,
                "path": filename,
                "byte_digest": byte_digest(payload),
            },
        )
    for filename in payloads:
        require_filename(filename)
        if not filename.endswith(".json") or filename in {
            "manifest.json",
            "descriptor.json",
        }:
            msg = "resources require distinct local JSON filenames"
            raise ValueError(msg)
    return payloads, schema_entries


def _check_resources(
    metadata: Mapping[str, object],
    payloads: Mapping[str, bytes],
    tables: Mapping[str, list[dict[str, object]]],
) -> None:
    configuration = metadata["configuration"]
    registries = metadata["registries"]
    if not isinstance(configuration, dict) or not isinstance(registries, list):
        msg = "configuration and registry resource references are required"
        raise TypeError(msg)
    document = _resource_document(configuration, payloads)
    validate_configuration(document, tables)
    if document["outputs"] != metadata["outputs"]:
        msg = "configuration outputs must match manifest outputs"
        raise ValueError(msg)
    if any(row["as_of"] != document["as_of"] for row in tables["build"]):
        msg = "configuration horizon must match every entity build"
        raise ValueError(msg)
    registry_documents = []
    for registry in registries:
        document = _resource_document(registry, payloads)
        validate_registries(document)
        if registry["content_digest"] != byte_digest(
            encode_json(document, integer_strings=True),
        ):
            msg = "registry content digest must match canonical semantics"
            raise ValueError(msg)
        registry_documents.append(document)
    validate_source_sequence_directions(tables, registry_documents)
    artifacts = metadata["artifacts"]
    if not isinstance(artifacts, list):
        msg = "artifacts must be an explicit list"
        raise TypeError(msg)
    for artifact in artifacts:
        path = artifact["path"]
        if (
            path not in payloads
            or byte_digest(payloads[path]) != artifact["byte_digest"]
        ):
            msg = "missing or corrupt retained artifact"
            raise ValueError(msg)
    referenced = {
        configuration["path"],
        *(registry["path"] for registry in registries),
        *(artifact["path"] for artifact in artifacts),
    }
    if referenced != payloads.keys():
        code = "snapshot_resource_inventory"
        raise ContractError(code, "resources")


def _resource_document(
    reference: dict[str, object],
    payloads: Mapping[str, bytes],
) -> dict[str, object]:
    path = reference["path"]
    if not isinstance(path, str):
        msg = "resource path must be a string"
        raise TypeError(msg)
    if path not in payloads or byte_digest(payloads[path]) != reference["byte_digest"]:
        msg = "missing or corrupt configuration/registry resource"
        raise ValueError(msg)
    return decode_document(payloads[path], path)


def _validate_metadata(metadata: Mapping[str, object]) -> None:
    schema = manifest_schema()
    generated = {
        "schema_version",
        "entities",
        "tables",
        "schemas",
        "writer_settings",
        "logical_content_digest",
        "numeric_limits",
    }
    schema["required"] = [name for name in schema["required"] if name not in generated]
    schema["properties"] = {
        name: definition
        for name, definition in schema["properties"].items()
        if name not in generated
    }
    try:
        validate_document(dict(metadata), schema)
    except ValidationError as error:
        code = "snapshot_metadata"
        raise ContractError(code, "metadata") from error


def _write_tables(
    root: Path,
    tables: Mapping[str, list[dict[str, object]]],
    settings: WriterSettings,
) -> list[TableArtifact]:
    entries: list[TableArtifact] = []
    definitions = {table["name"]: table for table in schema_document()["tables"]}
    for table in catalog():
        if table.name == "build":
            continue
        path = root / f"{table.name}.parquet"
        write_table(path, table, tables[table.name], settings)
        entries.append(
            {
                "name": table.name,
                "path": path.name,
                "byte_digest": byte_digest(path.read_bytes()),
                "logical_digest": table_digest(
                    definitions[table.name]["columns"],
                    tables[table.name],
                    schema_version=1,
                ),
                "row_count": len(tables[table.name]),
            },
        )
    return entries


def assemble_snapshot(
    root: Path,
    inputs: SnapshotInput,
    settings: WriterSettings,
) -> SnapshotResult:
    """Write one new static artifact; failure never creates a descriptor.

    The caller supplies a private, empty directory. Writer exclusion and
    atomic publication of that directory belong to the later runtime.
    """
    tables, builds = inputs.tables, inputs.builds
    metadata, resources = inputs.metadata, inputs.resources
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        msg = "snapshot assembly requires an empty ordinary directory"
        raise ValueError(msg)
    if not builds or len({build.entity_id for build in builds}) != len(builds):
        msg = "snapshot builds require distinct nonempty entities"
        raise ValueError(msg)
    schemas = _schema_resources()
    payloads, schema_entries = _resource_payloads(resources, schemas)
    schema = schemas["schema"]
    schema_digest = byte_digest(payloads["schema.json"])
    expected_names = {table.name for table in catalog()} - {"build"}
    if set(tables) != expected_names:
        msg = "snapshot requires exactly every non-build table"
        raise ValueError(msg)
    _validate_metadata(metadata)
    producer_digest = byte_digest(
        encode_json(metadata["toolchain"], integer_strings=True)
    )
    schema_fingerprint = byte_digest(encode_json(schemas, integer_strings=False))
    build_rows = _build_rows(
        builds,
        schema_digest,
        schema_fingerprint,
        settings,
        producer_digest,
    )
    complete = {**tables, "build": build_rows}
    validate_tables(complete)
    _check_resources(metadata, resources, complete)
    for filename, payload in payloads.items():
        (root / filename).write_bytes(payload)
    table_entries = _write_tables(root, tables, settings)
    table_checksums = [
        {"path": entry["path"], "byte_digest": entry["byte_digest"]}
        for entry in table_entries
    ]
    financial_digest = _financial_digest(
        schemas,
        table_entries,
        builds,
        metadata,
        resources,
    )
    manifest: dict[str, object] = {
        **metadata,
        "schema_version": 1,
        "entities": [
            {key: value for key, value in row.items() if key != "manifest_digest"}
            for row in build_rows
        ],
        "tables": table_entries,
        "schemas": schema_entries,
        "writer_settings": _writer_document(settings),
        "numeric_limits": schema["numeric_limits"],
        "logical_content_digest": financial_digest,
    }
    validate_manifest(manifest, complete)
    manifest_bytes = encode_json(manifest, integer_strings=True)
    manifest_digest = byte_digest(manifest_bytes)
    (root / "manifest.json").write_bytes(manifest_bytes)
    for row in build_rows:
        row["manifest_digest"] = manifest_digest
    build_table = next(table for table in catalog() if table.name == "build")
    write_table(root / "build.parquet", build_table, build_rows, settings)
    descriptor = {
        "schema_version": 1,
        "manifest_path": "manifest.json",
        "manifest_digest": manifest_digest,
        "payloads": sorted(
            [
                {"path": filename, "byte_digest": byte_digest(payload)}
                for filename, payload in payloads.items()
            ]
            + table_checksums
            + [
                {
                    "path": "build.parquet",
                    "byte_digest": byte_digest((root / "build.parquet").read_bytes()),
                }
            ],
            key=lambda payload: payload["path"],
        ),
    }
    validate_descriptor(descriptor, manifest)
    descriptor_bytes = encode_json(descriptor, integer_strings=True)
    (root / "descriptor.json").write_bytes(descriptor_bytes)
    return SnapshotResult(
        descriptor_digest=byte_digest(descriptor_bytes),
        manifest_digest=manifest_digest,
        financial_digest=financial_digest,
    )


def _financial_digest(
    schemas: Mapping[str, dict],
    entries: Sequence[TableArtifact],
    builds: Sequence[BuildInput],
    metadata: Mapping[str, object],
    resources: Mapping[str, bytes],
) -> str:
    return byte_digest(
        encode_json(
            {
                "entities": [[build.entity_id, build.as_of] for build in builds],
                "schemas": schemas,
                "tables": [
                    {"name": item["name"], "logical_digest": item["logical_digest"]}
                    for item in entries
                ],
                "resources": {
                    name: decode_document(payload, name)
                    for name, payload in resources.items()
                },
                **{
                    name: metadata[name]
                    for name in (
                        "effective_declarations",
                        "derivations",
                        "checks",
                    )
                },
            },
            integer_strings=True,
        ),
    )
