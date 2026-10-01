from copy import deepcopy
from fractions import Fraction

import pytest
from tests.integration.conformance.corpus import load_case
from tests.integration.conformance.reader import replay_rows, validate_rows

from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import ContractError
from jbt.contracts.validation import validate_tables

pytestmark = [pytest.mark.integration, pytest.mark.golden]


def _amount(row: dict[str, object], field: str) -> Fraction:
    coefficient, scale = row[field + "_coefficient"], row[field + "_scale"]
    assert isinstance(coefficient, str)
    assert type(scale) is int
    return Fraction(int(coefficient), 10**scale)


def test_one_disposal_leg_retains_three_explicit_origin_slices() -> None:
    case = load_case("three-lot-disposal")
    validate_tables(case.tables)
    validate_rows(case.tables, tabular_schema())
    entity = case.tables["build"][0]["entity_id"]
    assert isinstance(entity, str)
    replay = replay_rows(case.tables, entity=entity)
    slices = [
        row
        for row in case.tables["inventory_changes"]
        if row["posting_id"] == case.aliases["sale-q"]
    ]
    assert {row["lot_id"] for row in slices} == {
        case.aliases["L1"],
        case.aliases["L2"],
        case.aliases["L3"],
    }
    assert len({row["allocation_id"] for row in slices}) == 3
    for alias, expected in case.expected_answers["inventory"].items():
        remaining = [
            state
            for key, state in replay.inventory.items()
            if key[1] == case.aliases[alias]
        ]
        assert sum(state.units for state in remaining) == Fraction(expected["units"])
        assert all(state.principal is not None for state in remaining)
        assert sum(
            state.principal for state in remaining if state.principal is not None
        ) == Fraction(expected["principal"])
    assert len(case.tables["disposals"]) == 3
    released = -sum(_amount(row, "principal_delta") for row in slices)
    proceeds = sum(_amount(row, "proceeds_total") for row in case.tables["disposals"])
    assert released == Fraction(case.expected_answers["released_principal"])
    assert proceeds == Fraction(case.expected_answers["proceeds"])
    assert proceeds - released == Fraction(case.expected_answers["gain"])


def test_unspecified_origin_is_rejected_instead_of_selected_by_the_reader() -> None:
    case = load_case("three-lot-disposal")
    changed = deepcopy(case.tables)
    third = next(
        row
        for row in changed["inventory_changes"]
        if row["change_id"] == case.aliases["third-source"]
    )
    third["lot_id"] = None
    with pytest.raises(ContractError, match="inventory_slice"):
        validate_tables(changed)
