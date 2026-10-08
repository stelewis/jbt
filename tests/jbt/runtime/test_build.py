"""Build graph boundaries without real source parsing or financial-model fixtures."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, ValidationError

from jbt.artifacts.execution import ExecutionFacts, InputEdge, Producer, stage_envelope
from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.artifacts.reader import read_snapshot
from jbt.artifacts.snapshot import (
    SnapshotInput,
    SnapshotResult,
    _financial_digest,
    _schema_resources,
    _validate_metadata,
)
from jbt.contracts.catalog import catalog
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.cash import model_checks, resolve_cash, source_checks
from jbt.domain.numbers import ExactDecimal
from jbt.importers.ofx import MAX_BYTES
from jbt.runtime import build
from jbt.runtime.inputs import InputSnapshot
from jbt.runtime.project import Project, document, objects, read_project, text
from jbt.runtime.stages import StageResult
from jbt.storage.local import Acquisition

if TYPE_CHECKING:
    from collections.abc import Callable

    from jbt.artifacts.parquet import WriterSettings
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.record import ExtractInput


def payload(value: object) -> bytes:
    return encode_json(value, integer_strings=False)


def project_with_checks(document: dict, *sinks: str) -> Project:
    document["configuration"]["outputs"] = [
        {
            "sink": sink,
            "required_checks": [
                "source_reconciliation",
                "model_reconciliation",
                "occurrence_accounting",
            ],
        }
        for sink in sinks
    ]
    return read_project(payload(document))


def findings() -> list[dict[str, object]]:
    return [
        {
            "check_id": kind,
            "check_kind": kind,
            "entity_id": "invented-owner",
            "status": "not_evaluable" if kind == "coverage" else "pass",
            "severity": "error",
            "expected": {"kind": "boolean", "value": True},
            "observed": (
                {"kind": "unavailable", "reason": "insufficient_evidence"}
                if kind == "coverage"
                else {"kind": "boolean", "value": True}
            ),
            "scope": [],
            "evidence": [],
            "policy_declaration_ids": [],
            "exception_declaration_id": None,
        }
        for kind in (
            "source_reconciliation",
            "model_reconciliation",
            "occurrence_accounting",
            "coverage",
        )
    ]


def test_required_checks_exclude_non_evaluable_coverage(
    project_document: dict,
) -> None:
    project = project_with_checks(project_document, "tabular", "summary")
    effective = build._effective_configuration(project, findings())  # noqa: SLF001
    outputs = effective["outputs"]
    assert isinstance(outputs, list)
    assert all(
        item["required_checks"]
        == sorted(
            ["source_reconciliation", "model_reconciliation", "occurrence_accounting"]
        )
        for item in outputs
    )
    assert not any("coverage" in item["required_checks"] for item in outputs)


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("source_reconciliation", "not_evaluable"),
        ("occurrence_accounting", "fail"),
        ("model_reconciliation", "fail"),
        ("coverage", "fail"),
    ],
)
def test_does_not_publish_failed_required_or_false_coverage(
    project_document: dict,
    kind: str,
    status: str,
) -> None:
    project = project_with_checks(project_document, "tabular")
    checks = findings()
    next(item for item in checks if item["check_kind"] == kind)["status"] = status
    with pytest.raises(ContractError, match="required_check_failed"):
        build._effective_configuration(project, checks)  # noqa: SLF001


def test_compensating_dropped_movements_block_output_authorization(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
) -> None:
    project, extracts = make_inputs(project_document)
    _, opening, (statement,) = build.bind_sources(project, extracts)
    credit, debit = statement.movements
    balanced = replace(
        statement,
        balance=opening.amount,
        movements=(replace(credit, amount=ExactDecimal.parse("5.00")), debit),
    )
    events = resolve_cash(
        project.entity, opening, (balanced,), project.category_rules()
    )
    source = build._findings(  # noqa: SLF001
        project, source_checks(opening, (balanced,))
    )
    model = build._findings(  # noqa: SLF001
        project,
        model_checks(
            opening,
            (balanced,),
            events[:1],
            expected_periods=((balanced.start, balanced.end),),
            as_of=balanced.end,
        ),
    )
    assert all(check["status"] == "pass" for check in source)
    assert all(
        check["status"] == "pass"
        for check in model
        if check["check_kind"] == "model_reconciliation"
    )
    assert any(
        check["status"] == "fail"
        for check in model
        if check["check_kind"] == "occurrence_accounting"
    )
    with pytest.raises(ContractError, match="required_check_failed"):
        build._effective_configuration(project, [*source, *model])  # noqa: SLF001


def test_source_identity_excludes_authored_amount_and_accession(
    project_document: dict,
) -> None:
    project = project_with_checks(project_document, "tabular")
    original_source = build._source_configuration(project)  # noqa: SLF001
    original_model = build._model_configuration(project)  # noqa: SLF001
    authored = next(
        item["payload"]
        for item in project_document["declarations"]
        if item["payload"]["kind"] == "authored_fact"
    )
    authored["record"]["legs"][0]["amount"]["coefficient"] = "999"
    modified = project_with_checks(project_document, "tabular")
    assert build._source_configuration(modified) == original_source  # noqa: SLF001
    assert build._model_configuration(modified) != original_model  # noqa: SLF001
    project_document["sources"].append(
        {
            **project_document["sources"][-1],
            "accession_id": "january-duplicate",
        }
    )
    duplicate = project_with_checks(project_document, "tabular")
    assert build._source_configuration(duplicate) == original_source  # noqa: SLF001
    assert build._model_configuration(duplicate) == build._model_configuration(modified)  # noqa: SLF001
    authored["evidence"] = [{"kind": "source_record", "id": "different-evidence"}]
    changed = project_with_checks(project_document, "tabular")
    assert build._source_configuration(changed) != original_source  # noqa: SLF001


def test_published_envelope_references_published_payload() -> None:
    facts = ExecutionFacts(Producer("jbt", "1", "a" * 64), "a" * 64)
    data = b"ledger\n"
    envelope = payload(
        stage_envelope(
            stage="ledger",
            schema_id="text/x-beancount",
            configuration_digest="b" * 64,
            inputs=(InputEdge("model", 0, "model", "c" * 64),),
            facts=facts,
            path="payload",
            byte_digest_value=byte_digest(data),
            logical_digest=byte_digest(data),
        )
    )
    outputs = build._stage_outputs(  # noqa: SLF001
        StageResult("d" * 64, data, envelope),
        "ledger",
        "ledger",
        extension="beancount",
    )
    assert [output.path for output in outputs] == [
        "ledger.beancount",
        "ledger-envelope.json",
    ]
    published = decode_document(outputs[1].content, "ledger-envelope.json")
    reference = document(published["payload"])
    assert reference["path"] == outputs[0].path
    assert reference["byte_digest"] == byte_digest(data)
    assert reference["schema_id"] == "text/x-beancount"
    assert outputs[0].schema_id is None


def test_registered_stage_schemas_cover_complete_shared_tables_and_outputs() -> None:
    schemas = _schema_resources({})
    for name in (
        "checks_artifact_schema",
        "model_artifact_schema",
        "summary_artifact_schema",
    ):
        schema = schemas[name]
        Draft202012Validator.check_schema(schema)
    table_rows = {table.name: [] for table in catalog() if table.name != "build"}
    model = {
        "schema_version": 1,
        "tables": table_rows,
        "checks": findings(),
        "derivations": ["cash-quantity-v1"],
    }
    validate_document(model, schemas["model_artifact_schema"])
    validate_document(
        {"schema_version": 1, "checks": findings()},
        schemas["checks_artifact_schema"],
    )
    without_table = {
        **model,
        "tables": {
            key: value for key, value in table_rows.items() if key != "transactions"
        },
    }
    with pytest.raises(ValidationError):
        validate_document(without_table, schemas["model_artifact_schema"])
    extra_column = {
        **model,
        "tables": {**table_rows, "transactions": [{"unexpected": True}]},
    }
    with pytest.raises(ValidationError):
        validate_document(extra_column, schemas["model_artifact_schema"])
    report = {
        "schema_version": 1,
        "entity_id": "invented-owner",
        "as_of": "2026-01-31",
        "comparison_baseline": None,
        "monthly_quantities": [],
        "category_flows": [],
        "changes": [],
        "review": {"unreviewed_events": 0, "uncategorized_events": 0},
        "checks": findings(),
    }
    validate_document(report, schemas["summary_artifact_schema"])
    with pytest.raises(ValidationError):
        validate_document(
            {**report, "monthly_quantities": [{"month": "2026-01"}]},
            schemas["summary_artifact_schema"],
        )


def test_derivation_identity_hashes_retained_definitions_not_labels_or_runtime(
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = project_with_checks(project_document, "tabular")
    selected = ["cash-quantity-v1", "cash-rule-v1"]
    entries, retained = build._derivations(project, selected)  # noqa: SLF001
    definition_map = document(
        decode_document(retained, "derivations.json")["definitions"]
    )
    for entry in entries:
        identifier = text(entry["derivation_id"], "derivation_id")
        assert entry["content_digest"] == byte_digest(
            payload(definition_map[identifier])
        )
    assert all(entry["content_digest"] != "a" * 64 for entry in entries)
    changed = {
        identifier: dict(definition)
        for identifier, definition in build.CASH_DERIVATIONS.items()
    }
    changed["cash-quantity-v1"]["ordering"] = "A different semantic ordering."
    monkeypatch.setattr(build, "CASH_DERIVATIONS", changed)
    updated, updated_retained = build._derivations(project, selected)  # noqa: SLF001
    assert updated_retained != retained
    assert updated[0]["content_digest"] != entries[0]["content_digest"]
    assert updated[1]["content_digest"] == entries[1]["content_digest"]


def test_derivation_inventory_retains_only_selected_known_definitions(
    project_document: dict,
) -> None:
    project = project_with_checks(project_document, "tabular")
    entries, retained = build._derivations(  # noqa: SLF001
        project,
        ["cash-rule-v1"],
    )
    assert [entry["derivation_id"] for entry in entries] == ["cash-rule-v1"]
    definitions = document(decode_document(retained, "derivations.json")["definitions"])
    assert set(definitions) == {"cash-rule-v1"}
    for invalid in ([], ["cash-rule-v1", "cash-rule-v1"], ["missing-v1"]):
        with pytest.raises(ContractError, match="derivation_inventory"):
            build._derivations(project, invalid)  # noqa: SLF001


def test_financial_identity_ignores_runtime_and_rendering_but_tracks_definitions(
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = project_with_checks(project_document, "tabular")
    identifiers = ["cash-quantity-v1", "cash-rule-v1"]
    entries, definitions = build._derivations(project, identifiers)  # noqa: SLF001
    metadata: dict[str, object] = {
        "bindings": [],
        "registries": [],
        "artifacts": [],
        "configuration": {"path": "configuration.json"},
        "derivation_definitions": {
            "path": "derivations.json",
            "byte_digest": byte_digest(definitions),
        },
        "effective_declarations": [],
        "derivations": entries,
        "checks": [],
        "toolchain": [{"content_digest": "a" * 64}],
    }
    resources = {
        "configuration.json": project.configuration,
        "derivations.json": definitions,
    }
    schemas = _schema_resources({})

    def financial() -> str:
        return _financial_digest(
            schemas,
            [],
            [(project.entity.value, "2026-01-31")],
            metadata,
            resources,
        )

    original = financial()
    metadata["toolchain"] = [{"content_digest": "b" * 64}]
    rendering_only = project.configuration_document()
    rendering_only["rendering"] = []
    resources["configuration.json"] = payload(rendering_only)
    assert financial() == original
    changed = {
        identifier: dict(definition)
        for identifier, definition in build.CASH_DERIVATIONS.items()
    }
    changed["cash-rule-v1"]["effect"] = "A changed semantic category assignment."
    monkeypatch.setattr(build, "CASH_DERIVATIONS", changed)
    revised, resources["derivations.json"] = build._derivations(  # noqa: SLF001
        project,
        identifiers,
    )
    metadata["derivations"] = revised
    metadata["derivation_definitions"] = {
        "path": "derivations.json",
        "byte_digest": byte_digest(resources["derivations.json"]),
    }
    assert financial() != original


@pytest.mark.parametrize(
    "variation", ["ordinary", "duplicate", "other-namespace", "other-importer"]
)
def test_execute_validates_warm_model_and_selects_only_configured_sinks(  # noqa: PLR0915 - shared graph fixture
    tmp_path: Path,
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
    variation: str,
    release_execution: ObservedExecution,
) -> None:
    if variation == "duplicate":
        project_document["sources"].append(
            {**project_document["sources"][-1], "accession_id": "repeat-january"}
        )
    project = project_with_checks(project_document, "tabular", "summary")
    if variation == "other-namespace":
        project = replace(
            project,
            sources=(
                project.sources[0],
                replace(project.sources[1], source_scope_id="another-feed"),
            ),
        )
    digest = "a" * 64
    acquisitions = tuple(
        Acquisition(
            f"retry-{index}",
            selected.source_scope_id,
            "another-importer"
            if variation == "other-importer" and index == 1
            else "ofx",
            "2026-01-01T00:00:00Z",
            "evidence.ofx",
            digest,
            8,
            selected.accession_id,
        )
        for index, selected in enumerate(project.sources)
    )
    snapshot = InputSnapshot(
        digest, digest, release_execution.digest, acquisitions, None, None
    )
    monkeypatch.setattr(
        build, "read_object", lambda _root, _acquisition: b"same-source"
    )
    monkeypatch.setattr(build, "extract", lambda data: data)
    monkeypatch.setattr(
        build,
        "to_extraction",
        lambda data, context: {
            "data": data.decode(),
            "scope": context.source_scope_id,
            "importer": context.importer_id,
        },
    )
    validated: list[str] = []
    monkeypatch.setattr(
        build,
        "bind_sources",
        lambda _project, extracts: validated.append(f"bindings:{len(extracts)}"),
    )
    source_checks = findings()[:1]
    model_checks = findings()[1:]
    monkeypatch.setattr(
        build,
        "_source_payload",
        lambda _project, _extracts: payload({"checks": source_checks}),
    )
    monkeypatch.setattr(
        build,
        "_model_payload",
        lambda _project, _extracts: payload(
            {
                "tables": {"source_records": []},
                "checks": model_checks,
                "derivations": ["cash-quantity-v1", "cash-rule-v1"],
            }
        ),
    )
    monkeypatch.setattr(
        build,
        "validate_tables",
        lambda tables: validated.append(
            "tables" if "build" in tables else "missing-build"
        ),
    )
    monkeypatch.setattr(build, "summary", lambda *_args, **_kwargs: b"summary\n")
    monkeypatch.setattr(
        build, "ledger", lambda *_args, **_kwargs: pytest.fail("ledger")
    )
    monkeypatch.setattr(
        build,
        "read_retained",
        lambda _root, _digest: payload({"selection": True}),
    )
    captured: list[SnapshotInput] = []

    def assemble(
        _destination: Path, request: SnapshotInput, _settings: object
    ) -> SnapshotResult:
        assert validated[-2:] == [f"bindings:{len(project.sources)}", "tables"]
        _validate_metadata(request.metadata)
        captured.append(request)
        return SnapshotResult(digest, digest, digest)

    monkeypatch.setattr(build, "assemble_snapshot", assemble)
    cache = tmp_path / "cache"
    result = build.execute(
        tmp_path,
        snapshot,
        project,
        tmp_path / "candidate",
        execution=release_execution,
        cache=cache,
        baseline=None,
    )
    warm = build.execute(
        tmp_path,
        snapshot,
        project,
        tmp_path / "candidate-2",
        execution=release_execution,
        cache=cache,
        baseline=None,
    )
    assert result.snapshot.descriptor_digest == digest
    assert {"checks", "model", "summary"} <= set(warm.reused)
    extract_count = 2 if variation.startswith("other-") else 1
    assert result.executed.count("extract") == extract_count
    assert warm.reused.count("extract") == extract_count
    expected = [f"bindings:{len(project.sources)}", "tables"]
    assert validated == expected * 2
    request = captured[0]
    assert request.extra_schemas == {}
    published = {item.path: item for item in request.output_payloads}
    for output in request.output_payloads:
        if output.kind in {"extract", "checks", "model", "summary"}:
            assert output.schema_id in {
                "extraction_schema",
                "checks_artifact_schema",
                "model_artifact_schema",
                "summary_artifact_schema",
            }
        if output.kind == "envelope":
            envelope = document(decode_document(output.content, output.path))
            reference = document(envelope["payload"])
            path = text(reference["path"], "payload.path")
            assert path in published
            assert reference["schema_id"] == published[path].schema_id
    assert {
        entry["sink"] for entry in objects(request.metadata["outputs"], "outputs")
    } == {
        "tabular",
        "summary",
    }
    assert "coverage" in {
        check["check_kind"] for check in objects(request.metadata["checks"], "checks")
    }
    assert {output.kind for output in request.output_payloads} >= {
        "extract",
        "model",
        "checks",
        "summary",
        "envelope",
        "inputs",
    }
    assert (
        sum(output.kind == "extract" for output in request.output_payloads)
        == extract_count
    )
    recovery = document(request.metadata["recovery_set"])
    assert len(objects(recovery["accession_records"], "accessions")) == len(
        project.sources
    )
    assert not any(output.kind == "ledger" for output in request.output_payloads)
    definitions = document(
        decode_document(request.resources["derivations.json"], "derivations.json")[
            "definitions"
        ]
    )
    for item in objects(request.metadata["derivations"], "derivations"):
        identifier = text(item["derivation_id"], "derivation_id")
        assert item["content_digest"] == byte_digest(payload(definitions[identifier]))
    assert request.metadata["artifacts"] == []
    assert request.metadata["derivation_definitions"] == {
        "path": "derivations.json",
        "byte_digest": byte_digest(request.resources["derivations.json"]),
    }
    assert request.resources["derivations.json"] == payload(
        {
            "schema_version": 1,
            "definitions": build.CASH_DERIVATIONS,
        }
    )
    assert any(item.kind == "execution" for item in request.output_payloads)

    rendering = project.configuration_document()
    rendering["rendering"] = []
    rendered = replace(project, configuration=payload(rendering))
    rendering_only = build.execute(
        tmp_path,
        snapshot,
        rendered,
        tmp_path / "rendering-only",
        execution=release_execution,
        cache=cache,
        baseline=None,
    )
    assert rendering_only.executed == ()
    assert set(rendering_only.reused) == set(result.executed)
    assert (
        captured[-1].resources["configuration.json"]
        != request.resources["configuration.json"]
    )

    baseline_digest = "c" * 64
    compared = replace(
        snapshot,
        digest="b" * 64,
        baseline_path=f"generations/{baseline_digest}",
        baseline_digest=baseline_digest,
    )
    baseline_only = build.execute(
        tmp_path,
        compared,
        project,
        tmp_path / "baseline-only",
        execution=release_execution,
        cache=cache,
        baseline={},
    )
    assert baseline_only.executed == ("summary",)
    assert {"extract", "checks", "model"} <= set(baseline_only.reused)
    assert captured[-1].metadata["inputs"] != request.metadata["inputs"]

    schemas = _schema_resources(request.extra_schemas)

    def financial(candidate: SnapshotInput) -> str:
        return _financial_digest(
            schemas,
            [],
            [(project.entity.value, "2026-01-31")],
            candidate.metadata,
            candidate.resources,
        )

    assert financial(captured[1]) == financial(request)
    assert financial(captured[2]) == financial(request)
    assert financial(captured[3]) == financial(request)

    def refuse(_tables: object) -> None:
        code = "invalid_cached_model"
        raise ContractError(code, "tables")

    monkeypatch.setattr(build, "validate_tables", refuse)
    with pytest.raises(ContractError, match="invalid_cached_model"):
        build.execute(
            tmp_path,
            snapshot,
            project,
            tmp_path / "rejected",
            execution=release_execution,
            cache=cache,
            baseline=None,
        )
    assert len(captured) == 4
    oversized = replace(
        snapshot,
        acquisitions=(
            replace(acquisitions[0], size=MAX_BYTES + 1),
            *acquisitions[1:],
        ),
    )
    monkeypatch.setattr(
        build,
        "read_object",
        lambda *_args: pytest.fail("source loaded before size guard"),
    )
    with pytest.raises(ContractError, match="source_size_limit"):
        build.execute(
            tmp_path,
            oversized,
            project,
            tmp_path / "oversized",
            execution=release_execution,
            cache=cache,
            baseline=None,
        )
    assert len(captured) == 4


def test_genuine_synthetic_ofx_build_is_pinned_and_complete(  # noqa: PLR0915 - compares three published candidates
    tmp_path: Path,
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
    release_execution: ObservedExecution,
) -> None:
    project_bytes = payload(project_document)
    project = read_project(project_bytes)
    directory = Path(__file__).resolve().parents[1] / "importers/fixtures_synthetic"
    source_bytes = {
        name: (directory / f"{name}.ofx").read_bytes() for name in ("prior", "main")
    }
    acquisitions = tuple(
        Acquisition(
            f"retry-{name}",
            selection.source_scope_id,
            "ofx",
            "2026-02-02T00:00:00Z",
            f"{name}.ofx",
            byte_digest(source_bytes[name]),
            len(source_bytes[name]),
            selection.accession_id,
        )
        for name, selection in zip(source_bytes, project.sources, strict=True)
    )
    by_digest = {byte_digest(content): content for content in source_bytes.values()}
    monkeypatch.setattr(
        build,
        "read_object",
        lambda _root, acquisition: by_digest[acquisition.object_digest],
    )
    selection = payload(
        {
            "schema_version": 1,
            "project_digest": byte_digest(project_bytes),
            "execution_digest": release_execution.digest,
            "acquisitions": [asdict(item) for item in acquisitions],
            "baseline_path": None,
            "baseline_digest": None,
        }
    )
    snapshot = InputSnapshot(
        byte_digest(selection),
        byte_digest(project_bytes),
        release_execution.digest,
        acquisitions,
        None,
        None,
    )
    retained = {snapshot.digest: selection}
    monkeypatch.setattr(build, "read_retained", lambda _root, digest: retained[digest])
    destination = tmp_path / "candidate"
    destination.mkdir()
    result = build.execute(
        tmp_path,
        snapshot,
        project,
        destination,
        execution=release_execution,
        cache=None,
        baseline=None,
    )
    pinned = read_snapshot(
        destination,
        result.snapshot.descriptor_digest,
    )
    assert len(pinned.tables["transactions"]) == 3
    assert len(pinned.tables["postings"]) == 6
    assert {"ledger.beancount", "summary.json", "derivations.json"} <= set(
        pinned.outputs
    )
    definitions = document(
        decode_document(pinned.outputs["derivations.json"], "derivations.json")[
            "definitions"
        ]
    )
    for entry in pinned.manifest["derivations"]:
        identifier = text(entry["derivation_id"], "derivation_id")
        assert entry["content_digest"] == byte_digest(payload(definitions[identifier]))
    assert {entry["path"] for entry in pinned.manifest["schemas"]} >= {
        "model_artifact_schema.json",
        "checks_artifact_schema.json",
        "summary_artifact_schema.json",
    }
    extract_artifacts = [
        item for item in pinned.manifest["artifacts"] if item["kind"] == "extract"
    ]
    assert len(extract_artifacts) == 2

    duplicate_document = {
        **project_document,
        "sources": [
            *project_document["sources"],
            {**project_document["sources"][-1], "accession_id": "repeat-main"},
        ],
    }
    duplicate_bytes = payload(duplicate_document)
    duplicate_acquisitions = (
        *acquisitions,
        replace(acquisitions[-1], retry_key="repeat-main", accession_id="repeat-main"),
    )
    duplicate_selection = payload(
        {
            "schema_version": 1,
            "project_digest": byte_digest(duplicate_bytes),
            "execution_digest": release_execution.digest,
            "acquisitions": [asdict(item) for item in duplicate_acquisitions],
            "baseline_path": None,
            "baseline_digest": None,
        }
    )
    duplicate_snapshot = InputSnapshot(
        byte_digest(duplicate_selection),
        byte_digest(duplicate_bytes),
        release_execution.digest,
        duplicate_acquisitions,
        None,
        None,
    )
    retained[duplicate_snapshot.digest] = duplicate_selection
    duplicate_destination = tmp_path / "duplicate"
    duplicate_destination.mkdir()
    repeated = build.execute(
        tmp_path,
        duplicate_snapshot,
        read_project(duplicate_bytes),
        duplicate_destination,
        execution=release_execution,
        cache=None,
        baseline=None,
    )
    repeated_pin = read_snapshot(
        duplicate_destination,
        repeated.snapshot.descriptor_digest,
    )
    repeated_artifacts = [
        item for item in repeated_pin.manifest["artifacts"] if item["kind"] == "extract"
    ]
    assert len(repeated_artifacts) == len(extract_artifacts)
    assert [item["byte_digest"] for item in repeated_artifacts] == [
        item["byte_digest"] for item in extract_artifacts
    ]
    assert repeated.executed.count("extract") == result.executed.count("extract") == 2
    assert repeated.snapshot.financial_digest == result.snapshot.financial_digest
    assert {
        name: rows for name, rows in repeated_pin.tables.items() if name != "build"
    } == {name: rows for name, rows in pinned.tables.items() if name != "build"}
    assert len(repeated_pin.manifest["recovery_set"]["accession_records"]) == 3
    assert len(pinned.manifest["recovery_set"]["accession_records"]) == 2
    assert repeated_pin.manifest["inputs"] != pinned.manifest["inputs"]

    assemble = build.assemble_snapshot

    def with_duplicate_artifact(
        location: Path, request: SnapshotInput, settings: WriterSettings
    ) -> SnapshotResult:
        original = next(
            output for output in request.output_payloads if output.kind == "extract"
        )
        duplicated = replace(original, path="duplicate-extract.json")
        return assemble(
            location,
            replace(
                request,
                output_payloads=(*request.output_payloads, duplicated),
            ),
            settings,
        )

    monkeypatch.setattr(build, "assemble_snapshot", with_duplicate_artifact)
    duplicated_destination = tmp_path / "extra-extract"
    duplicated_destination.mkdir()
    packaged = build.execute(
        tmp_path,
        duplicate_snapshot,
        read_project(duplicate_bytes),
        duplicated_destination,
        execution=release_execution,
        cache=None,
        baseline=None,
    )
    packaged_pin = read_snapshot(
        duplicated_destination,
        packaged.snapshot.descriptor_digest,
    )
    assert (
        sum(item["kind"] == "extract" for item in packaged_pin.manifest["artifacts"])
        == 3
    )
    assert packaged.snapshot.financial_digest == result.snapshot.financial_digest
