from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from tests.jbt.runtime.test_checks import transfer_tables

from jbt.artifacts.integrity import decode_document
from jbt.checks.quantity import CheckKind
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import checks_artifact_schema, validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.journal import JournalHead, select_journal
from jbt.runtime.build import (
    _check_policy,
    _effective_configuration,
    _findings,
    _model_configuration,
    _model_payload,
    _source_configuration,
    _source_payload,
)
from jbt.runtime.checks import projected_model_checks
from jbt.runtime.project import document, objects, read_project

if TYPE_CHECKING:
    from collections.abc import Callable

    from jbt.runtime.project import Project
    from jbt.runtime.resolution import ExtractInput


def test_production_model_payload_never_claims_checks_ran(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
) -> None:
    project, inputs = make_inputs(project_document)
    journal = select_journal((), head=JournalHead(0, None), retained_heads=())
    payload = document(
        decode_document(_model_payload(project, inputs, (), journal, ()), "model")
    )
    assert "checks" not in payload
    assert payload["determinations"]
    assert all(
        d["status"] == "resolved"
        for d in objects(payload["determinations"], "determinations")
    )
    source = document(decode_document(_source_payload(project, inputs), "checks"))
    checks = objects(source["checks"], "checks")
    assert all(c["check_kind"] == "source_reconciliation" for c in checks)
    assert all(c["status"] == "pass" for c in checks)


def test_each_effective_declaration_and_check_policy_is_in_cache_context(
    project_document: dict,
) -> None:
    value = project_document
    project = read_project(encode_json(value, integer_strings=False))
    changed = deepcopy(value)
    changed["declarations"][0]["valid_to"] = "2026-02-15"
    revised = read_project(encode_json(changed, integer_strings=False))
    assert _source_configuration(project) != _source_configuration(revised)
    assert _model_configuration(project) != _model_configuration(revised)
    changed = deepcopy(value)
    changed["configuration"]["outputs"][0]["required_checks"].append("classification")
    revised = read_project(encode_json(changed, integer_strings=False))
    assert _source_configuration(project) != _source_configuration(revised)
    assert CheckKind.CLASSIFICATION.value in _check_policy(revised)


def test_configured_check_requires_actual_pass_not_stage_execution(
    project_document: dict,
) -> None:
    project_document["configuration"]["outputs"] = [
        {"sink": "tabular", "required_checks": ["coverage"]}
    ]
    project = read_project(encode_json(project_document, integer_strings=False))
    with pytest.raises(ContractError, match="required_check_missing"):
        _effective_configuration(project, [])
    checks: list[dict[str, object]] = [
        {"check_id": kind, "check_kind": kind, "status": "not_evaluable"}
        for kind in _check_policy(project)
    ]
    with pytest.raises(ContractError, match="required_check_failed"):
        _effective_configuration(project, checks)
    checks[0]["status"] = "pass"
    _effective_configuration(project, checks)


def test_transfer_findings_preserve_posting_scope_not_source_evidence(
    project_document: dict,
) -> None:
    project = read_project(encode_json(project_document, integer_strings=False))
    check = projected_model_checks(transfer_tables(), frozenset())[0]
    (result,) = _findings(project, (check,))
    assert result["evidence"] == []
    assert result["scope"] == [
        {"table": "postings", "key": [key.value]} for key in check.evidence
    ]
    validate_document(
        {"schema_version": 1, "checks": [result]}, checks_artifact_schema()
    )


@pytest.mark.parametrize("status", ["fail", "not_evaluable"])
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (CheckKind.TRANSFER, "transfer_conservation_failed"),
        (CheckKind.LIFECYCLE, "account_lifecycle_failed"),
    ],
)
def test_financial_invariants_cannot_be_waived_by_output_policy(
    project_document: dict,
    status: str,
    kind: CheckKind,
    code: str,
) -> None:
    project_document["configuration"]["outputs"] = [
        {"sink": "tabular", "required_checks": []}
    ]
    project = read_project(encode_json(project_document, integer_strings=False))
    with pytest.raises(ContractError, match=code):
        _effective_configuration(
            project,
            [
                {
                    "check_id": "transfer",
                    "check_kind": kind.value,
                    "status": status,
                }
            ],
        )
