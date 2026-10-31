"""Compose the fixed offline graph into one complete publication candidate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.metadata import version
from typing import TYPE_CHECKING

from jbt.artifacts.attempt import write_target
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
from jbt.checks.quantity import Check, CheckKind
from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.schemas import envelope_schema, validate_document
from jbt.contracts.validation import validate_tables
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    AssignBody,
    DocumentLinkBody,
    IdentityBody,
    LifecycleLinkBody,
    SplitBody,
    StatementRevisionBody,
    TransferBody,
)
from jbt.domain.journal import encode_entry, encode_head, entry_digest
from jbt.extraction import ExtractionContext, decode, extract
from jbt.model.corrections import Decision, fold
from jbt.model.decision_merge import fold as merge_fold
from jbt.model.links import fold as link_fold
from jbt.model.proposals import proposals
from jbt.model.rules import enrich
from jbt.runtime.checks import (
    projected_account_checks,
    projected_model_checks,
    retained_source_checks,
)
from jbt.runtime.decisions import selected_decisions
from jbt.runtime.execution import select_importers
from jbt.runtime.inputs import require_source_size, source_importers
from jbt.runtime.outputs import check_document, ledger, summary
from jbt.runtime.project import (
    Project,
    SourceSelection,
    document,
    objects,
    strings,
    text,
)
from jbt.runtime.resolution import (
    QUANTITY_DERIVATIONS,
    ExtractInput,
    prepare,
    project_resolution,
    rule_evidence,
)
from jbt.runtime.stages import Stage, StageResult, StageRunner
from jbt.runtime.temporal import timezone_provenance
from jbt.storage.local import read_object, read_retained

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from jbt.domain.journal import JournalSelection
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.inputs import InputSnapshot


@dataclass(frozen=True, slots=True)
class BuildResult:
    """A validated but unpublished candidate and transient execution details."""

    snapshot: SnapshotResult
    executed: tuple[str, ...]
    reused: tuple[str, ...]


def review_document(snapshot: InputSnapshot) -> dict[str, object]:
    """Name captured financial inputs without including the report's seal."""
    return {
        "schema_version": 1,
        "attempt_digest": snapshot.digest,
        "selection": {
            "input_digest": snapshot.digest,
            "journal_head": decode_document(encode_head(snapshot.journal.head), "head"),
            "journal_headers": list(snapshot.journal_headers),
            "journal_bases": list(snapshot.journal_bases),
            "uncommitted_sequences": list(snapshot.journal_uncommitted),
        },
        "model": None,
        "checks": [],
        "failure": None,
    }


def _json(value: object) -> bytes:
    return encode_json(value, integer_strings=False)


def _decode_tables(value: object) -> dict[str, list[dict[str, object]]]:
    return {
        name: [dict(row) for row in objects(rows, name)]
        for name, rows in document(value).items()
    }


def _check_policy(project: Project) -> list[str]:
    configuration = project.configuration_document()
    selectors = {
        text(kind, "required_check")
        for output in objects(configuration["outputs"], "outputs")
        for kind in strings(output["required_checks"], "required_checks")
    }
    require(
        selectors <= {kind.value for kind in CheckKind}, "check_unsupported", "outputs"
    )
    return sorted(selectors)


