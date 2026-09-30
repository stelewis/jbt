import json
from copy import deepcopy
from fractions import Fraction

import pytest
from tests.integration.conformance.corpus import load_case
from tests.integration.conformance.reader import ReaderError, replay_rows, validate_rows

from jbt.contracts.catalog import schema_document
from jbt.contracts.primitives import ContractError
from jbt.contracts.validation import validate_tables

pytestmark = [pytest.mark.integration, pytest.mark.golden]


def _amount(row: dict[str, object], field: str) -> Fraction:
    coefficient = row[field + "_coefficient"]
    scale = row[field + "_scale"]
    assert isinstance(coefficient, str)
    assert type(scale) is int
    return Fraction(int(coefficient), 10**scale)


def test_corporate_history_replays_exact_components_without_applying_ratios() -> None:
    case = load_case("corporate-variants")
    validate_tables(case.tables)
    validate_rows(case.tables, schema_document())
    entity = case.tables["build"][0]["entity_id"]
    assert isinstance(entity, str)
    replay = replay_rows(case.tables, entity=entity)
    for alias, expected in case.expected_answers["inventory"].items():
        slices = [
            state
            for key, state in replay.inventory.items()
            if key[1] == case.aliases[alias]
        ]
        assert sum(state.units for state in slices) == Fraction(expected["units"])
        assert all(state.principal is not None for state in slices)
        assert sum(
            state.principal for state in slices if state.principal is not None
        ) == Fraction(expected["principal"])
        assert all(state.capitalized_fee == state.expensed_fee == 0 for state in slices)
    for alias, expected in case.expected_answers["positions"].items():
        assert sum(
            _amount(row, "amount")
            for row in case.tables["postings"]
            if row["position_id"] == case.aliases[alias]
        ) == Fraction(expected)


def test_each_spinoff_output_has_its_own_basis_application() -> None:
    case = load_case("corporate-variants")
    applications = {row["effect_id"]: row for row in case.tables["action_applications"]}
    changes = {row["change_id"]: row for row in case.tables["inventory_changes"]}
    for effect_alias, change_alias, principal in [
        ("spinoff-parent-basis", "spinoff-parent", 60),
        ("spinoff-child-basis", "spinoff-child", 20),
        ("capital-basis-effect", "capital-target", 40),
    ]:
        application = applications[case.aliases[effect_alias]]
        assert application["target_kind"] == "inventory_change"
        assert application["target_id"] == case.aliases[change_alias]
        assert (
            _amount(changes[application["target_id"]], "principal_delta") == principal
        )


def test_later_actions_do_not_rewrite_a_completed_disposal_or_acquisition() -> None:
    case = load_case("corporate-variants")
    assert len(case.tables["disposals"]) == 1
    disposal = case.tables["disposals"][0]
    assert _amount(disposal, "proceeds_total") == 22
    (sale,) = [
        row
        for row in case.tables["inventory_changes"]
        if row["allocation_id"] == disposal["allocation_id"]
    ]
    assert _amount(sale, "units_delta") == -2
    assert _amount(sale, "principal_delta") == -20
    assert _amount(disposal, "proceeds_total") + _amount(sale, "principal_delta") == (
        Fraction(case.expected_answers["pre_action_gain"])
    )
    lots = {row["lot_id"]: row for row in case.tables["lots"]}
    assert lots[case.aliases["child"]]["parent_lot_id"] == case.aliases["root"]
    assert (
        lots[case.aliases["child"]]["acquisition_date"]
        == (case.expected_answers["original_acquisition_date"])
    )
    assert lots[case.aliases["reinvested"]]["parent_lot_id"] is None
    assert lots[case.aliases["reinvested"]]["acquisition_date"] == "2026-03-11"


