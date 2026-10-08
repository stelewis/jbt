from collections.abc import Callable
from dataclasses import replace

from jbt.artifacts.integrity import decode_document
from jbt.domain.cash import CashCheck, CheckStatus
from jbt.domain.numbers import ExactDecimal
from jbt.runtime.outputs import check_document, ledger, summary
from jbt.runtime.project import Project, document, objects
from jbt.runtime.record import ExtractInput, make_record

type Inputs = Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]]


def test_findings_preserve_unknown_separately_from_zero() -> None:
    check = CashCheck(
        "source",
        "source_reconciliation",
        CheckStatus.NOT_EVALUABLE,
        ExactDecimal.parse("115.00"),
        None,
        ("evidence",),
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
    record = make_record(project, extracts)
    rendered = document(
        decode_document(
            summary(
                project, record.tables, checks=[], baseline=None, baseline_digest=None
            ),
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
    output = ledger(project, record.tables).decode()
    assert "2026-02-01 balance Assets:Bank 115 ~ 0 USD" in output
    assert "2026-01-01 balance Assets:Bank 100 ~ 0 USD" in output


def test_summary_comparison_is_explicit_and_deterministic(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, extracts = make_inputs(project_document)
    record = make_record(project, extracts)
    first = summary(
        project,
        record.tables,
        checks=[],
        baseline=record.tables,
        baseline_digest="a" * 64,
    )
    assert first == summary(
        project,
        record.tables,
        checks=[],
        baseline=record.tables,
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
    record = make_record(project, extracts)
    result = document(
        decode_document(
            summary(
                project,
                record.tables,
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