def _findings(project: Project, checks: tuple[Check, ...]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for check in checks:
        item = check_document(
            check,
            entity_id=project.entity.value,
            commodity_id=check.commodity.value if check.commodity else None,
        )
        if check.account is not None:
            item["scope"] = [{"table": "accounts", "key": [check.account.value]}]
        item["policy_declaration_ids"] = (
            sorted(policy.value for policy in check.policies)
            if check.kind in {CheckKind.COVERAGE, CheckKind.LIFECYCLE}
            else [
                d.revision
                for d in project.declarations
                if d.kind == "assertion_scope"
                and (
                    check.account is None
                    or d.decode()["account_id"] == check.account.value
                )
            ]
        )
        results.append(item)
    return results


def _model_payload(
    project: Project,
    extracts: tuple[ExtractInput, ...],
    decisions: tuple[Decision, ...],
    journal: JournalSelection,
    documents: tuple[str, ...],
) -> bytes:
    prepared = prepare(project, extracts)
    merged = merge_fold(
        prepared.inputs,
        tuple(
            decision
            for decision in decisions
            if isinstance(decision.body, IdentityBody | StatementRevisionBody)
        ),
    )
    enriched = fold(
        merged.inputs,
        enrich(merged.inputs, merged.resolution),
        tuple(
            decision
            for decision in decisions
            if isinstance(decision.body, AssignBody | SplitBody)
        ),
    )
    linked = link_fold(
        merged.inputs,
        enriched.resolution,
        tuple(
            decision
            for decision in decisions
            if isinstance(
                decision.body, TransferBody | DocumentLinkBody | LifecycleLinkBody
            )
        ),
        documents,
    )
    resolution = linked.resolution
    for entry in journal.committed_entries:
        prepared.rows.add(
            "declarations",
            {
                "declaration_id": entry.revision_id.value,
                "declaration_key": entry.key.value,
                "declaration_kind": entry.kind.value,
                "revision_digest": entry.revision_id.value,
                "document_path": f"journal/entries/{entry.sequence:019d}.json",
                "document_locator": f"journal:{entry.sequence}",
                "payload_json": encode_entry(entry).decode().strip(),
                "valid_from": None,
                "valid_to": None,
            },
            evidence=None,
        )
    tables = project_resolution(replace(prepared, inputs=merged.inputs), resolution)
    review = {
        "tail": [write_target(target) for target in (*merged.tail, *linked.tail)],
        "receipts": [
            {
                "decision_key": receipt.entry.key.value,
                "entry_digest": entry_digest(receipt.entry),
                "expected": write_target(receipt.expected),
                "candidates": [write_target(target) for target in receipt.candidates],
                "restore": [
                    {
                        "entity_id": reference.entity.value,
                        "table": reference.table.value,
                        "key": list(reference.key),
                        "candidates": [write_target(target) for target in candidates],
                    }
                    for reference, candidates in receipt.restore
                ],
                "outcome": receipt.evaluation.outcome.value,
                "expected_digest": receipt.evaluation.guard.expected_digest,
                "observed_digest": receipt.evaluation.observed_digest,
            }
            for receipt in (*merged.receipts, *enriched.receipts, *linked.receipts)
        ],
        "rules": rule_evidence(resolution),
        "counts": {
            "pending_events": sum(
                event.state.value == "pending" for event in resolution.events
            ),
            "blocked_records": sum(
                d.status.value == "blocked" for d in resolution.determinations
            ),
        },
        "proposals": [proposal.document() for proposal in proposals(resolution)],
    }
    return _json(
        {
            "schema_version": 1,
            "tables": tables,
            "findings": [
                {
                    "code": f.code.value,
                    "scope": f.scope.value,
                    "blocking": f.blocking,
                    "severity": f.severity,
                }
                for f in resolution.findings
            ],
            "determinations": [
                {"record_id": d.identity.value, "status": d.status.value}
                for d in resolution.determinations
            ],
            "derivations": list(QUANTITY_DERIVATIONS),
            "review": review,
        }
    )


def _source_payload(project: Project, extracts: tuple[ExtractInput, ...]) -> bytes:
    return _json(
        {
            "schema_version": 1,
            "checks": _findings(project, retained_source_checks(project, extracts))
            if CheckKind.SOURCE in _check_policy(project)
            else [],
        }
    )


def _source_binding(selection: SourceSelection) -> dict[str, object]:
    return {"accession_id": selection.accession_id}


def _selected_bindings(project: Project) -> list[dict[str, object]]:
    unique = {
        _json(_source_binding(selection)): _source_binding(selection)
        for selection in project.sources
    }
    return [unique[key] for key in sorted(unique)]


def _source_configuration(project: Project) -> dict[str, object]:
    return {**_model_configuration(project), "check_policy": _check_policy(project)}


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
        "configuration": project.configuration_document(),
        "registries": decode_document(project.registries, "registries"),
    }