def test_redenomination_is_not_a_display_alias() -> None:
    case = load_case("corporate-variants")
    assert case.aliases["OLD"] != case.aliases["NEW"]
    symbols = case.tables["commodity_symbols"]
    assert {row["symbol"] for row in symbols} == {"Q", "Q-NEW", "Q-\u00e9"}
    assert {row["commodity_id"] for row in symbols} == {case.aliases["Q"]}
    assert not [
        row
        for row in case.tables["postings"]
        if row["txn_id"] == case.aliases["rename"]
    ]
    effects = {row["effect_id"]: row for row in case.tables["corporate_action_effects"]}
    conversion = effects[case.aliases["redenomination-units"]]
    assert conversion["input_commodity_id"] == case.aliases["OLD"]
    assert conversion["resulting_commodity_id"] == case.aliases["NEW"]
    assert _amount(conversion, "ratio_numerator") == 1
    assert _amount(conversion, "ratio_denominator") == 2


@pytest.mark.parametrize(
    "scenario",
    [
        "global",
        "scoped_against_global",
        "distinct_scopes",
        "distinct_exchanges",
        "adjacent",
    ],
)
def test_symbol_lookup_cannot_resolve_to_two_instruments(scenario: str) -> None:
    case = load_case("corporate-variants")
    tables = deepcopy(case.tables)
    original = next(
        row for row in tables["commodity_symbols"] if row["symbol"] == "Q-\u00e9"
    )
    if scenario == "distinct_scopes":
        original["source_scope_id"] = "custodian-a"
    if scenario == "distinct_exchanges":
        original["exchange"] = "X"
    if scenario == "adjacent":
        original["valid_to"] = "2026-06-01"
    conflicting = deepcopy(original)
    conflicting.update(
        symbol_id="conflicting-symbol",
        commodity_id=case.aliases["NEW"],
        source_scope_id=(
            "custodian-b"
            if scenario == "distinct_scopes"
            else "custodian-a"
            if scenario == "scoped_against_global"
            else original["source_scope_id"]
        ),
        exchange="Y" if scenario == "distinct_exchanges" else original["exchange"],
        valid_from="2026-06-01" if scenario == "adjacent" else "2026-05-01",
        valid_to=None,
    )
    tables["commodity_symbols"].append(conflicting)
    tables["commodity_symbols"].sort(
        key=lambda row: (
            row["entity_id"],
            row["commodity_id"],
            row["valid_from"],
            row["symbol_id"],
        )
    )
    if scenario in {"global", "scoped_against_global"}:
        with pytest.raises(ContractError, match="ambiguous_commodity_symbol"):
            validate_tables(tables)
        with pytest.raises(ReaderError, match="ambiguous commodity symbol"):
            validate_rows(tables, schema_document())
    else:
        validate_tables(tables)
        validate_rows(tables, schema_document())


@pytest.mark.parametrize(
    ("target_kind", "constraint"),
    [
        ("inventory_allocation", "action_application_target"),
        ("inventory_change", "basis_application_amount"),
    ],
)
def test_basis_cannot_target_the_whole_allocation_or_the_wrong_output(
    target_kind: str,
    constraint: str,
) -> None:
    case = load_case("corporate-variants")
    changed = deepcopy(case.tables)
    application = next(
        row
        for row in changed["action_applications"]
        if row["effect_id"] == case.aliases["spinoff-child-basis"]
    )
    application["target_kind"] = target_kind
    application["target_id"] = case.aliases[
        "spinoff-allocation"
        if target_kind == "inventory_allocation"
        else "spinoff-parent"
    ]
    for evidence in changed["provenance"]:
        if evidence["record_kind"] == "action_applications":
            encoded = evidence["record_id"]
            assert isinstance(encoded, str)
            key = json.loads(encoded)
            if case.aliases["spinoff-child-basis"] in key:
                evidence["record_id"] = json.dumps(
                    [
                        application["target_id"]
                        if value == case.aliases["spinoff-child"]
                        else target_kind
                        if value == "inventory_change"
                        else value
                        for value in key
                    ],
                    separators=(",", ":"),
                )
    with pytest.raises(ContractError, match=constraint):
        validate_tables(changed)
