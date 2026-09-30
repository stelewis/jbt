import json
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from tests.integration.step_0.breadth import FAMILIES, family_cases
from tests.integration.step_0.corpus import load_case as load_specimen
from tests.integration.step_0.reader import ReaderError, replay_rows, validate_rows

from jbt.contracts.catalog import schema_document
from jbt.contracts.declarations import validate_declaration
from jbt.contracts.validation import ContractError, validate_tables

if TYPE_CHECKING:
    from tests.integration.step_0.reader import Row


class FamilyCase(Protocol):
    tables: dict[str, list[Row]]
    evidence: dict[str, Any]
    aliases: dict[str, str]
    expected_answers: dict[str, Any]


pytestmark = [pytest.mark.integration, pytest.mark.golden]


def load_case(name: str) -> FamilyCase:
    return cast("FamilyCase", load_specimen(name))


def _number(row: Row, field: str) -> Fraction | None:
    coefficient = row[field + "_coefficient"]
    return (
        None
        if coefficient is None
        else Fraction(int(coefficient), 10 ** row[field + "_scale"])
    )


def _amount(row: Row, field: str) -> Fraction:
    number = _number(row, field)
    assert number is not None
    return number


def test_family_selection_uses_the_shared_corpus_loader() -> None:
    cases = family_cases()
    assert {case.case_id for case in cases} == {
        "breadth/" + family for family in FAMILIES
    }
    assert all(case.domain_records and case.oracle_notes for case in cases)


@pytest.mark.parametrize(
    ("name", "total"),
    [("split-then-buy", 25), ("buy-then-split", 30)],
)
def test_same_day_evidenced_order_changes_lot_quantities(name: str, total: int) -> None:
    case = load_case("breadth/" + name)
    validate_tables(case.tables)
    validate_rows(case.tables, schema_document())
    entity = case.tables["build"][0]["entity_id"]
    replay = replay_rows(case.tables, entity=entity)
    slices = [
        state for key, state in replay.inventory.items() if key[1] == case.aliases["AQ"]
    ]
    assert (
        sum(state.units for state in slices)
        == Fraction(case.expected_answers["same_day_units"])
        == total
    )
    assert all(state.principal is not None for state in slices)
    assert (
        sum(state.principal for state in slices if state.principal is not None)
        == Fraction(case.expected_answers["same_day_principal"])
        == 150
    )
    steps = case.tables["book_steps"]
    assert [step["sequence"] for step in steps] == [0, 1, 2]
    assert [step["effective_date"] for step in steps[1:]] == [
        "2026-02-05",
        "2026-02-05",
    ]
    edge = case.tables["book_step_dependencies"][0]
    assert edge["before_step_id"] == steps[1]["step_id"]
    assert edge["after_step_id"] == steps[2]["step_id"]
    sources = {
        row["txn_id"]: row["description_as_stated"]
        for row in case.tables["transactions"]
    }
    assert "sequence 1" in sources[steps[1]["txn_id"]]
    assert "sequence 2" in sources[steps[2]["txn_id"]]


def test_missing_or_backward_same_day_precedence_is_not_inferred() -> None:
    case = load_case("breadth/split-then-buy")
    entity = case.tables["build"][0]["entity_id"]
    without_edge = deepcopy(case.tables)
    without_edge["book_step_dependencies"] = []
    with pytest.raises(ReaderError, match="missing decisive same-day order"):
        validate_rows(without_edge, schema_document())
    with pytest.raises(ReaderError, match="missing decisive same-day order"):
        replay_rows(without_edge, entity=entity)
    reversed_edge = deepcopy(case.tables)
    edge = reversed_edge["book_step_dependencies"][0]
    edge["before_step_id"], edge["after_step_id"] = (
        edge["after_step_id"],
        edge["before_step_id"],
    )
    with pytest.raises(ContractError, match="precedence_forward"):
        validate_tables(reversed_edge)
    with pytest.raises(ReaderError, match="backward"):
        replay_rows(reversed_edge, entity=entity)
    cycle = deepcopy(case.tables)
    cycle["book_step_dependencies"].append(edge)
    cycle["book_step_dependencies"].sort(
        key=lambda row: (
            row["entity_id"],
            row["before_step_id"],
            row["after_step_id"],
            row["constraint_kind"],
        )
    )
    with pytest.raises(ContractError, match="precedence_forward"):
        validate_tables(cycle)
    with pytest.raises(ReaderError, match="backward"):
        replay_rows(cycle, entity=entity)


