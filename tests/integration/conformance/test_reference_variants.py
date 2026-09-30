import json
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction

import pytest
from tests.integration.conformance.corpus import FixtureCase, load_case

from jbt.contracts.catalog import catalog
from jbt.contracts.declarations import validate_declaration
from jbt.contracts.validation import ContractError, validate_tables

pytestmark = [pytest.mark.integration, pytest.mark.golden]


def _number(row: dict, field: str) -> Fraction | None:
    coefficient = row[field + "_coefficient"]
    if coefficient is None:
        return None
    return Fraction(int(coefficient), 10 ** row[field + "_scale"])


def _rows(case: FixtureCase, table: str, key: str) -> dict:
    return {row[key]: row for row in case.tables[table]}


def _reference_state(
    case: FixtureCase,
) -> dict[tuple[str, str], tuple[Fraction, Fraction]]:
    sequence = {step["step_id"]: step["sequence"] for step in case.tables["book_steps"]}
    state: dict[tuple[str, str], tuple[Fraction, Fraction]] = {}
    for change in sorted(
        case.tables["reference_changes"],
        key=lambda row: (sequence[row["step_id"]], row["reference_change_id"]),
    ):
        position_id, lot_id = change["position_id"], change["lot_id"]
        assert isinstance(position_id, str)
        assert isinstance(lot_id, str)
        key = position_id, lot_id
        units = _number(change, "units")
        assert units is not None
        if change["change_kind"] == "entry":
            reference = _number(change, "reference_after")
            assert reference is not None
            assert key not in state
            state[key] = (units, reference)
            continue
        held, reference = state[key]
        assert _number(change, "reference_before") == reference
        if change["change_kind"] == "reset":
            assert units == held
            new_reference = _number(change, "reference_after")
            assert new_reference is not None
            state[key] = held, new_reference
        elif units == held:
            del state[key]
        else:
            state[key] = held - units, reference
    return state


def _transfer_reference_is_preserved(case: FixtureCase) -> bool:
    changes = _rows(case, "reference_changes", "reference_change_id")
    outgoing = changes[case.aliases["variant-transfer-out"]]
    incoming = changes[case.aliases["variant-transfer-in"]]
    return (
        _number(outgoing, "units") == _number(incoming, "units")
        and _number(outgoing, "reference_before")
        == _number(incoming, "reference_after")
        and outgoing["lot_id"] == incoming["lot_id"]
    )


def test_variant_is_a_complete_valid_hand_authored_specimen() -> None:
    case = load_case("reference-variants")
    validate_tables(case.tables)
    for declaration in case.tables["declarations"]:
        payload_json = declaration["payload_json"]
        assert isinstance(payload_json, str)
        validate_declaration(json.loads(payload_json))
    assert set(case.tables) == {table.name for table in catalog()}
    assert len(case.domain_records) == 12
    assert len(case.tables["reference_changes"]) == 14
    assert {mutation["id"] for mutation in case.negative_cases} >= {
        "partial-reset-without-branch",
        "transfer-reprices-destination",
        "collateral-only-reset",
        "eur-fee-converted-without-evidence",
    }


def test_final_variation_partial_reset_transfer_and_reversal_replay() -> None:
    case = load_case("reference-variants")
    changes = _rows(case, "reference_changes", "reference_change_id")
    assert _number(changes[case.aliases["variant-one-reset"]], "reference_after") == 101
    assert (
        _number(changes[case.aliases["variant-transfer-out"]], "reference_before")
        == 100
    )
    assert _transfer_reference_is_preserved(case)
    assert _number(changes[case.aliases["variant-short-entry"]], "units") == -2

    settlements = defaultdict(Fraction)
    for row in case.tables["reference_settlements"]:
        amount = _number(row, "amount")
        assert amount is not None
        settlements[row["reference_change_id"]] += amount
    for alias, hand_calculated in {
        "ref-daily": 30,
        "ref-close": 20,
        "variant-one-reset": 10,
        "variant-one-close": 40,
        "variant-three-A-close": 50,
        "variant-three-B-close": 100,
        "variant-short-close": 80,
    }.items():
        assert settlements[case.aliases[alias]] == hand_calculated
    assert sum(settlements.values()) == Fraction(
        case.expected_answers["settlement_total"]
    )
    assert sum(settlements.values()) - settlements[
        case.aliases["ref-daily"]
    ] - settlements[case.aliases["ref-close"]] == Fraction(
        case.expected_answers["additional_settlement_total"]
    )
    assert _reference_state(case) == {
        (case.aliases["collateral-future"], case.aliases["collateral-contract-lot"]): (
            Fraction(1),
            Fraction(case.expected_answers["collateral_only_contract_reference"]),
        )
    }

    inventory = defaultdict(Fraction)
    for row in case.tables["inventory_changes"]:
        if row["position_id"] in {case.aliases["future"], case.aliases["future-B"]}:
            units = _number(row, "units_delta")
            assert units is not None
            inventory[row["position_id"]] += units
    assert all(units == 0 for units in inventory.values())
    assert len(case.tables["disposals"]) == 5


