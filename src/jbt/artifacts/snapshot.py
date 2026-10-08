"""Assemble complete static snapshots without claiming runtime durability."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from jsonschema import Draft202012Validator, ValidationError

from jbt.artifacts.canonical import table_digest
from jbt.artifacts.execution import ExecutionFacts, InputEdge, execution_fingerprint
from jbt.artifacts.integrity import (
    byte_digest,
    decode_document,
    require_digest,
    require_filename,
)
from jbt.artifacts.parquet import WriterSettings, write_table
from jbt.contracts.catalog import catalog, tabular_schema
from jbt.contracts.primitives import ContractError
from jbt.contracts.publication import (
    validate_configuration,
    validate_descriptor,
    validate_extraction,
    validate_manifest,
    validate_registries,
)
from jbt.contracts.schemas import (
    checks_artifact_schema,
    configuration_schema,
    declaration_schema,
    derivation_definitions_schema,
    descriptor_schema,
    envelope_schema,
    execution_record_schema,
    extraction_schema,
    manifest_schema,
    model_artifact_schema,
    registry_schema,
    summary_artifact_schema,
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
    execution: ExecutionFacts
    writer_version: str
    output_payloads: Sequence[OutputPayload] = ()
    extra_schemas: Mapping[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputPayload:
    """Opaque sink bytes with explicitly verified byte identity."""

    kind: str
    path: str
    content: bytes
    logical_digest: str
    schema_id: str | None = None


class TableArtifact(TypedDict):
    """Manifest identity and physical location of one logical relation."""

    name: str
    path: str
    byte_digest: str
    logical_digest: str
    row_count: int


def _writer_document(settings: WriterSettings, version: str) -> dict[str, object]:
    return {
        "library": "pyarrow",
        "version": version,
        "compression": settings.compression.value.lower(),
        "row_group_size": settings.row_group_size,
        "dictionary": settings.dictionary,
        "statistics": settings.statistics,
        "data_page_version": "1.0",
    }


def _schema_resources(extra: Mapping[str, dict]) -> dict[str, dict]:
    schemas = {
        "tabular_schema": tabular_schema(),
        "declaration_schema": declaration_schema(),
        "manifest_schema": manifest_schema(),
        "descriptor_schema": descriptor_schema(),
        "configuration_schema": configuration_schema(),
        "derivation_definitions_schema": derivation_definitions_schema(),
        "registry_schema": registry_schema(),
        "extraction_schema": extraction_schema(),
        "envelope_schema": envelope_schema(),
        "execution_record_schema": execution_record_schema(),
        "checks_artifact_schema": checks_artifact_schema(),
        "model_artifact_schema": model_artifact_schema(),
        "summary_artifact_schema": summary_artifact_schema(),
    }
    for name, schema in extra.items():
        require_filename(f"{name}.json")
        if name in schemas or name in {"manifest", "descriptor"}:
            msg = "additional schemas cannot replace publication contracts"
            raise ValueError(msg)
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    return schemas


def _build_rows(
    builds: Sequence[BuildInput],
    schema_digest: str,
    schema_fingerprint: str,
    writer_digest: str,
    execution: ExecutionFacts,
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
                schema_id="tabular_schema",
                configuration_digest=writer_digest,
                inputs=(
                    InputEdge(
                        "retained_inputs", 0, "snapshot", build.input_fingerprint
                    ),
                    InputEdge("schemas", 0, "schema", schema_fingerprint),
                ),
                facts=execution,
            ),
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


def _check_resources(  # noqa: C901 - distinct resource boundaries
    metadata: Mapping[str, object],
    payloads: Mapping[str, bytes],
    tables: Mapping[str, list[dict[str, object]]],
    schemas: Mapping[str, dict],
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
    _validate_derivation_definitions(metadata, payloads, schemas)
    definitions_ref = metadata["derivation_definitions"]
    if not isinstance(definitions_ref, dict):
        msg = "derivation definitions require a resource reference"
        raise TypeError(msg)
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
        _check_interpretation(artifact, payloads[path], schemas)
    referenced = {
        configuration["path"],
        definitions_ref["path"],
        *(registry["path"] for registry in registries),
        *(artifact["path"] for artifact in artifacts),
    }
    if referenced != payloads.keys():
        code = "snapshot_resource_inventory"
        raise ContractError(code, "resources")


def _validate_derivation_definitions(
    metadata: Mapping[str, object],
    payloads: Mapping[str, bytes],
    schemas: Mapping[str, dict],
) -> dict:
    reference = metadata["derivation_definitions"]
    if not isinstance(reference, dict):
        msg = "derivation definitions require a resource reference"
        raise TypeError(msg)
    path = reference["path"]
    document = _resource_document(reference, payloads)
    validate_document(document, schemas["derivation_definitions_schema"])
    definitions = document["definitions"]
    entries = metadata["derivations"]
    if not isinstance(definitions, dict):
        msg = "derivation definitions must be an object"
        raise TypeError(msg)
    if (
        not isinstance(entries, list)
        or not all(isinstance(entry, dict) for entry in entries)
        or set(definitions) != {entry["derivation_id"] for entry in entries}
        or len(definitions) != len(entries)
    ):
        code = "derivation_definitions_inventory"
        raise ContractError(code, path)
    for entry in entries:
        definition = definitions[entry["derivation_id"]]
        if not isinstance(definition, dict):
            msg = "derivation definition must be an object"
            raise TypeError(msg)
        if (
            str(definition["version"]) != entry["version"]
            or byte_digest(encode_json(definition, integer_strings=False))
            != entry["content_digest"]
        ):
            code = "derivation_definition_digest"
            raise ContractError(code, path)
    return document


def _check_interpretation(
    artifact: dict, payload: bytes, schemas: Mapping[str, dict]
) -> None:
    if artifact["logical_digest"] != byte_digest(payload):
        code = "artifact_logical_digest"
        raise ContractError(code, artifact["path"])
    schema_id = artifact["schema_id"]
    required_schema = {
        "extract": "extraction_schema",
        "execution": "execution_record_schema",
        "envelope": "envelope_schema",
    }.get(artifact["kind"])
    if required_schema is not None and schema_id != required_schema:
        code = f"{artifact['kind']}_schema"
        raise ContractError(code, artifact["path"])
    if schema_id is None:
        return
    if schema_id not in schemas:
        code = "unknown_artifact_schema"
        raise ContractError(code, artifact["path"])
    document = _canonical_resource_document(payload, artifact["path"])
    if artifact["kind"] == "extract":
        validate_extraction(document)
    else:
        try:
            validate_document(document, schemas[schema_id])
        except ValidationError as error:
            code = "artifact_schema"
            raise ContractError(code, artifact["path"]) from error


def _canonical_resource_document(payload: bytes, path: str) -> dict[str, object]:
    document = decode_document(payload, path)
    if payload != encode_json(document, integer_strings=False):
        code = "artifact_document_encoding"
        raise ContractError(code, path)
    return document


def _validate_artifact_envelopes(
    metadata: Mapping[str, object], payloads: Mapping[str, bytes]
) -> None:
    artifacts = _metadata_references(metadata, "artifacts")
    by_path = {entry["path"]: entry for entry in artifacts}
    for entry in artifacts:
        if entry["kind"] != "envelope":
            continue
        envelope = decode_document(payloads[entry["path"]], entry["path"])
        reference = envelope["payload"]
        if not isinstance(reference, dict):
            msg = "stage envelope payload must be a reference"
            raise TypeError(msg)
        target = by_path.get(reference["path"])
        if (
            target is None
            or target["kind"] != envelope["kind"]
            or target["byte_digest"] != reference["byte_digest"]
            or target["logical_digest"] != reference["logical_digest"]
            or (
                target["schema_id"] is not None
                and target["schema_id"] != reference["schema_id"]
            )
            or (
                target["kind"] == "ledger"
                and target["schema_id"] is None
                and reference["schema_id"] != "text/x-beancount"
            )
        ):
            code = "stage_envelope_payload"
            raise ContractError(code, entry["path"])
        inputs = envelope["inputs"]
        if not isinstance(inputs, list):
            msg = "stage envelope inputs must be a list"
            raise TypeError(msg)
        keys = [(edge["role"], edge["ordinal"]) for edge in inputs]
        if len(set(keys)) != len(keys):
            code = "stage_envelope_input_ordinals"
            raise ContractError(code, entry["path"])


def _resource_document(
    reference: dict[str, object],
    payloads: Mapping[str, bytes],
) -> dict[str, object]:
    path = reference["path"]
    if not isinstance(path, str):
        msg = "resource path must be a string"
        raise TypeError(msg)
    if path not in payloads or byte_digest(payloads[path]) != reference["byte_digest"]:
        msg = "missing or corrupt referenced resource"
        raise ValueError(msg)
    return _canonical_resource_document(payloads[path], path)


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


def _metadata_references(metadata: Mapping[str, object], name: str) -> list[dict]:
    value = metadata[name]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        msg = f"{name} must be a list of references"
        raise TypeError(msg)
    return value


def _configuration_path(metadata: Mapping[str, object]) -> str:
    reference = metadata["configuration"]
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        msg = "configuration must have a path"
        raise TypeError(msg)
    return reference["path"]


def _output_entries(
    outputs: Sequence[OutputPayload],
    reserved: Mapping[str, bytes],
    schemas: Mapping[str, dict],
) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    payloads: dict[str, bytes] = {}
    for output in outputs:
        require_digest(output.logical_digest)
        require_filename(output.path)
        if output.logical_digest != byte_digest(output.content):
            msg = "opaque output logical digest must match its bytes"
            raise ValueError(msg)
        if output.kind not in {
            "extract",
            "model",
            "checks",
            "ledger",
            "summary",
            "evidence",
            "inputs",
            "envelope",
            "project",
            "execution",
        }:
            msg = "unsupported output kind"
            raise ValueError(msg)
        if (
            output.path in reserved
            or output.path in payloads
            or output.path
            in {
                "manifest.json",
                "descriptor.json",
                "build.parquet",
            }
        ):
            msg = "duplicate output payload path"
            raise ValueError(msg)
        entry: dict[str, object] = {
            "kind": output.kind,
            "path": output.path,
            "byte_digest": byte_digest(output.content),
            "logical_digest": output.logical_digest,
            "schema_id": output.schema_id,
        }
        _check_interpretation(entry, output.content, schemas)
        payloads[output.path] = output.content
        entries.append(entry)
    return payloads, entries


def _write_tables(
    root: Path,
    tables: Mapping[str, list[dict[str, object]]],
    settings: WriterSettings,
) -> list[TableArtifact]:
    entries: list[TableArtifact] = []
    definitions = {table["name"]: table for table in tabular_schema()["tables"]}
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
    schemas = _schema_resources(inputs.extra_schemas)
    payloads, schema_entries = _resource_payloads(resources, schemas)
    schema = schemas["tabular_schema"]
    schema_digest = byte_digest(payloads["tabular_schema.json"])
    expected_names = {table.name for table in catalog()} - {"build"}
    if set(tables) != expected_names:
        msg = "snapshot requires exactly every non-build table"
        raise ValueError(msg)
    _validate_metadata(metadata)
    if not any(
        item
        == {
            "component": inputs.execution.producer.name,
            "version": inputs.execution.producer.version,
            "content_digest": inputs.execution.producer.content_digest,
        }
        for item in _metadata_references(metadata, "toolchain")
    ):
        msg = "execution producer is not in retained toolchain"
        raise ValueError(msg)
    writer = _writer_document(settings, inputs.writer_version)
    writer_digest = byte_digest(encode_json(writer, integer_strings=True))
    schema_fingerprint = byte_digest(encode_json(schemas, integer_strings=False))
    build_rows = _build_rows(
        builds,
        schema_digest,
        schema_fingerprint,
        writer_digest,
        inputs.execution,
    )
    complete = {**tables, "build": build_rows}
    validate_tables(complete)
    _check_resources(metadata, resources, complete, schemas)
    output_bytes, output_entries = _output_entries(
        inputs.output_payloads, payloads, schemas
    )
    manifest_metadata = {
        **metadata,
        "artifacts": [*_metadata_references(metadata, "artifacts"), *output_entries],
    }
    _validate_artifact_envelopes(manifest_metadata, {**resources, **output_bytes})
    for filename, payload in payloads.items():
        (root / filename).write_bytes(payload)
    for filename, payload in output_bytes.items():
        (root / filename).write_bytes(payload)
    table_entries = _write_tables(root, tables, settings)
    table_checksums = [
        {"path": entry["path"], "byte_digest": entry["byte_digest"]}
        for entry in table_entries
    ]
    financial_digest = _financial_digest(
        schemas,
        table_entries,
        [(build.entity_id, build.as_of) for build in builds],
        manifest_metadata,
        {**resources, **output_bytes},
    )
    manifest: dict[str, object] = {
        **manifest_metadata,
        "schema_version": 1,
        "entities": [
            {key: value for key, value in row.items() if key != "manifest_digest"}
            for row in build_rows
        ],
        "tables": table_entries,
        "schemas": schema_entries,
        "writer_settings": writer,
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
                {"path": filename, "byte_digest": byte_digest(payload)}
                for filename, payload in output_bytes.items()
            ]
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
    entities: Sequence[tuple[str, str]],
    metadata: Mapping[str, object],
    resources: Mapping[str, bytes],
) -> str:
    source_bindings = metadata["bindings"]
    if not isinstance(source_bindings, list):
        msg = "validated source bindings must be a list"
        raise TypeError(msg)
    bindings = [
        {
            **binding,
            "account_mappings": sorted(
                binding["account_mappings"],
                key=lambda mapping: encode_json(mapping, integer_strings=True),
            ),
        }
        for binding in source_bindings
    ]
    configuration_path = _configuration_path(metadata)
    configuration = decode_document(resources[configuration_path], configuration_path)
    configuration_contract = schemas["configuration_schema"]
    financial_configuration_fields = (
        "as_of",
        "source_authority",
        "declaration_source_order",
    )
    semantic_schemas = {
        name: schemas[name]
        for name in (
            "tabular_schema",
            "declaration_schema",
            "registry_schema",
            "extraction_schema",
            "derivation_definitions_schema",
        )
    }
    semantic_schemas["configuration_schema"] = {
        **configuration_contract,
        "properties": {
            name: configuration_contract["properties"][name]
            for name in financial_configuration_fields
        },
        "required": list(financial_configuration_fields),
    }
    extracts = [
        {
            "kind": ref["kind"],
            "document": decode_document(resources[ref["path"]], ref["path"]),
        }
        for ref in _metadata_references(metadata, "artifacts")
        if ref["kind"] == "extract"
    ]
    distinct_extracts = {
        encode_json(extract, integer_strings=True): extract for extract in extracts
    }
    return byte_digest(
        encode_json(
            {
                "entities": [[entity_id, as_of] for entity_id, as_of in entities],
                "schemas": semantic_schemas,
                "bindings": sorted(
                    bindings,
                    key=lambda binding: encode_json(binding, integer_strings=True),
                ),
                "tables": [
                    {"name": item["name"], "logical_digest": item["logical_digest"]}
                    for item in entries
                ],
                "resources": {
                    "configuration": {
                        field: configuration[field]
                        for field in financial_configuration_fields
                    },
                    "registries": sorted(
                        (
                            decode_document(resources[ref["path"]], ref["path"])
                            for ref in _metadata_references(metadata, "registries")
                        ),
                        key=lambda document: encode_json(
                            document, integer_strings=True
                        ),
                    ),
                    "derivation_definitions": _validate_derivation_definitions(
                        metadata, resources, schemas
                    )["definitions"],
                    "interpretation": [
                        distinct_extracts[key] for key in sorted(distinct_extracts)
                    ],
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