@pytest.mark.parametrize("family", FAMILIES)
def test_complete_family_replays_independent_hand_oracles(family: str) -> None:
    case = load_case("breadth/" + family)
    validate_tables(case.tables)
    for declaration in case.tables["declarations"]:
        validate_declaration(json.loads(declaration["payload_json"]))
    for source in case.tables["source_records"]:
        data = case.evidence["blobs"][source["source_blob_digest"]]
        assert data.startswith("INVENTED")
        assert sha256(data.encode()).hexdigest() == source["source_blob_digest"]
    entity = case.tables["build"][0]["entity_id"]
    replay = replay_rows(case.tables, entity=entity)
    states = {row["txn_id"]: row["event_state"] for row in case.tables["transactions"]}
    for alias, expected in case.expected_answers["quantities"].items():
        amounts = [
            _amount(row, "amount")
            for row in case.tables["postings"]
            if row["position_id"] == case.aliases[alias]
            and states[row["txn_id"]] == "recognized"
        ]
        assert sum(amounts) == Fraction(expected), alias
    for alias, expected in case.expected_answers.get("inventory", {}).items():
        slices = [
            state
            for key, state in replay.inventory.items()
            if key[1] == case.aliases[alias] and state.units
        ]
        assert slices
        for field, value in expected.items():
            actual = [getattr(state, field) for state in slices]
            if value is None:
                assert all(component is None for component in actual)
            else:
                assert all(component is not None for component in actual)
                assert sum(cast("list[Fraction]", actual)) == Fraction(value), (
                    alias,
                    field,
                )
    for alias, expected in case.expected_answers.get("category_amounts", {}).items():
        assert sum(
            _amount(row, "amount")
            for row in case.tables["postings"]
            if row["category_id"] == case.aliases[alias]
        ) == Fraction(expected)


def test_options_lifecycle_retains_premium_and_adjusted_delivery() -> None:
    case = load_case("breadth/options")
    postings = {row["posting_id"]: row for row in case.tables["postings"]}
    expiry = [
        row for row in postings.values() if row["txn_id"] == case.aliases["expiry"]
    ]
    assert not any(row["posting_role"] == "cash" for row in expiry)
    assert _number(case.tables["disposals"][0], "proceeds_total") == 0
    changes = {row["change_id"]: row for row in case.tables["inventory_changes"]}
    assert _number(changes[case.aliases["call-entry"]], "principal_delta") == 2
    assert _number(changes[case.aliases["put-entry"]], "principal_delta") == -3
    assert _number(changes[case.aliases["call-stock-entry"]], "principal_delta") == 51
    assert _number(changes[case.aliases["put-stock-entry"]], "principal_delta") == 47
    assert _number(postings[case.aliases["call-strike"]], "amount") == -50
    assert _number(postings[case.aliases["call-adjustment"]], "amount") == 1
    branches = {row["lot_id"]: row["parent_lot_id"] for row in case.tables["lots"]}
    assert branches[case.aliases["CQL"]] == case.aliases["CL"]
    assert branches[case.aliases["PQL"]] == case.aliases["PL"]


def test_claims_keep_legal_relationships_and_do_not_invent_barrier_path() -> None:
    case = load_case("breadth/claims")
    positions = {row["position_id"]: row for row in case.tables["positions"]}
    assert positions[case.aliases["etf-property"]]["position_kind"] == "property"
    assert positions[case.aliases["etn-claim"]]["position_kind"] == "claim"
    assert (
        positions[case.aliases["retirement"]]["counterparty_id"]
        != positions[case.aliases["exchange-failure"]]["counterparty_id"]
    )
    observations = {row["observation_id"]: row for row in case.tables["observations"]}
    assert (
        observations[case.aliases["issuer-trigger"]]["text_value"] == "issuer_confirmed"
    )
    assert (
        observations[case.aliases["missing-path-observation"]]["text_value"]
        == "unavailable"
    )
    assert not any(
        row["txn_id"] == case.aliases["missing-path"]
        for row in case.tables["book_steps"]
    )
    coupon = next(
        row
        for row in case.tables["postings"]
        if row["posting_id"] == case.aliases["coupon-cash"]
    )
    assert _number(coupon, "amount") == 5


def test_obligations_keep_cover_cost_margin_and_refinancing_separate() -> None:
    case = load_case("breadth/obligations")
    disposals = {row["disposal_id"]: row for row in case.tables["disposals"]}
    assert _number(disposals[case.aliases["cover-disposal"]], "proceeds_total") == -26
    assert _number(disposals[case.aliases["margin-disposal"]], "proceeds_total") == 8
    positions = {row["position_id"]: row for row in case.tables["positions"]}
    assert (
        positions[case.aliases["debt"]]["counterparty_id"]
        != positions[case.aliases["replacement-debt"]]["counterparty_id"]
    )
    assert not any(
        row["txn_id"] == case.aliases["unsplit-payment"]
        for row in case.tables["book_steps"]
    )
    assert any(row["posting_role"] == "borrow_fee" for row in case.tables["postings"])