def _derivations(
    project: Project, identifiers: object
) -> tuple[list[dict[str, object]], bytes]:
    supported = {
        "quantity-v1": (
            ("account", "position", "commodity", "authored_fact", "assertion_scope"),
            "quantity",
        ),
        "enrichment-rule-v1": (("rule", "category"), "none"),
        "transfer-v1": (("account", "position"), "quantity"),
    }
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or not all(isinstance(item, str) for item in identifiers)
        or len(identifiers) != len(set(identifiers))
        or not set(identifiers) <= supported.keys() & QUANTITY_DERIVATIONS.keys()
    ):
        code = "derivation_inventory"
        raise ContractError(code, "build")
    definitions = {
        identifier: QUANTITY_DERIVATIONS[identifier]
        for identifier in sorted(identifiers)
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
    require(
        all(
            check["status"] == "pass"
            for check in checks
            if check["check_kind"] == CheckKind.TRANSFER.value
        ),
        "transfer_conservation_failed",
        "outputs",
    )
    require(
        all(
            check["status"] == "pass"
            for check in checks
            if check["check_kind"] == CheckKind.LIFECYCLE.value
        ),
        "account_lifecycle_failed",
        "outputs",
    )
    configuration = project.configuration_document()
    outputs: list[dict[str, object]] = []
    observed = {check["check_kind"] for check in checks}
    for output in objects(configuration["outputs"], "outputs"):
        require(
            output["sink"] in {"tabular", "beancount", "summary"},
            "sink_unsupported",
            "outputs",
        )
        selectors = strings(output["required_checks"], "required_checks")
        sink = text(output["sink"], "sink")
        require(
            set(selectors) <= {kind.value for kind in CheckKind},
            "required_check_policy",
            "outputs",
        )
        require(set(selectors) <= observed, "required_check_missing", sink)
        require(
            all(c["status"] == "pass" for c in checks if c["check_kind"] in selectors),
            "required_check_failed",
            sink,
        )
        outputs.append(
            {
                "sink": output["sink"],
                "required_checks": sorted(
                    text(check["check_id"], "check")
                    for check in checks
                    if check["check_kind"] in selectors
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
    project: Project,
    snapshot: InputSnapshot,
    extracts: tuple[ExtractInput, ...],
) -> list[dict[str, object]]:
    bindings: list[dict[str, object]] = []
    stated_accounts = {
        (
            text(extract["source_scope_id"], "scope"),
            text(section["section_id"], "section"),
        ): section["source_account_identifier"]
        for selected in extracts
        for extract in (document(decode_document(selected.payload, "extract")),)
        for section in objects(
            document(extract["source_scope"])["sections"], "sections"
        )
    }
    for scope in sorted(
        {text(item.source_scope_id, "source_scope") for item in snapshot.acquisitions}
    ):
        mappings = {}
        for item in project.declarations:
            if item.kind != "account":
                continue
            account = item.decode()
            for binding in objects(account["bindings"], "bindings"):
                if binding["source_scope_id"] != scope:
                    continue
                candidates = tuple(
                    p.decode()
                    for p in project.declarations
                    if p.kind == "position"
                    and p.decode()["account_id"] == account["account_id"]
                )
                for position in candidates:
                    key = (binding["source_section"], position["position_id"])
                    mappings[key] = {
                        "source_section": binding["source_section"],
                        "source_account_identifier": stated_accounts.get(
                            (scope, binding["source_section"])
                        ),
                        "account_id": account["account_id"],
                        "source_position_key": None,
                        "position_id": position["position_id"],
                    }
        bindings.append(
            {
                "entity_id": project.entity.value,
                "source_scope_id": scope,
                "importer_id": next(
                    item.importer_id
                    for item in snapshot.acquisitions
                    if item.source_scope_id == scope
                ),
                "account_mappings": [mappings[key] for key in sorted(mappings)],
            }
        )
    return bindings


def _recovery(snapshot: InputSnapshot) -> dict[str, object]:
    acquisitions = {
        item.object_digest: item
        for item in (*snapshot.acquisitions, *snapshot.documents)
    }
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
            for item in (*snapshot.acquisitions, *snapshot.documents)
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
    review: dict[str, object] | None = None,
) -> BuildResult:
    """Execute only captured inputs; the caller owns writer exclusion."""
    require(
        snapshot.timezone_rules == _json(timezone_provenance()),
        "historical_timezone_rules_mismatch_install_recorded_release",
        "build",
    )
    execution, importers = select_importers(
        execution, source_importers(snapshot.acquisitions)
    )
    importer_records = {
        item["importer_id"]: item for item in execution.document()["importers"]
    }
    for acquisition in snapshot.acquisitions:
        require_source_size(
            acquisition,
            importers[text(acquisition.importer_id, "importer")].maximum_bytes,
        )
    facts = ExecutionFacts(
        Producer("jbt", execution.producer_version, execution.producer_digest),
        execution.digest,
    )
    runner = StageRunner(cache if execution.is_release else None, facts)
    review = review_document(snapshot) if review is None else review
    decisions = selected_decisions(root, snapshot)
    extracts: list[ExtractInput] = []
    outputs: list[OutputPayload] = []
    unique_extracts: dict[tuple[str, str, str], StageResult] = {}
    for selection, acquisition in zip(
        project.sources, snapshot.acquisitions, strict=True
    ):
        data = read_object(root, acquisition)
        importer_id = acquisition.importer_id
        if importer_id is None:
            code = "importer_id_required"
            raise ContractError(code, "build")
        source_scope = text(acquisition.source_scope_id, "source_scope")
        key = (
            acquisition.object_digest,
            source_scope,
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
                            "source_scope_id": acquisition.source_scope_id,
                            "importer_id": importer_id,
                            "interpreter": importer_records[importer_id],
                        }
                    )
                ),
                (InputEdge("source", 0, "source", acquisition.object_digest),),
            )
            context = ExtractionContext(
                source_scope, acquisition.object_digest, importer_id
            )
            result = runner.run(
                stage,
                lambda data=data, context=context: extract(
                    importers[context.importer_id], data, context
                ),
            )
            decode(result.payload, context)
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
            key=lambda item: (byte_digest(item.payload),),
        )
    )
    execution_project = replace(
        project, sources=tuple(item.selection for item in retained_extracts)
    )
    edges = tuple(unique_edges)
    source_result = runner.run(
        Stage(
            "source_checks",
            "checks_artifact_schema",
            byte_digest(
                _json(
                    {
                        **_source_configuration(execution_project),
                        "timezone_rules": decode_document(
                            snapshot.timezone_rules, "timezone_rules"
                        ),
                    }
                )
            ),
            edges,
        ),
        lambda: _source_payload(execution_project, retained_extracts),
    )
    model_result = runner.run(
        Stage(
            "model",
            "model_artifact_schema",
            byte_digest(
                _json(
                    {
                        **_model_configuration(execution_project),
                        "timezone_rules": decode_document(
                            snapshot.timezone_rules, "timezone_rules"
                        ),
                        "journal_headers": list(snapshot.journal_headers),
                        "journal_bases": list(snapshot.journal_bases),
                        "documents": sorted(
                            {item.object_digest for item in snapshot.documents}
                        ),
                    }
                )
            ),
            edges,
        ),
        lambda: _model_payload(
            execution_project,
            retained_extracts,
            decisions,
            snapshot.journal,
            tuple(item.object_digest for item in snapshot.documents),
        ),
    )
    model = document(decode_document(model_result.payload, "model"))
    review["model"] = model
    review["checks"] = document(
        decode_document(source_result.payload, "source-checks")
    )["checks"]
    tables = _decode_tables(model["tables"])
    blocking = tuple(f for f in objects(model["findings"], "findings") if f["blocking"])
    if blocking:
        raise ContractError(
            text(blocking[0]["code"], "finding"), text(blocking[0]["scope"], "scope")
        )
    _validate_model(project, tables, model_result.fingerprint)
    model_check_result = runner.run(
        Stage(
            "model_checks",
            "checks_artifact_schema",
            byte_digest(
                _json(
                    {
                        "check_policy": _check_policy(project),
                        "as_of": project.configuration_document()["as_of"],
                        "declarations": _model_configuration(project)["declarations"],
                    }
                )
            ),
            (InputEdge("model", 0, "model", byte_digest(model_result.payload)),),
        ),
        lambda: _json(
            {
                "schema_version": 1,
                "checks": _findings(
                    project,
                    (
                        *projected_model_checks(
                            tables,
                            frozenset(
                                CheckKind(k)
                                for k in _check_policy(project)
                                if k != CheckKind.SOURCE
                            ),
                        ),
                        *projected_account_checks(project, tables),
                    ),
                ),
            }
        ),
    )
    source_document = document(decode_document(source_result.payload, "source-checks"))
    checks: list[dict[str, object]] = [
        dict(item) for item in objects(source_document["checks"], "checks")
    ]
    model_check_document = document(
        decode_document(model_check_result.payload, "model-checks")
    )
    checks.extend(
        dict(item) for item in objects(model_check_document["checks"], "checks")
    )
    review["checks"] = checks
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
    outputs.extend(_stage_outputs(source_result, "source_checks", "source-checks"))
    outputs.extend(_stage_outputs(model_check_result, "model_checks", "model-checks"))
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
                            "journal": {
                                "sequence": snapshot.journal.head.sequence,
                                "entry_digest": snapshot.journal.head.entry_digest,
                                "uncommitted_sequences": list(
                                    snapshot.journal_uncommitted
                                ),
                            },
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
                journal_state={
                    "sequence": snapshot.journal.head.sequence,
                    "entry_digest": snapshot.journal.head.entry_digest,
                    "active": [
                        {
                            "decision_key": active.entry.key.value,
                            "kind": active.entry.kind.value,
                            "logical_position": active.logical_position,
                        }
                        for active in snapshot.journal.active_decisions
                    ],
                    "uncommitted_sequences": list(snapshot.journal_uncommitted),
                },
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
        ]
        + [
            {
                "entity_id": entry.entry.entity.value,
                "declaration_id": entry.entry.revision_id.value,
            }
            for entry in snapshot.journal.active_decisions
        ],
        "bindings": _bindings(project, snapshot, retained_extracts),
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
        "source_checks": "checks_artifact_schema",
        "model_checks": "checks_artifact_schema",
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
