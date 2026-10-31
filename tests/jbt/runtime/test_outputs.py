from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace

import pytest
from tests.jbt.runtime.test_checks import transfer_tables

from jbt.artifacts.integrity import decode_document
from jbt.checks.quantity import Check, CheckKind, CheckStatus
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import (
    summary_artifact_schema,
    validate_document,
)
from jbt.domain.canonical import encode_json
from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.model.quantity import resolve
from jbt.runtime.checks import projected_account_checks
from jbt.runtime.outputs import check_document, ledger, summary
from jbt.runtime.project import Project, document, objects, read_project
from jbt.runtime.resolution import ExtractInput, prepare, project_resolution

type Inputs = Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]]


def test_dated_account_and_position_revisions_preserve_lifetime_and_coverage(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project_document["configuration"]["as_of"] = "2026-02-10"
    for kind in ("account", "position"):
        first = next(
            d for d in project_document["declarations"] if d["payload"]["kind"] == kind
        )
        later = deepcopy(first)
        first["valid_to"] = later["valid_from"] = "2026-01-15"
        if kind == "account":
            later["payload"]["closed_date"] = "2026-01-31"
            later["valid_to"] = "2026-02-01"
        project_document["declarations"].append(later)
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    tables = project_resolution(prepared, resolve(prepared.inputs))
    for claim in tables["statement_coverage"]:
        claim["is_complete"] = True
    checks = projected_account_checks(project, tables)
    assert all(check.status == CheckStatus.PASS for check in checks)
    assert len([check for check in checks if check.kind == CheckKind.COVERAGE]) == 2
    rendered = ledger(project, tables).decode()
    assert rendered.count("open Assets:Bank") == 1
    assert "2026-02-01 close Assets:Bank" in rendered
    assert "2026-01-15 close Assets:Bank" not in rendered


@pytest.mark.parametrize("complete", [False, True])
def test_summary_exposes_transit_across_month_end_without_external_flows(
    project_document: dict, *, complete: bool
) -> None:
    project_document["configuration"]["as_of"] = "2026-02-28"
    project = read_project(encode_json(project_document, integer_strings=False))
    tables = transfer_tables()
    tables["accounts"] = [
        {
            "account_id": key,
            "coverage_start_date": "2026-01-01",
            "opened_date": "2026-01-01",
        }
        for key in ("checking", "card", "transit")
    ]
    for transaction in tables["transactions"]:
        transaction.update(event_kind="transfer", review_state="reviewed")
    for posting in tables["postings"]:
        posting["account_id"] = posting["position_id"]
        posting["date_posted_mode"] = "inherit"
    if not complete:
        tables["links"] = []
        tables["postings"] = [
            p for p in tables["postings"] if p["txn_id"] == "departure"
        ]
    result = document(
        decode_document(
            summary(project, tables, checks=[], baseline=None, baseline_digest=None),
            "summary",
        )
    )
    validate_document(result, summary_artifact_schema())
    transit = objects(result["transit_balances"], "transit")
    assert [row["month"] for row in transit] == ["2026-01", "2026-02"]
    assert [document(row["closing"])["coefficient"] for row in transit] == [
        "10",
        "0" if complete else "10",
    ]
    assert result["category_flows"] == []
    assert result["uncategorized_work"] == []
    assert document(result["review"])["uncategorized_events"] == 0


def test_findings_preserve_unknown_separately_from_zero() -> None:
    check = Check(
        SemanticKey("source"),
        CheckKind.SOURCE,
        CheckStatus.NOT_EVALUABLE,
        ExactDecimal.parse("115.00"),
        None,
        SemanticKey("USD"),
        SemanticKey("bank"),
        (SemanticKey("evidence"),),
    )
    result = check_document(check, entity_id="owner", commodity_id="USD")
    assert result["observed"] == {
        "kind": "unavailable",
        "reason": "insufficient_evidence",
    }
    assert result["status"] == "not_evaluable"
    assert check_document(
        replace(check, observed=0), entity_id="owner", commodity_id="USD"
    )["observed"] == {"kind": "integer", "value": 0}


def test_summary_and_real_ledger_show_independent_cash_equation(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    tables = project_resolution(prepared, resolve(prepared.inputs))
    rendered = document(
        decode_document(
            summary(project, tables, checks=[], baseline=None, baseline_digest=None),
            "summary",
        )
    )
    month = next(
        row
        for row in objects(rendered["monthly_quantities"], "months")
        if row["month"] == "2026-01"
    )
    assert document(month["opening"])["coefficient"] == "100"
    assert document(month["credits"])["coefficient"] == "20"
    assert document(month["debits"])["coefficient"] == "-5"
    assert document(month["closing"])["coefficient"] == "115"
    output = ledger(project, tables).decode()
    assert "2026-02-01 balance Assets:Bank 115 ~ 0 USD" in output
    assert "2026-01-01 balance Assets:Bank 100 ~ 0 USD" in output


def test_summary_comparison_is_explicit_and_deterministic(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    tables = project_resolution(prepared, resolve(prepared.inputs))
    first = summary(
        project,
        tables,
        checks=[],
        baseline=tables,
        baseline_digest="a" * 64,
    )
    assert first == summary(
        project,
        tables,
        checks=[],
        baseline=tables,
        baseline_digest="a" * 64,
    )
    comparison = document(decode_document(first, "summary"))
    change = objects(comparison["changes"], "changes")[0]
    assert document(change["change"])["coefficient"] == "0"
    assert document(change["before"])["coefficient"] == "115"


def test_summary_carries_quantity_through_a_month_without_movements(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project_document["configuration"]["as_of"] = "2026-02-28"
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    tables = project_resolution(prepared, resolve(prepared.inputs))
    result = document(
        decode_document(
            summary(
                project,
                tables,
                checks=[],
                baseline=None,
                baseline_digest=None,
            ),
            "summary",
        )
    )
    months = objects(result["monthly_quantities"], "months")
    assert [month["month"] for month in months] == ["2026-01", "2026-02"]
    assert [
        document(months[1][key])["coefficient"]
        for key in ("opening", "credits", "debits", "closing")
    ] == [
        "115",
        "0",
        "0",
        "115",
    ]


def test_summary_does_not_inherit_explicitly_unknown_posting_date(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    tables = project_resolution(prepared, resolve(prepared.inputs))
    boundary = next(row for row in tables["postings"] if row["leg_kind"] == "boundary")
    boundary["date_posted_mode"] = "unknown"
    boundary["date_posted"] = None
    with pytest.raises(
        ContractError,
        match=f"summary_posting_date_required: {boundary['posting_id']}",
    ):
        summary(project, tables, checks=[], baseline=None, baseline_digest=None)
