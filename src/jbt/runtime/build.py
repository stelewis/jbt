"""Compose the fixed offline graph into one complete publication candidate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.metadata import version
from typing import TYPE_CHECKING

from jbt.artifacts.execution import ExecutionFacts, InputEdge, Producer
from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.snapshot import (
    BuildInput,
    OutputPayload,
    SnapshotInput,
    SnapshotResult,
    assemble_snapshot,
)
from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.schemas import envelope_schema, validate_document
from jbt.contracts.validation import validate_tables
from jbt.domain.canonical import encode_json
from jbt.domain.cash import CashCheck, source_checks
from jbt.importers.ofx import OfxContext, extract, to_extraction
from jbt.runtime.inputs import require_source_size
from jbt.runtime.outputs import check_document, ledger, summary
from jbt.runtime.project import Project, SourceSelection, document, objects, text
from jbt.runtime.record import CASH_DERIVATIONS, ExtractInput, bind_sources, make_record
from jbt.runtime.stages import Stage, StageResult, StageRunner
from jbt.storage.local import read_object, read_retained

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.inputs import InputSnapshot


REQUIRED_CHECK_KINDS = frozenset(
    {"source_reconciliation", "model_reconciliation", "occurrence_accounting"}
)


@dataclass(frozen=True, slots=True)
class BuildResult:
    """A validated but unpublished candidate and transient execution details."""

    snapshot: SnapshotResult
    executed: tuple[str, ...]
    reused: tuple[str, ...]


def _json(value: object) -> bytes:
    return encode_json(value, integer_strings=False)


def _decode_tables(value: object) -> dict[str, list[dict[str, object]]]:
    return {
        name: [dict(row) for row in objects(rows, name)]
        for name, rows in document(value).items()
    }


def _findings(
    project: Project, checks: tuple[CashCheck, ...]
) -> list[dict[str, object]]:
    position = project.one("position").decode()
    results: list[dict[str, object]] = []
    for check in checks:
        item = check_document(
            check,
            entity_id=project.entity.value,
            commodity_id=text(position["commodity_id"], "commodity"),
        )
        item["scope"] = [{"table": "accounts", "key": [position["account_id"]]}]
        item["policy_declaration_ids"] = [project.one("assertion_scope").revision]
        results.append(item)
    return results


def _model_payload(project: Project, extracts: tuple[ExtractInput, ...]) -> bytes:
    record = make_record(project, extracts)
    return _json(
        {
            "schema_version": 1,
            "tables": record.tables,
            "checks": _findings(project, record.checks),
            "derivations": list(record.derivations),
        }
    )


def _source_payload(project: Project, extracts: tuple[ExtractInput, ...]) -> bytes:
    _, opening, statements = bind_sources(project, extracts)
    return _json(
        {
            "schema_version": 1,
            "checks": _findings(project, source_checks(opening, statements)),
        }
    )


def _source_binding(selection: SourceSelection) -> dict[str, object]:
    return {
        "source_scope_id": selection.source_scope_id,
        "source_account_identifier": selection.source_account_identifier,
        "account_id": selection.account_id,
        "role": selection.role.value,
    }


def _selected_bindings(project: Project) -> list[dict[str, object]]:
    unique = {
        _json(_source_binding(selection)): _source_binding(selection)
        for selection in project.sources
    }
    return [unique[key] for key in sorted(unique)]


def _source_configuration(project: Project) -> dict[str, object]:
    opening = project.one("authored_fact").decode()
    return {
        "account": project.one("account").decode(),
        "position": project.one("position").decode(),
        "commodity": project.one("commodity").decode(),
        "scope": project.one("assertion_scope").decode(),
        "opening_evidence": opening["evidence"],
        "opening_event_key": opening["event_key"],
        "sources": _selected_bindings(project),
    }


def _model_configuration(project: Project) -> dict[str, object]:
    return {
        "declarations": [
            {
                "key": item.key,
                "revision": item.revision,
                "payload": item.decode(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            }
            for item in project.declarations
        ],
        "sources": _selected_bindings(project),
        "as_of": project.configuration_document()["as_of"],
        "registries": decode_document(project.registries, "registries"),
    }


def _derivations(
    project: Project, identifiers: object
) -> tuple[list[dict[str, object]], bytes]:
    supported = {
        "cash-quantity-v1": (
            ("account", "position", "commodity", "authored_fact", "assertion_scope"),
            "quantity",
        ),
        "cash-rule-v1": (("rule", "category"), "category"),
    }
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or not all(isinstance(item, str) for item in identifiers)
        or len(identifiers) != len(set(identifiers))
        or not set(identifiers) <= supported.keys() & CASH_DERIVATIONS.keys()
    ):
        code = "derivation_inventory"
        raise ContractError(code, "build")
    definitions = {
        identifier: CASH_DERIVATIONS[identifier] for identifier in sorted(identifiers)
    }
    entries = [
        {
            "derivation_id": identifier,
            "algorithm": identifier.rsplit("-v", 1)[0],
            "version": str(definitions[identifier]["version"]),
            "declaration_ids": sorted(
                item.revision
                for item in project.declarations
                if item.kind in supported[identifier][0]
            ),
            "units": supported[identifier][1],
            "content_digest": byte_digest(_json(definitions[identifier])),
        }
        for identifier in sorted(identifiers)
    ]
    return entries, _json({"schema_version": 1, "definitions": definitions})


def _effective_configuration(
    project: Project, checks: list[dict[str, object]]
) -> dict[str, object]:
    configuration = project.configuration_document()
    outputs: list[dict[str, object]] = []
    observed = {check["check_kind"] for check in checks}
    require(observed >= REQUIRED_CHECK_KINDS, "required_check_missing", "outputs")
    require(
        all(
            check["status"] == "pass"
            if check["check_kind"] != "coverage"
            else check["status"] in {"pass", "not_evaluable"}
            for check in checks
        ),
        "required_check_failed",
        "outputs",
    )
    for output in objects(configuration["outputs"], "outputs"):
        require(
            output["sink"] in {"tabular", "beancount", "summary"},
            "sink_unsupported",
            "outputs",
        )
        selectors = output["required_checks"]
        require(
            isinstance(selectors, list)
            and len(selectors) == len(REQUIRED_CHECK_KINDS)
            and set(selectors) == REQUIRED_CHECK_KINDS,
            "required_check_policy",
            "outputs",
        )
        outputs.append(
            {
                "sink": output["sink"],
                "required_checks": sorted(
                    text(check["check_id"], "check")
                    for check in checks
                    if check["check_kind"] in REQUIRED_CHECK_KINDS
                ),
            }
        )
    require(
        len({item["sink"] for item in outputs}) == len(outputs)
        and any(item["sink"] == "tabular" for item in outputs),
        "output_selection",
        "outputs",
    )
    return {**configuration, "outputs": outputs}


def _validate_model(
    project: Project, tables: dict[str, list[dict[str, object]]], fingerprint: str
) -> None:
    build = {
        "entity_id": project.entity.value,
        "manifest_digest": "0" * 64,
        "producer_version": version("jbt"),
        "schema_version": 1,
        "schema_digest": byte_digest(_json(tabular_schema())),
        "as_of": project.configuration_document()["as_of"],
        "execution_fingerprint": fingerprint,
    }
    validate_tables({**tables, "build": [build]})


def _bindings(
    project: Project, tables: dict[str, list[dict[str, object]]]
) -> list[dict[str, object]]:
    position = project.one("position").decode()
    bindings: list[dict[str, object]] = []
    for scope in sorted({source.source_scope_id for source in project.sources}):
        sources = tuple(
            source for source in project.sources if source.source_scope_id == scope
        )
        require(
            len(
                {
                    (source.account_id, source.source_account_identifier)
                    for source in sources
                }
            )
            == 1,
            "source_binding_conflict",
            "bindings",
        )
        sections = sorted(
            {
                text(row["source_section"], "source_section")
                for row in tables["source_records"]
                if row["source_scope_id"] == scope
            }
        )
        bindings.append(
            {
                "entity_id": project.entity.value,
                "source_scope_id": scope,
                "importer_id": "ofx",
                "account_mappings": [
                    {
                        "source_section": section,
                        "source_account_identifier": sources[
                            0
                        ].source_account_identifier,
                        "account_id": sources[0].account_id,
                        "source_position_key": None,
                        "position_id": position["position_id"],
                    }
                    for section in sections
                ],
            }
        )
    return bindings


def _recovery(snapshot: InputSnapshot) -> dict[str, object]:
    acquisitions = {item.object_digest: item for item in snapshot.acquisitions}
    return {
        "vault_objects": [
            {
                "object_id": digest,
                "path": f"objects/{digest}",
                "digest_algorithm": "sha256",
                "digest": digest,
                "size_bytes": item.size,
            }
            for digest, item in sorted(acquisitions.items())
        ],
        "accession_records": [
            {
                "accession_id": item.accession_id,
                "object_id": item.object_digest,
                "acquired_at": item.acquired_at,
                "original_name": item.original_filename,
                "source_scope_id": item.source_scope_id,
                "acquisition_method": "local_copy",
            }
            for item in snapshot.acquisitions
        ],
        "authored_history": [
            {
                "path": f"inputs/{snapshot.project_digest}",
                "byte_digest": snapshot.project_digest,
                "revision_id": snapshot.project_digest,
            }
        ],
        "dependencies": [],
        "external_assets": [],
        "runtime": [],
    }


def execute(  # noqa: PLR0913, PLR0915 - explicit replay graph and context
    root: Path,
    snapshot: InputSnapshot,
    project: Project,
    destination: Path,
    *,
    execution: ObservedExecution,
    cache: Path | None,
    baseline: Mapping[str, Sequence[dict[str, object]]] | None,
) -> BuildResult:
    """Execute only captured inputs; the caller owns writer exclusion."""
    facts = ExecutionFacts(
        Producer("jbt", execution.producer_version, execution.producer_digest),
        execution.digest,
    )
    runner = StageRunner(cache if execution.is_release else None, facts)
    extracts: list[ExtractInput] = []
    outputs: list[OutputPayload] = []
    unique_extracts: dict[tuple[str, str, str], StageResult] = {}
    for selection, acquisition in zip(
        project.sources, snapshot.acquisitions, strict=True
    ):
        require_source_size(acquisition)
        data = read_object(root, acquisition)
        importer_id = acquisition.importer_id
        if importer_id is None:
            code = "importer_id_required"
            raise ContractError(code, "build")
        key = (
            acquisition.object_digest,
            selection.source_scope_id,
            importer_id,
        )
        result = unique_extracts.get(key)
        if result is None:
            stage = Stage(
                "extract",
                "extraction_schema",
                byte_digest(
                    _json(
                        {
                            "source_scope_id": selection.source_scope_id,
                            "importer_id": importer_id,
                        }
                    )
                ),
                (InputEdge("source", 0, "source", acquisition.object_digest),),
            )
            context = OfxContext(
                selection.source_scope_id, acquisition.object_digest, importer_id
            )
            result = runner.run(
                stage,
                lambda data=data, context=context: _json(
                    to_extraction(extract(data), context)
                ),
            )
            unique_extracts[key] = result
        extracts.append(ExtractInput(selection, result.payload))
    unique_edges: list[InputEdge] = []
    for ordinal, (_, result) in enumerate(sorted(unique_extracts.items())):
        unique_edges.append(
            InputEdge("extract", ordinal, "extract", byte_digest(result.payload))
        )
        outputs.extend(_stage_outputs(result, "extract", f"extract-{ordinal}"))
    retained_extracts = tuple(
        sorted(
            extracts,
            key=lambda item: (
                item.selection.source_scope_id,
                item.selection.role.value,
                item.selection.account_id,
                item.selection.source_account_identifier,
                byte_digest(item.payload),
            ),
        )
    )
    execution_project = replace(
        project, sources=tuple(item.selection for item in retained_extracts)
    )
    bind_sources(execution_project, retained_extracts)
    edges = tuple(unique_edges)
    source_result = runner.run(
        Stage(
            "checks",
            "checks_artifact_schema",
            byte_digest(_json(_source_configuration(execution_project))),
            edges,
        ),
        lambda: _source_payload(execution_project, retained_extracts),
    )
    model_result = runner.run(
        Stage(
            "model",
            "model_artifact_schema",
            byte_digest(_json(_model_configuration(execution_project))),
            edges,
        ),
        lambda: _model_payload(execution_project, retained_extracts),
    )
    model = document(decode_document(model_result.payload, "model"))
    tables = _decode_tables(model["tables"])
    _validate_model(project, tables, model_result.fingerprint)
    source_document = document(decode_document(source_result.payload, "source-checks"))
    checks: list[dict[str, object]] = [
        dict(item) for item in objects(source_document["checks"], "checks")
    ]
    checks.extend(dict(item) for item in objects(model["checks"], "checks"))
    checks.append(
        {
            "check_id": "structure",
            "check_kind": "schema",
            "entity_id": project.entity.value,
            "status": "pass",
            "severity": "error",
            "expected": {"kind": "boolean", "value": True},
            "observed": {"kind": "boolean", "value": True},
            "scope": [],
            "evidence": [],
            "policy_declaration_ids": [],
            "exception_declaration_id": None,
        }
    )
    configuration = _effective_configuration(project, checks)
    outputs.extend(_stage_outputs(source_result, "checks", "source-checks"))
    outputs.extend(_stage_outputs(model_result, "model", "model"))
    configured = {item["sink"] for item in objects(configuration["outputs"], "outputs")}
    if "beancount" in configured:
        result = runner.run(
            Stage(
                "ledger",
                "text/x-beancount",
                byte_digest(_json(configuration["rendering"])),
                (InputEdge("model", 0, "model", byte_digest(model_result.payload)),),
            ),
            lambda: ledger(project, tables),
        )
        outputs.extend(
            _stage_outputs(result, "ledger", "ledger", extension="beancount")
        )
    if "summary" in configured:
        result = runner.run(
            Stage(
                "summary",
                "summary_artifact_schema",
                byte_digest(
                    _json(
                        {
                            "as_of": configuration["as_of"],
                            "baseline": snapshot.baseline_digest,
                        }
                    )
                ),
                (
                    InputEdge("model", 0, "model", byte_digest(model_result.payload)),
                    InputEdge("checks", 0, "checks", byte_digest(_json(checks))),
                ),
            ),
            lambda: summary(
                project,
                tables,
                checks=checks,
                baseline=baseline,
                baseline_digest=snapshot.baseline_digest,
            ),
        )
        outputs.extend(_stage_outputs(result, "summary", "summary"))
    selector = _json(
        {
            "schema_version": 1,
            "snapshot_digest": snapshot.digest,
            "selection": decode_document(
                read_retained(root, snapshot.digest), "inputs"
            ),
        }
    )
    outputs.append(
        OutputPayload("inputs", "inputs.json", selector, byte_digest(selector))
    )
    outputs.append(
        OutputPayload(
            "execution",
            "execution.json",
            execution.payload,
            execution.digest,
            schema_id="execution_record_schema",
        )
    )
    configuration_bytes = _json(configuration)
    derivations, definitions = _derivations(project, model["derivations"])
    definitions_digest = byte_digest(definitions)
    resources = {
        "configuration.json": configuration_bytes,
        "registries.json": project.registries,
        "derivations.json": definitions,
    }
    configured_outputs = objects(configuration["outputs"], "outputs")
    capabilities: list[dict[str, object]] = [
        {
            "capability_id": key,
            "stage": stage,
            "status": "supported",
            "event_families": ["cash"],
            "required_checks": [],
        }
        for key, stage in (("ofx-cash", "extraction"), ("cash-quantity", "model"))
    ]
    capabilities.extend(
        {
            "capability_id": f"{output['sink']}-cash",
            "stage": "sink",
            "status": "supported",
            "event_families": ["cash"],
            "required_checks": output["required_checks"],
        }
        for output in configured_outputs
    )
    metadata = {
        "artifacts": [],
        "configuration": {
            "path": "configuration.json",
            "byte_digest": byte_digest(configuration_bytes),
        },
        "derivation_definitions": {
            "path": "derivations.json",
            "byte_digest": definitions_digest,
        },
        "registries": [
            {
                "registry_id": "source",
                "schema_id": "registry_schema",
                "path": "registries.json",
                "byte_digest": byte_digest(project.registries),
                "content_digest": byte_digest(
                    encode_json(
                        decode_document(project.registries, "registries"),
                        integer_strings=True,
                    )
                ),
            }
        ],
        "effective_declarations": [
            {"entity_id": project.entity.value, "declaration_id": item.revision}
            for item in project.declarations
        ],
        "bindings": _bindings(project, tables),
        "recovery_set": _recovery(snapshot),
        "inputs": [
            {
                "role": "retained-input-snapshot",
                "ordinal": 0,
                "kind": "inputs",
                "digest": snapshot.digest,
            }
        ],
        "toolchain": [
            {
                "component": facts.producer.name,
                "version": facts.producer.version,
                "content_digest": facts.producer.content_digest,
            }
        ],
        "derivations": derivations,
        "capabilities": capabilities,
        "outputs": configuration["outputs"],
        "checks": checks,
    }
    result = assemble_snapshot(
        destination,
        SnapshotInput(
            tables=tables,
            builds=[
                BuildInput(
                    project.entity.value,
                    version("jbt"),
                    text(configuration["as_of"], "as_of"),
                    snapshot.digest,
                )
            ],
            metadata=metadata,
            resources=resources,
            execution=facts,
            writer_version=version("pyarrow"),
            output_payloads=outputs,
        ),
        WriterSettings(
            compression=Compression.ZSTD,
            row_group_size=100,
            dictionary=False,
            statistics=True,
        ),
    )
    return BuildResult(result, tuple(runner.executed), tuple(runner.reused))


def _stage_outputs(
    result: StageResult,
    kind: str,
    name: str,
    *,
    extension: str = "json",
) -> list[OutputPayload]:
    path = f"{name}.{extension}"
    envelope = document(decode_document(result.envelope, "envelope.json"))
    payload = document(envelope["payload"])
    require(
        payload["byte_digest"] == byte_digest(result.payload),
        "stage_envelope_payload_mismatch",
        name,
    )
    envelope["payload"] = {**payload, "path": path}
    validate_document(envelope, envelope_schema())
    envelope_bytes = _json(envelope)
    schema_id = {
        "extract": "extraction_schema",
        "checks": "checks_artifact_schema",
        "model": "model_artifact_schema",
        "summary": "summary_artifact_schema",
    }.get(kind)
    require(schema_id is not None or kind == "ledger", "unknown_stage_output", kind)
    require(
        payload["schema_id"] == (schema_id or "text/x-beancount"),
        "stage_schema_mismatch",
        kind,
    )
    return [
        OutputPayload(
            kind,
            path,
            result.payload,
            byte_digest(result.payload),
            schema_id=schema_id,
        ),
        OutputPayload(
            "envelope",
            f"{name}-envelope.json",
            envelope_bytes,
            byte_digest(envelope_bytes),
            schema_id="envelope_schema",
        ),
    ]
