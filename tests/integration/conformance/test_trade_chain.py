import json
from copy import deepcopy
from fractions import Fraction

import pytest
from tests.integration.conformance.corpus import load_case
from tests.integration.conformance.reader import replay_rows, validate_rows

from jbt.contracts.catalog import schema_document
from jbt.contracts.primitives import ContractError
from jbt.contracts.validation import validate_tables

pytestmark = [pytest.mark.integration, pytest.mark.golden]


def _proceeds(row: dict[str, object]) -> Fraction:
    coefficient, scale = row["proceeds_total_coefficient"], row["proceeds_total_scale"]
    assert isinstance(coefficient, str)
    assert type(scale) is int
    return Fraction(int(coefficient), 10**scale)


def test_n_ary_chain_retains_both_rolls_and_their_closed_lots() -> None:
    case = load_case("trade-chain")
    validate_tables(case.tables)
    validate_rows(case.tables, schema_document())
    entity = case.tables["build"][0]["entity_id"]
    assert isinstance(entity, str)
    replay = replay_rows(case.tables, entity=entity)
    assert len(replay.inventory) == 3
    assert all(
        state.units == state.principal == 0 for state in replay.inventory.values()
    )
    members = case.tables["links"]
    assert len(members) == int(case.expected_answers["chain_members"])
    assert {row["member_id"] for row in members if row["member_role"] == "roll"} == {
        case.aliases["roll-one"],
        case.aliases["roll-two"],
    }
    assert len(case.tables["disposals"]) == 3
    assert sum(_proceeds(row) for row in case.tables["disposals"]) == Fraction(
        case.expected_answers["realized_principal"],
    )


def test_trade_chain_rejects_reusing_one_event_in_two_roles() -> None:
    case = load_case("trade-chain")
    changed = deepcopy(case.tables)
    close = next(row for row in changed["links"] if row["member_role"] == "close")
    close["member_id"] = case.aliases["open"]
    for evidence in changed["provenance"]:
        if evidence["record_kind"] == "links":
            encoded = evidence["record_id"]
            assert isinstance(encoded, str)
            key = json.loads(encoded)
            if key[-1] == case.aliases["close"]:
                key[-1] = case.aliases["open"]
                evidence["record_id"] = json.dumps(key, separators=(",", ":"))
    with pytest.raises(ContractError, match="link_distinct_members"):
        validate_tables(changed)
