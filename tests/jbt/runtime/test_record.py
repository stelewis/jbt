from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace

import pytest

from jbt.contracts.primitives import ContractError
from jbt.contracts.validation import validate_tables
from jbt.domain.cash import CheckStatus, source_checks
from jbt.runtime.project import Project
from jbt.runtime.record import ExtractInput, make_record

type Inputs = Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]]


def test_real_processor_determines_shared_cash_rows(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, sources = make_inputs(project_document)
    result = make_record(project, sources)
    assert all(check.status == CheckStatus.PASS for check in result.checks[:-1])
    assert result.checks[-1].status == CheckStatus.NOT_EVALUABLE
    assert len(result.tables["transactions"]) == 3
    assert len(result.tables["postings"]) == 6
    assert result.tables["inventory_allocations"] == []
    assert [e.amount.coefficient for e in result.events] == ["100", "20", "-5"]
    assert len(result.tables["balance_assertions"]) == 2
    assert result.tables["declarations"]
    assert result.tables["provenance"]
    assert {row["assertion_kind"] for row in result.tables["balance_assertions"]} == {
        "point"
    }
    assert {row["timestamp_local"] for row in result.tables["balance_assertions"]} == {
        "00:00:00"
    }
    assert {row["field_name"] for row in result.tables["observations"]} == {
        "source.accttype",
        "source.bankid",
        "source.trntype",
        "source.memo",
        "source.dtstart",
        "source.dtend",
    }
    validate_tables(
        {
            **result.tables,
            "build": [
                {
                    "entity_id": project.entity.value,
                    "manifest_digest": "0" * 64,
                    "producer_version": "test",
                    "schema_version": 1,
                    "schema_digest": "0" * 64,
                    "as_of": "2026-01-31",
                    "execution_fingerprint": "0" * 64,
                }
            ],
        }
    )


def test_account_binding_does_not_guess_from_file(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project_document["sources"][0]["source_account_identifier"] = "another"
    project, sources = make_inputs(project_document)
    with pytest.raises(ContractError, match="source_account_binding"):
        make_record(project, sources)


def test_wrong_authored_opening_preserves_source_pass(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    changed = deepcopy(project_document)
    record = next(
        d["payload"]["record"]
        for d in changed["declarations"]
        if d["payload"]["kind"] == "authored_fact"
    )
    record["legs"][0]["amount"]["coefficient"] = "101"
    record["legs"][1]["amount"]["coefficient"] = "-101"
    project, sources = make_inputs(changed)
    result = make_record(project, sources)
    assert (
        source_checks(result.opening, result.statements)[0].status == CheckStatus.PASS
    )
    assert result.checks[0].status == CheckStatus.FAIL
    assert result.checks[1].status == CheckStatus.FAIL


def test_repeated_acquisition_preserves_the_same_financial_record(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, sources = make_inputs(project_document)
    expected = make_record(project, sources)
    repeated = replace(
        sources[-1],
        selection=replace(sources[-1].selection, accession_id="a" * 64),
    )
    actual = make_record(
        replace(project, sources=(*project.sources, repeated.selection)),
        (*sources, repeated),
    )
    assert actual == expected


@pytest.mark.parametrize(
    "binding",
    [
        {"source_scope_id": "another-scope"},
        {"source_account_identifier": "another-account"},
    ],
)
def test_repeated_payload_cannot_hide_a_conflicting_binding(
    project_document: dict,
    make_inputs: Inputs,
    binding: dict[str, str],
) -> None:
    project, sources = make_inputs(project_document)
    repeated = replace(
        sources[-1],
        selection=replace(sources[-1].selection, accession_id="a" * 64, **binding),
    )
    with pytest.raises(ContractError, match="source_binding_conflict"):
        make_record(
            replace(project, sources=(*project.sources, repeated.selection)),
            (*sources, repeated),
        )


def test_balance_provenance_separates_stated_values_and_declared_bindings(
    project_document: dict,
    make_inputs: Inputs,
) -> None:
    project, sources = make_inputs(project_document)
    result = make_record(project, sources)
    provenance = result.tables["provenance"]
    for table, field, origin in (
        ("balances", "amount", "extracted"),
        ("balances", "position_id", "declaration"),
        ("balances", "commodity_id", "declaration"),
        ("balance_assertions", "scope_id", "declaration"),
        ("balance_assertions", "timestamp_local", "extracted"),
    ):
        matches = [
            row
            for row in provenance
            if row["record_kind"] == table and row["field_name"] == field
        ]
        assert len(matches) == 2
        assert {row["value_origin"] for row in matches} == {origin}
        assert all(row["evidence_id"] for row in matches)