def test_collateral_only_movement_neither_settles_nor_resets_reference() -> None:
    case = load_case("reference-variants")
    step = case.aliases["step:collateral-only-variation"]
    assert not any(row["step_id"] == step for row in case.tables["reference_changes"])
    postings = [row for row in case.tables["postings"] if row["step_id"] == step]
    amounts = {row["position_id"]: _number(row, "amount") for row in postings}
    assert amounts[case.aliases["margin"]] == 12
    assert amounts[case.aliases["cash-A"]] == -12
    collateral = sum(
        _number(row, "amount") or 0
        for row in case.tables["postings"]
        if row["position_id"] == case.aliases["margin"]
    )
    assert collateral == Fraction(case.expected_answers["collateral"])
    assert not any(
        row["settlement_posting_id"] in {posting["posting_id"] for posting in postings}
        for row in case.tables["reference_settlements"]
    )


def test_original_fee_attribution_keeps_source_currency_and_unknown_book_value() -> (
    None
):
    case = load_case("reference-variants")
    allocations = _rows(case, "fee_allocations", "fee_allocation_id")
    for currency, fee_aliases, source_alias, portions in (
        (
            "USD",
            ("variant-fee-usd", "variant-fee-usd-three"),
            "variant-usd-fee",
            (1, 3),
        ),
        (
            "EUR",
            ("variant-fee-eur", "variant-fee-eur-three"),
            "variant-eur-fee",
            (Fraction("0.5"), Fraction("1.5")),
        ),
    ):
        attributed = Fraction()
        for fee_alias, lot_alias, portion in zip(
            fee_aliases, ("variant-one", "variant-three"), portions, strict=True
        ):
            allocation = allocations[case.aliases[fee_alias]]
            assert allocation["fee_posting_id"] == case.aliases[source_alias]
            assert allocation["target_id"] == case.aliases[lot_alias]
            assert allocation["commodity_id"] == case.aliases[currency]
            assert allocation["treatment"] == "attribution_only"
            source_amount = _number(allocation, "amount")
            assert source_amount == -portion
            attributed -= portion
            assert _number(allocation, "book_amount") is None
            matching = [
                row
                for row in case.tables["inventory_fee_changes"]
                if row["fee_allocation_id"] == allocation["fee_allocation_id"]
            ]
            assert len(matching) == 1
            assert _number(matching[0], "amount_delta") == source_amount
            assert _number(matching[0], "book_amount_delta") is None
        fee_posting = _rows(case, "postings", "posting_id")[case.aliases[source_alias]]
        assert attributed == _number(fee_posting, "amount")
        assert -attributed == Fraction(case.expected_answers["original_fees"][currency])


def test_partial_reset_of_undivided_lot_is_rejected() -> None:
    case = load_case("reference-variants")
    tables = deepcopy(case.tables)
    row = _rows(case, "reference_changes", "reference_change_id")[
        case.aliases["variant-one-reset"]
    ]
    target = next(
        item
        for item in tables["reference_changes"]
        if item["reference_change_id"] == row["reference_change_id"]
    )
    target["units_coefficient"] = "5"
    target["units_scale"] = 1
    with pytest.raises(ContractError, match="partial_reset_requires_branch"):
        validate_tables(tables)


def test_collateral_only_terms_cannot_request_final_reference_reset() -> None:
    case = load_case("reference-variants")
    declaration = next(
        row
        for row in case.tables["declarations"]
        if row["declaration_key"] == "collateral-terms"
    )
    payload_json = declaration["payload_json"]
    assert isinstance(payload_json, str)
    payload = json.loads(payload_json)
    payload["settlement"]["reference_reset"] = "on_final_variation"
    with pytest.raises(ContractError, match="variation_reset"):
        validate_declaration(payload)


def test_eur_fee_must_not_gain_unevidenced_reference_book_amount() -> None:
    case = load_case("reference-variants")
    tables = deepcopy(case.tables)
    row = next(
        row
        for row in tables["inventory_fee_changes"]
        if row["fee_allocation_id"] == case.aliases["variant-fee-eur"]
    )
    row["book_amount_delta_coefficient"] = "1"
    row["book_amount_delta_scale"] = 0
    with pytest.raises(ContractError, match="reference_fee_book_amount"):
        validate_tables(tables)


def test_transfer_repricing_is_rejected_even_if_later_closes_agree() -> None:
    case = load_case("reference-variants")
    tables = deepcopy(case.tables)
    incoming = next(
        row
        for row in tables["reference_changes"]
        if row["reference_change_id"] == case.aliases["variant-transfer-in"]
    )
    incoming["reference_after_coefficient"] = "102"
    later_close = next(
        row
        for row in tables["reference_changes"]
        if row["reference_change_id"] == case.aliases["variant-three-B-close"]
    )
    later_close["reference_before_coefficient"] = "102"
    changed = FixtureCase(
        case.case_id,
        tables,
        case.expected_answers,
        case.evidence,
        case.oracle_notes,
        case.domain_records,
        case.aliases,
        case.negative_cases,
    )
    assert not _transfer_reference_is_preserved(changed)
    with pytest.raises(ContractError, match="reference_transfer_continuity"):
        validate_tables(tables)