def test_inverse_perpetual_replays_settlements_not_notional_or_funding() -> None:
    case = load_case("breadth/perpetual")
    settlements = [
        value
        for row in case.tables["reference_settlements"]
        if (value := _number(row, "amount")) is not None
    ]
    assert sorted(settlements) == [Fraction("-0.3"), Fraction("0.5")]
    assert sum(settlements) == Fraction("0.2")
    assert 100 * (Fraction(1, 100) - Fraction(1, 200)) == Fraction("0.5")
    assert 100 * (Fraction(1, 200) - Fraction(1, 125)) == Fraction("-0.3")
    references = case.tables["reference_changes"]
    assert len(references) == 3
    assert not any(
        row["step_id"]
        in {case.aliases["step:funding-paid"], case.aliases["step:funding-received"]}
        for row in references
    )
    terms = next(
        json.loads(row["payload_json"])
        for row in case.tables["declarations"]
        if row["declaration_key"] == "inverse"
    )
    assert terms["expiry"] is None
    assert terms["collateral"]["position_ids"] == sorted(
        [case.aliases["btc-margin"], case.aliases["margin"]]
    )


def test_onchain_mechanics_preserve_tokens_fees_and_unknown_basis() -> None:
    case = load_case("breadth/onchain")
    symbols = case.tables["commodity_symbols"]
    assert {row["symbol"] for row in symbols} == {"USDC"}
    assert len({row["commodity_id"] for row in symbols}) == 2
    token_keys = {
        row["text_value"]
        for row in case.tables["observations"]
        if row["field_name"] == "chain_contract_token"
    }
    assert token_keys == {"InventedChainA/0xSYNTH/1", "InventedChainA/0xSYNTH/2"}
    postings = {row["posting_id"]: row for row in case.tables["postings"]}
    assert postings[case.aliases["failed-principal"]]["step_id"] is None
    assert postings[case.aliases["gas-paid"]]["step_id"] is not None
    assert _number(postings[case.aliases["gas-paid"]], "amount") == Fraction("-0.1")
    assert postings[case.aliases["orphan-credit"]]["step_id"] is None
    assert _number(postings[case.aliases["canonical-credit"]], "amount") == 1
    assert _number(postings[case.aliases["reversal-cash"]], "amount") == Fraction(
        "-0.5"
    )
    assert _number(postings[case.aliases["pool-native-in"]], "amount") == -2
    assert _number(postings[case.aliases["pool-native-out"]], "amount") == Fraction(
        "1.5"
    )
    assert all(
        row["principal_delta_coefficient"] is None
        for row in case.tables["inventory_changes"]
    )


@pytest.mark.parametrize(
    ("family", "table", "alias", "field", "value", "constraint"),
    [
        (
            "options",
            "inventory_changes",
            "call-exit",
            "principal_delta_coefficient",
            "-1",
            "full_close_components",
        ),
        (
            "claims",
            "positions",
            "etn-claim",
            "counterparty_id",
            None,
            "position_counterparty",
        ),
        (
            "obligations",
            "inventory_changes",
            "short-exit",
            "principal_delta_coefficient",
            "19",
            "full_close_components",
        ),
        (
            "perpetual",
            "reference_changes",
            "inverse-close",
            "reference_before_coefficient",
            "100",
            "reference_continuity",
        ),
        (
            "onchain",
            "postings",
            "gas-paid",
            "commodity_id",
            "@NATIVE",
            "position_leg_identity",
        ),
    ],
)
def test_family_negative_rejects_wrong_financial_record(  # noqa: PLR0913, PLR0917
    family: str, table: str, alias: str, field: str, value: object, constraint: str
) -> None:
    case = load_case("breadth/" + family)
    tables = deepcopy(case.tables)
    key = {
        "inventory_changes": "change_id",
        "positions": "position_id",
        "reference_changes": "reference_change_id",
        "postings": "posting_id",
    }[table]
    row = next(row for row in tables[table] if row[key] == case.aliases[alias])
    row[field] = (
        case.aliases[value[1:]]
        if isinstance(value, str) and value.startswith("@")
        else value
    )
    with pytest.raises(ContractError, match=constraint):
        validate_tables(tables)
