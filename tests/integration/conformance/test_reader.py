import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction
from shutil import copytree
from typing import TYPE_CHECKING, cast

import pytest
from tests.integration.conformance.corpus import (
    approved_schemas,
    case_ids,
)
from tests.integration.conformance.corpus import (
    load_case as load_specimen,
)
from tests.integration.conformance.reader import (
    ReaderError,
    _json,
    _sequence_directions,
    _source_key_event_id,
    _validate_cell,
    _validate_event_support,
    _validate_position_identities,
    _validate_transfer_links,
    _wire_integers,
    canonical_bytes,
    check_assertions,
    effective_posting_date,
    logical_table_digest,
    read_snapshot,
    replay_rows,
    validate_json,
    validate_rows,
)

from jbt.artifacts.execution import ExecutionFacts, Producer
from jbt.artifacts.parquet import Compression, WriterSettings, write_table
from jbt.artifacts.reader import read_snapshot as read_production_snapshot
from jbt.artifacts.snapshot import (
    BuildInput,
    OutputPayload,
    SnapshotInput,
    assemble_snapshot,
)
from jbt.contracts.catalog import catalog, tabular_schema
from jbt.contracts.primitives import ContractError

INDEX_CASE_IDS = case_ids()
SOURCE_SEQUENCE_DIRECTIONS = {
    ("synthetic-fixture", "statement-events", "source.sequence"): "ascending"
}

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conformance.reader import ReplayResult, Row, Snapshot


@dataclass(frozen=True)
class Case:
    tables: dict[str, list[Row]]
    expected_answers: Row
    aliases: dict[str, str]
    evidence: Row


def load_case(name: str) -> Case:
    specimen = load_specimen(name)
    return Case(
        specimen.tables, specimen.expected_answers, specimen.aliases, specimen.evidence
    )


def test_independent_wire_decoder_respects_tuple_schema_and_boolean_items() -> None:
    schema = {
        "$defs": {},
        "type": "object",
        "properties": {
            "key": {
                "type": "array",
                "prefixItems": [{"type": "integer"}],
                "items": False,
            }
        },
    }
    assert _wire_integers({"key": ["2"]}, schema, schema) == {"key": [2]}
    with pytest.raises(ReaderError, match="rejected by schema"):
        _wire_integers({"key": ["2", "extra"]}, schema, schema)


@pytest.mark.parametrize("damage", ["amount", "side", "reused", "missing", "transit"])
def test_independent_transfer_reader_rejects_invalid_named_members(damage: str) -> None:
    case = load_case("multi-origin")
    _validate_transfer_links(case.tables)
    members = [m for m in case.tables["links"] if m["link_kind"] == "transfer"]
    outgoing = next(m for m in members if m["member_role"] == "outgoing")
    posting = next(
        p for p in case.tables["postings"] if p["posting_id"] == outgoing["member_id"]
    )
    if damage == "amount":
        posting["amount_coefficient"] = "-1"
    elif damage == "side":
        case.tables["links"] = [outgoing]
    elif damage == "reused":
        case.tables["links"].append({**outgoing, "link_id": "another"})
    elif damage == "missing":
        outgoing["member_id"] = "missing"
    else:
        position = next(
            p
            for p in case.tables["positions"]
            if p["position_id"] == posting["position_id"]
        )
        position["purpose"] = "in_transit"
    with pytest.raises(ReaderError, match="transfer"):
        _validate_transfer_links(case.tables)


def test_schema_cache_preserves_mutation_and_local_reference_checks() -> None:
    schema = {"type": "integer", "minimum": 0}
    validate_json(1, schema)
    schema["minimum"] = 2
    with pytest.raises(ReaderError, match="approved schema"):
        validate_json(1, schema)
    with pytest.raises(ReaderError, match="nonlocal schema"):
        validate_json({}, {"$ref": "https://invalid.example/schema"})


@pytest.mark.parametrize("case_id", INDEX_CASE_IDS)
def test_independent_rows_replay_hand_calculated_corpus(case_id: str) -> None:
    case = load_case(case_id)
    entity = case.tables["build"][0]["entity_id"]
    validate_rows(
        case.tables,
        tabular_schema(),
        source_sequence_directions=SOURCE_SEQUENCE_DIRECTIONS,
    )
    result = replay_rows(case.tables, entity=entity)
    _assert_snapshot_oracles(case, case.tables, result, entity=entity)


def test_inventory_changes_are_atomic_within_a_split_step() -> None:
    case = load_case("compound-expensed")
    entity = case.tables["build"][0]["entity_id"]
    # Targets first must not turn a split into two separate movements.
    case.tables["inventory_changes"].sort(
        key=lambda row: row["change_role"], reverse=True
    )
    result = replay_rows(case.tables, entity=entity)
    assert sum(state.units for state in result.inventory.values()) == 15
    assert (
        sum(
            state.principal
            for state in result.inventory.values()
            if state.principal is not None
        )
        == 75
    )
    assert sum(
        state.expensed_fee
        for state in result.inventory.values()
        if state.expensed_fee is not None
    ) == Fraction(3, 2)


def test_equal_local_ids_remain_entity_qualified() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    original = deepcopy(case.tables)
    for name, rows in original.items():
        case.tables[name].extend({**row, "entity_id": "other"} for row in rows)
    original_result = replay_rows(case.tables, entity=entity)
    other_result = replay_rows(case.tables, entity="other")
    position = case.aliases["cash-A"]
    assert original_result.quantity_positions == {(entity, position): Fraction(115)}
    assert other_result.quantity_positions == {("other", position): Fraction(115)}


def test_pending_and_review_flags_do_not_infer_participation() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    for transaction in case.tables["transactions"]:
        transaction["review_state"] = "ignored"
    result = replay_rows(case.tables, entity=entity)
    assert result.quantity_positions[(entity, case.aliases["cash-A"])] == 115
    case.tables["transactions"][0]["event_state"] = "pending"
    with pytest.raises(ReaderError, match="nonparticipating"):
        replay_rows(case.tables, entity=entity)


def test_date_cut_cannot_reorder_recorded_booking() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    case.tables["book_steps"][0]["effective_date"] = "2026-02-01"
    with pytest.raises(ReaderError, match="prefix"):
        replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 15))


def test_in_transit_slice_survives_between_departure_and_arrival() -> None:
    case = load_case("in-transit")
    entity = case.tables["build"][0]["entity_id"]
    between = replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 6))
    for alias, expected in case.expected_answers["between_dates"].items():
        position = case.aliases[alias]
        assert sum(
            state.units
            for key, state in between.inventory.items()
            if key[1] == position
        ) == Fraction(expected)
    transit = [
        state
        for key, state in between.inventory.items()
        if key[1] == case.aliases["transit-Q"]
    ]
    assert sum(state.units for state in transit) == 6
    assert all(state.principal is not None for state in transit)
    assert (
        sum(state.principal for state in transit if state.principal is not None) == 100
    )
    final = replay_rows(case.tables, entity=entity)
    assert (
        sum(
            state.units
            for key, state in final.inventory.items()
            if key[1] == case.aliases["transit-Q"]
        )
        == 0
    )


def test_backward_dependencies_are_rejected_not_resorted() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    steps = case.tables["book_steps"]
    case.tables["book_step_dependencies"] = [
        {
            "entity_id": entity,
            "before_step_id": steps[1]["step_id"],
            "after_step_id": steps[0]["step_id"],
        }
    ]
    with pytest.raises(ReaderError, match="backward"):
        replay_rows(case.tables, entity=entity)


def test_posting_unknown_override_prevents_transaction_inheritance() -> None:
    transaction = {"date_settled": "2026-01-02"}
    posting = {"date_settled": None, "date_settled_mode": "unknown"}
    assert effective_posting_date(posting, transaction, role="settled") is None
    posting["date_settled_mode"] = "inherit"
    assert effective_posting_date(posting, transaction, role="settled") == date(
        2026, 1, 2
    )
    posting.update(date_settled="2026-01-03", date_settled_mode="value")
    assert effective_posting_date(posting, transaction, role="settled") == date(
        2026, 1, 3
    )


def test_complete_empty_scope_is_not_a_missing_assertion() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    account = case.tables["positions"][0]["account_id"]
    case.tables["assertion_scopes"] = [
        {
            "entity_id": entity,
            "scope_id": "scope",
            "account_id": account,
            "measurement": "units",
            "scope_kind": "account_net",
            "coverage_basis": "explicit-basis",
            "selection": "account_all",
            "position_ids": [],
        }
    ]
    case.tables["balance_assertions"] = [
        {
            "entity_id": entity,
            "scope_id": "scope",
            "assertion_set_id": "empty",
            "date": "2026-01-31",
            "assertion_kind": "closing",
            "is_complete": True,
        }
    ]
    assert check_assertions(case.tables, entity=entity, date_roles={})[0].status == (
        "not_evaluable"
    )
    result = check_assertions(
        case.tables, entity=entity, date_roles={"explicit-basis": "posted"}
    )
    assert result[0].status == "fail"
    case.tables["balance_assertions"][0]["is_complete"] = False
    result = check_assertions(
        case.tables, entity=entity, date_roles={"explicit-basis": "posted"}
    )
    assert result[0].status == "pass"


def test_prior_point_is_source_only_but_in_period_opening_still_fails() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    position = case.tables["positions"][0]
    case.tables["assertion_scopes"] = [
        {
            "entity_id": entity,
            "scope_id": "opening-scope",
            "account_id": position["account_id"],
            "measurement": "units",
            "scope_kind": "positions",
            "coverage_basis": "explicit-basis",
            "selection": "positions",
            "position_ids": [position["position_id"]],
        }
    ]
    case.tables["balance_assertions"] = [
        {
            "entity_id": entity,
            "scope_id": "opening-scope",
            "assertion_set_id": identity,
            "date": when,
            "assertion_kind": kind,
            "is_complete": False,
        }
        for identity, when, kind in (
            ("prior-point", "2025-12-31", "point"),
            ("in-period-opening", "2026-01-01", "opening"),
        )
    ]
    case.tables["balances"] = [
        {
            "entity_id": entity,
            "assertion_set_id": identity,
            "position_id": position["position_id"],
            "commodity_id": position["commodity_id"],
            "amount_coefficient": "100",
            "amount_scale": 0,
            "amount_source_scale": 0,
        }
        for identity in ("prior-point", "in-period-opening")
    ]
    result = check_assertions(
        case.tables, entity=entity, date_roles={"explicit-basis": "posted"}
    )
    assert [(answer.assertion_set_id, answer.status) for answer in result] == [
        ("prior-point", "not_evaluable"),
        ("in-period-opening", "fail"),
    ]


def test_utc_midnight_point_excludes_same_day_civil_transactions() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    position = next(
        row
        for row in case.tables["positions"]
        if row["position_id"] == case.aliases["cash-A"]
    )
    transactions = case.tables["transactions"]
    transactions[0]["date_posted"] = "2025-12-31"
    transactions[1]["date_posted"] = "2026-01-01"
    case.tables["book_steps"][0]["effective_date"] = "2025-12-31"
    case.tables["book_steps"][1]["effective_date"] = "2026-01-01"
    case.tables["assertion_scopes"] = [
        {
            "entity_id": entity,
            "scope_id": "cash-scope",
            "account_id": position["account_id"],
            "measurement": "units",
            "scope_kind": "positions",
            "coverage_basis": "posted-basis",
            "selection": "positions",
            "position_ids": [position["position_id"]],
        }
    ]
    case.tables["balance_assertions"] = [
        {
            "entity_id": entity,
            "scope_id": "cash-scope",
            "assertion_set_id": identity,
            "date": when,
            "assertion_kind": "point",
            "is_complete": False,
            "timestamp_date": when,
            "timestamp_local": "00:00:00",
            "timestamp_offset_minutes": 0,
            "timestamp_zone": "GMT",
            "timestamp_precision": "second",
            "timestamp_fraction_digits": None,
        }
        for identity, when in (
            ("opening-point", "2026-01-01"),
            ("closing-point", "2026-02-01"),
        )
    ]
    case.tables["balances"] = [
        {
            "entity_id": entity,
            "assertion_set_id": identity,
            "position_id": position["position_id"],
            "commodity_id": position["commodity_id"],
            "amount_coefficient": amount,
            "amount_scale": 0,
            "amount_source_scale": 0,
        }
        for identity, amount in (("opening-point", "100"), ("closing-point", "115"))
    ]
    date_roles = {"posted-basis": "posted"}
    result = check_assertions(case.tables, entity=entity, date_roles=date_roles)
    assert [answer.status for answer in result] == ["pass", "pass"], result
    assert result[0].observed == {position["position_id"]: Fraction(100)}
    assert result[1].observed == {position["position_id"]: Fraction(115)}

    case.tables["balance_assertions"][0]["timestamp_local"] = "00:00:01"
    result = check_assertions(case.tables, entity=entity, date_roles=date_roles)
    assert [answer.status for answer in result] == ["not_evaluable", "pass"]

    case.tables["balance_assertions"][0]["timestamp_local"] = "00:00:00"
    case.tables["book_steps"][0]["timestamp_precision"] = "second"
    result = check_assertions(case.tables, entity=entity, date_roles=date_roles)
    assert [answer.status for answer in result] == [
        "not_evaluable",
        "not_evaluable",
    ]
    case.tables["book_steps"][0]["timestamp_precision"] = None
    case.tables["balance_assertions"][0]["timestamp_zone"] = "Europe/London"
    result = check_assertions(case.tables, entity=entity, date_roles=date_roles)
    assert [answer.status for answer in result] == ["not_evaluable", "pass"]


def test_reference_resets_replay_without_calculating_another_settlement() -> None:
    case = load_case("reference")
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    assert result.settlements[(entity, case.aliases["USD"])] == Fraction(
        case.expected_answers["settlement_total"]
    )
    assert result.quantity_positions[(entity, case.aliases["margin"])] == Fraction(
        case.expected_answers["collateral"]
    )
    assert result.references == {}
    reset = replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 3))
    assert reset.references[(entity, case.aliases["future"], case.aliases["FL"])] == 103
    assert reset.settlements[(entity, case.aliases["USD"])] == 30
    close = next(
        row for row in case.tables["reference_changes"] if row["change_kind"] == "close"
    )
    close["reference_before_coefficient"] = "100"
    with pytest.raises(ReaderError, match="stale reference"):
        replay_rows(case.tables, entity=entity)


def test_gross_assertions_cannot_be_replaced_by_an_equal_net() -> None:
    case = load_case("brokerage")
    entity = case.tables["build"][0]["entity_id"]
    settlement_dates = {
        case.aliases["broker-open"]: "2026-01-01",
        case.aliases["broker-fee"]: "2026-01-02",
    }
    for transaction in case.tables["transactions"]:
        transaction["date_settled"] = settlement_dates[transaction["txn_id"]]
    before = check_assertions(
        case.tables, entity=entity, date_roles={"settled-units": "settled"}
    )
    assert [answer.status for answer in before] == ["pass", "pass"]
    for row in case.tables["balances"]:
        if row["position_id"] == case.aliases["cash-A"]:
            row["amount_coefficient"] = "107"
        elif row["position_id"] == case.aliases["debt"]:
            row["amount_coefficient"] = "-50"
    after = check_assertions(
        case.tables, entity=entity, date_roles={"settled-units": "settled"}
    )
    assert sorted(answer.status for answer in after) == ["fail", "pass"]


def test_fee_lineage_tracks_original_fee_without_another_expense() -> None:
    case = load_case("compound-expensed")
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    originals = {
        identity for state in result.inventory.values() for identity in state.fees
    }
    assert len(originals) == 1
    assert sum(
        value for state in result.inventory.values() for value in state.fees.values()
    ) == Fraction(3, 2)


@pytest.mark.parametrize("mutation", ["version", "foreign", "null", "type", "order"])
def test_shipped_schema_is_interpreted_independently(mutation: str) -> None:
    case = load_case("cash")
    schema = tabular_schema()
    if mutation == "version":
        schema["schema_version"] = 2
    elif mutation == "foreign":
        case.tables["postings"][0]["txn_id"] = "missing"
    elif mutation == "null":
        case.tables["postings"][0]["amount_scale"] = None
    elif mutation == "type":
        case.tables["postings"][0]["posting_index"] = True
    else:
        case.tables["postings"].reverse()
    with pytest.raises((ReaderError, ValueError)):
        validate_rows(case.tables, schema)


@pytest.mark.parametrize(
    "mutation", ["digest", "interval", "payload", "payload_semantics"]
)
def test_independent_declaration_revision_checks_exact_meaning(
    mutation: str,
) -> None:
    case = load_case("cash")
    declaration = next(
        row
        for row in case.tables["declarations"]
        if row["declaration_kind"] == "position"
    )
    if mutation == "digest":
        declaration["revision_digest"] = "0" * 64
    elif mutation == "interval":
        declaration["valid_from"] = (
            "2026-01-01" if declaration["valid_from"] is None else "2026-01-02"
        )
    else:
        payload = json.loads(declaration["payload_json"])
        if mutation == "payload":
            payload["schema_version"] = "1"
        else:
            payload["purpose"] = "collateral"
        declaration["payload_json"] = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    with pytest.raises(
        ReaderError, match=r"declaration revision identity|kind or version"
    ):
        validate_rows(case.tables, tabular_schema())


def test_position_identity_is_explicit_authored_id_not_declaration_key_hash() -> None:
    case = load_case("cash")
    position = next(row for row in case.tables["positions"] if row["declaration_id"])
    declaration = next(
        row
        for row in case.tables["declarations"]
        if row["declaration_id"] == position["declaration_id"]
    )
    position["position_id"] = "authored-stable-position"
    declaration["declaration_key"] = "different-authored-key"
    payload = json.loads(declaration["payload_json"])
    payload["position_id"] = position["position_id"]
    declaration["payload_json"] = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    _validate_position_identities(case.tables)
    payload["position_id"] = "different-position"
    declaration["payload_json"] = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    with pytest.raises(ReaderError, match="position identity"):
        _validate_position_identities(case.tables)


def test_pool_identity_is_explicit_authored_id_not_declaration_key_hash() -> None:
    case = load_case("pools")
    pool = case.tables["inventory_pools"][0]
    declaration = next(
        row
        for row in case.tables["declarations"]
        if row["declaration_id"] == pool["declaration_id"]
    )
    pool["pool_id"] = "authored-stable-pool"
    declaration["declaration_key"] = "different-authored-key"
    payload = json.loads(declaration["payload_json"])
    payload["pool_id"] = pool["pool_id"]
    declaration["payload_json"] = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    _validate_position_identities(case.tables)
    payload["pool_id"] = "different-pool"
    declaration["payload_json"] = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    with pytest.raises(ReaderError, match="pool identity"):
        _validate_position_identities(case.tables)


def source_key_support_tables() -> dict[str, list[Row]]:
    # Fixed JBT v1 framing vector; UTF-8 lengths and textual "007" matter.
    event_id = "a630143025f422bf7de5c922f5118f450805f122f532df1bcc50deacb8c70d3e"
    return {
        "source_records": [
            {
                "entity_id": "entity",
                "source_record_id": "occurrence-original",
                "source_scope_id": "feed",
                "source_section": "account-é",
                "source_key_domain": "FITID",
                "source_key": "007",
            }
        ],
        "transactions": [
            {
                "entity_id": "entity",
                "txn_id": event_id,
                "event_state": "recognized",
            }
        ],
        "economic_identities": [
            {
                "entity_id": "entity",
                "record_kind": "transaction",
                "record_id": event_id,
                "anchor_kind": "source_key",
                "anchor_id": '["feed","account-é","FITID","007"]',
                "event_key": "movement",
                "identity_state": "active",
            }
        ],
        "event_support": [
            {
                "entity_id": "entity",
                "txn_id": event_id,
                "source_record_id": "occurrence-original",
                "event_key": "movement",
                "support_kind": "selected",
            }
        ],
        "book_steps": [],
        "postings": [],
    }


def test_independent_source_key_support_preserves_textual_identity() -> None:
    tables = source_key_support_tables()
    original = tables["source_records"][0]
    tables["source_records"].append(
        {**original, "source_record_id": "occurrence-download"}
    )
    tables["event_support"].append(
        {
            **tables["event_support"][0],
            "source_record_id": "occurrence-download",
            "event_key": "pending-movement",
            "support_kind": "confirming",
        }
    )
    _validate_event_support(tables)
    tables["transactions"][0]["description_as_stated"] = "changed mutable facts"
    tables["transactions"][0]["amount_coefficient"] = "999"
    _validate_event_support(tables)


def test_independent_support_rejects_double_assignment_of_occurrence_component() -> (
    None
):
    tables = source_key_support_tables()
    tables["transactions"].append(
        {**tables["transactions"][0], "txn_id": "other-event"}
    )
    tables["economic_identities"].append(
        {
            **tables["economic_identities"][0],
            "record_id": "other-event",
            "anchor_kind": "source_record",
            "anchor_id": "occurrence-original",
        }
    )
    tables["event_support"].append(
        {**tables["event_support"][0], "txn_id": "other-event"}
    )
    with pytest.raises(ReaderError, match="component supports multiple events"):
        _validate_event_support(tables)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "active event has no support"),
        ("confirming", "active event has no selected evidence"),
        ("source", "canonical event and source"),
        ("transaction", "canonical event and source"),
        ("identity", "canonical event and source"),
        ("identity-and-support", "economic event lacks canonical identity"),
        ("retracted", "replaced evidence only"),
    ],
)
def test_independent_support_rejects_invalid_membership(
    mutation: str, message: str
) -> None:
    tables = source_key_support_tables()
    support = tables["event_support"][0]
    if mutation == "missing":
        tables["event_support"].clear()
    elif mutation == "confirming":
        support["support_kind"] = "confirming"
    elif mutation == "source":
        support["source_record_id"] = "absent"
    elif mutation == "transaction":
        support["txn_id"] = "absent"
    elif mutation == "identity":
        tables["economic_identities"].clear()
    elif mutation == "identity-and-support":
        tables["economic_identities"].clear()
        tables["event_support"].clear()
    else:
        tables["transactions"][0]["event_state"] = "retracted"
    with pytest.raises(ReaderError, match=message):
        _validate_event_support(tables)


@pytest.mark.parametrize(
    ("state", "effect"),
    [("pending", "book_steps"), ("cancelled", "postings"), ("retracted", "book_steps")],
)
def test_independent_support_does_not_admit_nonparticipating_booked_effects(
    state: str, effect: str
) -> None:
    tables = source_key_support_tables()
    transaction = tables["transactions"][0]
    transaction["event_state"] = state
    if state == "retracted":
        tables["event_support"][0]["support_kind"] = "replaced"
    _validate_event_support(tables)
    tables[effect].append(
        {
            "entity_id": "entity",
            "txn_id": transaction["txn_id"],
            "step_id": "booked",
        }
    )
    with pytest.raises(ReaderError, match="nonparticipating event has booked effects"):
        _validate_event_support(tables)


@pytest.mark.parametrize("retired", [False, True])
def test_independent_source_key_history_keeps_original_tuple_after_retraction(
    *, retired: bool
) -> None:
    tables = source_key_support_tables()
    tables["transactions"][0]["event_state"] = "retracted"
    tables["event_support"][0]["support_kind"] = "replaced"
    if retired:
        tables["economic_identities"][0]["identity_state"] = "retired"
        tables["event_support"].clear()
    _validate_event_support(tables)
    tables["source_records"][0]["source_key"] = "replacement-key"
    with pytest.raises(ReaderError, match="original retained evidence"):
        _validate_event_support(tables)


def test_independent_retired_membership_moves_to_survivor() -> None:
    tables = source_key_support_tables()
    tables["transactions"][0]["event_state"] = "retracted"
    tables["economic_identities"][0]["identity_state"] = "retired"
    retired_support = {**tables["event_support"][0], "support_kind": "replaced"}
    tables["transactions"].append(
        {"entity_id": "entity", "txn_id": "survivor", "event_state": "recognized"}
    )
    tables["economic_identities"].append(
        {
            **tables["economic_identities"][0],
            "record_id": "survivor",
            "anchor_kind": "source_record",
            "anchor_id": "occurrence-original",
            "identity_state": "active",
        }
    )
    tables["event_support"][0]["txn_id"] = "survivor"
    _validate_event_support(tables)
    tables["event_support"].append(retired_support)
    with pytest.raises(ReaderError, match="retired identity cannot have canonical"):
        _validate_event_support(tables)


def test_independent_active_retracted_event_requires_replaced_support() -> None:
    tables = source_key_support_tables()
    tables["transactions"][0]["event_state"] = "retracted"
    tables["event_support"][0]["support_kind"] = "replaced"
    _validate_event_support(tables)
    tables["event_support"].clear()
    with pytest.raises(ReaderError, match="active event has no support"):
        _validate_event_support(tables)


def test_independent_source_key_override_can_select_another_key_family() -> None:
    tables = source_key_support_tables()
    tables["source_records"].append(
        {
            **tables["source_records"][0],
            "source_record_id": "occurrence-merged-family",
            "source_key": "other-key",
        }
    )
    original_support = {
        **tables["event_support"][0],
        "support_kind": "confirming",
    }
    tables["event_support"][0]["source_record_id"] = "occurrence-merged-family"
    tables["event_support"][0]["event_key"] = "posted-movement"
    tables["event_support"].append(original_support)
    _validate_event_support(tables)
    tables["source_records"][0]["source_key"] = "lost-original-key"
    with pytest.raises(ReaderError, match="original retained evidence"):
        _validate_event_support(tables)


@pytest.mark.parametrize("anchor_kind", ["source_record", "source_key"])
def test_independent_active_anchor_requires_its_own_supported_component(
    anchor_kind: str,
) -> None:
    tables = source_key_support_tables()
    identity = tables["economic_identities"][0]
    identity["anchor_kind"] = anchor_kind
    if anchor_kind == "source_record":
        identity["anchor_id"] = "occurrence-original"
    tables["source_records"].append(
        {
            **tables["source_records"][0],
            "source_record_id": "other-occurrence",
            "source_key": "other-key",
        }
    )
    tables["event_support"].append(
        {
            **tables["event_support"][0],
            "source_record_id": "other-occurrence",
            "event_key": "posted-movement",
            "support_kind": "confirming",
        }
    )
    _validate_event_support(tables)
    tables["event_support"][0]["event_key"] = "different-original-component"
    with pytest.raises(ReaderError, match="anchor component lacks supporting evidence"):
        _validate_event_support(tables)


def test_independent_authored_identity_cannot_have_source_support() -> None:
    tables = source_key_support_tables()
    identity = tables["economic_identities"][0]
    identity["anchor_kind"] = "declaration"
    identity["anchor_id"] = "authored-key"
    with pytest.raises(ReaderError, match="authored event cannot have source support"):
        _validate_event_support(tables)
    tables["event_support"].clear()
    _validate_event_support(tables)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("key", "original retained evidence"),
        ("domain", "original retained evidence"),
        ("scope", "original retained evidence"),
        ("section", "original retained evidence"),
        ("hash", "identity hash mismatch"),
        ("component", "identity hash mismatch"),
        ("empty-component", "event key requires nonempty NFC text"),
        ("unpaired-key", "key and domain must occur together"),
        ("unpaired-domain", "key and domain must occur together"),
        ("numeric-anchor", "four nonempty strings"),
        ("escaped-anchor", "noncanonical source-key anchor"),
        ("malformed-anchor", "invalid source-key anchor JSON"),
    ],
)
def test_independent_source_key_identity_rejects_inconsistent_anchor(
    mutation: str, message: str
) -> None:
    tables = source_key_support_tables()
    identity = tables["economic_identities"][0]
    if mutation in {"key", "domain", "scope", "section"}:
        field = {
            "key": "source_key",
            "domain": "source_key_domain",
            "scope": "source_scope_id",
            "section": "source_section",
        }[mutation]
        tables["source_records"][0][field] = "different"
    elif mutation == "hash":
        identity["record_id"] = "0" * 64
        tables["transactions"][0]["txn_id"] = identity["record_id"]
        tables["event_support"][0]["txn_id"] = identity["record_id"]
    elif mutation in {"component", "empty-component"}:
        identity["event_key"] = "" if mutation == "empty-component" else "fee"
        tables["event_support"][0]["event_key"] = identity["event_key"]
    elif mutation.startswith("unpaired"):
        tables["source_records"].append(
            {
                **tables["source_records"][0],
                "source_record_id": "unused-occurrence",
                "source_key"
                if mutation == "unpaired-key"
                else "source_key_domain": None,
            }
        )
    elif mutation == "numeric-anchor":
        identity["anchor_id"] = '["feed","account-é","FITID",7]'
    elif mutation == "escaped-anchor":
        identity["anchor_id"] = '["feed","account-\\u00e9","FITID","007"]'
    else:
        identity["anchor_id"] = "["
    with pytest.raises(ReaderError, match=message):
        _validate_event_support(tables)


def test_independent_snapshot_contract_requires_acquired_event_support() -> None:
    case = load_case("cash")
    case.tables["event_support"].clear()
    with pytest.raises(ReaderError, match="active event has no support"):
        validate_rows(case.tables, tabular_schema())


def test_independent_keyed_overlap_reconstructs_distinct_identical_purchases() -> None:
    case = load_case("keyed-overlap")
    confirmation = next(
        row
        for row in case.tables["event_support"]
        if row["txn_id"] == case.aliases["credit"]
        and row["support_kind"] == "confirming"
    )
    original_key = json.dumps(
        [
            confirmation["txn_id"],
            confirmation["source_record_id"],
            confirmation["event_key"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    confirmation["event_key"] = "pending-transaction"
    updated_key = json.dumps(
        [
            confirmation["txn_id"],
            confirmation["source_record_id"],
            confirmation["event_key"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for table in ("observations", "provenance"):
        for row in case.tables[table]:
            if (
                row["record_kind"] == "event_support"
                and row["record_id"] == original_key
            ):
                row["record_id"] = updated_key
    support_specification = next(
        row for row in tabular_schema()["tables"] if row["name"] == "event_support"
    )
    support_keys = {
        tuple(row[field] for field in support_specification["primary_key"])
        for row in case.tables["event_support"]
    }
    assert len(support_keys) == len(case.tables["event_support"])
    validate_rows(case.tables, tabular_schema())
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    assert result.quantity_positions[(entity, case.aliases["cash-A"])] == Fraction(110)
    keyed = [
        row
        for row in case.tables["economic_identities"]
        if row["anchor_kind"] == "source_key"
    ]
    assert len(keyed) == case.expected_answers["keyed_events"] == 3
    assert {json.loads(row["anchor_id"])[3] for row in keyed} == {"C1", "D1", "D2"}
    assert case.aliases["debit"] != case.aliases["identical-debit"]
    for alias, count in case.expected_answers["support_counts"].items():
        support = [
            row
            for row in case.tables["event_support"]
            if row["txn_id"] == case.aliases[alias]
        ]
        assert len(support) == count
        assert sum(row["support_kind"] == "selected" for row in support) == 1
        assert all(
            row["event_key"] == "transaction"
            for row in support
            if row["support_kind"] == "selected"
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("double-assignment", "duplicate key: event_support"),
        ("missing-support", "active event has no support"),
        ("source-key", "original retained evidence"),
        ("key-hash", "identity hash mismatch"),
        ("anchor-component", "anchor component lacks supporting evidence"),
    ],
)
def test_independent_keyed_overlap_rejects_broken_membership(
    mutation: str, message: str
) -> None:
    case = load_case("keyed-overlap")
    credit = case.aliases["credit"]
    if mutation == "double-assignment":
        support = next(
            row for row in case.tables["event_support"] if row["txn_id"] == credit
        )
        case.tables["event_support"].append(
            {**support, "txn_id": case.aliases["debit"]}
        )
        specification = next(
            row for row in tabular_schema()["tables"] if row["name"] == "event_support"
        )
        case.tables["event_support"].sort(
            key=lambda row: tuple(row[key] for key in specification["sort_key"])
        )
    elif mutation == "missing-support":
        case.tables["event_support"] = [
            row for row in case.tables["event_support"] if row["txn_id"] != credit
        ]
    elif mutation == "source-key":
        for source in case.tables["source_records"]:
            if source["source_key"] == "C1":
                source["source_key"] = "wrong-key"
    elif mutation == "anchor-component":
        for row in case.tables["event_support"]:
            if row["txn_id"] == credit:
                row["event_key"] = "other-source-component"
    else:
        identity = next(
            row
            for row in case.tables["economic_identities"]
            if row["record_id"] == credit
        )
        anchor = json.loads(identity["anchor_id"])
        anchor[3] = "wrong-key"
        identity["anchor_id"] = json.dumps(
            anchor, ensure_ascii=False, separators=(",", ":")
        )
    with pytest.raises(ReaderError, match=message):
        validate_rows(case.tables, tabular_schema())


@pytest.mark.parametrize(
    ("target_kind", "target_alias", "expected"),
    [
        (
            "inventory_allocation",
            "spinoff-allocation",
            "basis effect requires an exact inventory change target",
        ),
        (
            "inventory_change",
            "spinoff-child",
            "basis effect targets the wrong change amount",
        ),
    ],
)
def test_independent_basis_effect_targets_exact_change(
    target_kind: str, target_alias: str, expected: str
) -> None:
    case = load_case("corporate-variants")
    application = next(
        row
        for row in case.tables["action_applications"]
        if row["effect_id"] == case.aliases["spinoff-parent-basis"]
    )
    application["target_kind"] = target_kind
    application["target_id"] = case.aliases[target_alias]
    specification = next(
        row
        for row in tabular_schema()["tables"]
        if row["name"] == "action_applications"
    )
    case.tables["action_applications"].sort(
        key=lambda row: tuple(row[key] for key in specification["sort_key"])
    )
    with pytest.raises(ReaderError, match=expected):
        validate_rows(case.tables, tabular_schema())


def test_independent_reader_rejects_transferred_reference_repricing() -> None:
    case = load_case("reference-variants")
    validate_rows(case.tables, tabular_schema())
    tables = deepcopy(case.tables)
    for alias, field in (
        ("variant-transfer-in", "reference_after"),
        ("variant-three-B-close", "reference_before"),
    ):
        change = next(
            row
            for row in tables["reference_changes"]
            if row["reference_change_id"] == case.aliases[alias]
        )
        change[field + "_coefficient"] = "102"
    with pytest.raises(ReaderError, match="transferred reference changed"):
        validate_rows(tables, tabular_schema())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("domain", "incomparable source sequence scope or domain"),
        ("scope", "incomparable source sequence scope or domain"),
        ("fractional", "invalid source sequence observations"),
    ],
)
def test_independent_reader_rejects_incomparable_source_sequence(
    mutation: str, message: str
) -> None:
    case = load_case("source-sequence")
    validate_rows(
        case.tables,
        tabular_schema(),
        source_sequence_directions=SOURCE_SEQUENCE_DIRECTIONS,
    )
    tables = deepcopy(case.tables)
    if mutation == "scope":
        row = next(
            row
            for row in tables["source_records"]
            if row["source_record_id"] == case.aliases["source:debit"]
        )
        row["source_scope_id"] = "unrelated-scope"
    else:
        row = next(
            row
            for row in tables["observations"]
            if row["observation_id"]
            == case.aliases[
                "debit-domain" if mutation == "domain" else "debit-sequence"
            ]
        )
        if mutation == "domain":
            row["text_value"] = "unrelated-statement"
        else:
            row["decimal_value_scale"] = 1
    with pytest.raises(ReaderError, match=message):
        validate_rows(
            tables,
            tabular_schema(),
            source_sequence_directions=SOURCE_SEQUENCE_DIRECTIONS,
        )


def test_source_sequence_requires_one_retained_direction() -> None:
    case = load_case("source-sequence")
    with pytest.raises(ReaderError, match="missing source sequence direction"):
        validate_rows(case.tables, tabular_schema())
    entry = {
        "source_scope_id": "synthetic-fixture",
        "sequence_domain": "statement-events",
        "field_name": "source.sequence",
        "direction": "ascending",
    }
    with pytest.raises(ReaderError, match="ambiguous source sequence direction"):
        _sequence_directions(
            {"registries": [{"path": "first.json"}, {"path": "second.json"}]},
            {
                "first.json": {"source_sequences": [entry]},
                "second.json": {"source_sequences": [entry]},
            },
        )


def test_descending_source_sequence_registry_preserves_source_ordinals(
    artifact_root: Path,
) -> None:
    case = load_case("source-sequence")
    tables = deepcopy(case.tables)
    for row in tables["book_steps"]:
        if row["txn_id"] == case.aliases["credit"]:
            row["sequence"] = 2
        elif row["txn_id"] == case.aliases["debit"]:
            row["sequence"] = 1
    tables["book_steps"].sort(key=lambda row: (row["entity_id"], row["sequence"]))
    edge = tables["book_step_dependencies"][0]
    before, after = edge["before_step_id"], edge["after_step_id"]
    edge["before_step_id"], edge["after_step_id"] = after, before
    for row in tables["provenance"]:
        if row["record_kind"] == "book_step_dependencies":
            key = json.loads(row["record_id"])
            row["record_id"] = json.dumps(
                [
                    after if value == before else before if value == after else value
                    for value in key
                ],
                separators=(",", ":"),
            )
    digest = _snapshot(
        artifact_root,
        case_id="source-sequence",
        staged_tables=tables,
        sequence_direction="descending",
    )
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert snapshot.tables["book_step_dependencies"][0]["before_step_id"] == after
    assert snapshot.tables["book_step_dependencies"][0]["after_step_id"] == before
    assert {
        row["decimal_value_coefficient"]
        for row in snapshot.tables["observations"]
        if row["field_name"] == "source.sequence"
    } == {"10", "11"}


def test_publication_rejects_registered_direction_disagreeing_with_ordinals(
    artifact_root: Path,
) -> None:
    with pytest.raises(ContractError, match="source_sequence_direction"):
        _snapshot(
            artifact_root,
            case_id="source-sequence",
            sequence_direction="descending",
        )


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(scope="module")
def cash_snapshot(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("reader-cash")
    return root, _snapshot(root)


@pytest.fixture
def mutable_cash_snapshot(
    artifact_root: Path, cash_snapshot: tuple[Path, str]
) -> tuple[Path, str]:
    root, digest = cash_snapshot
    copytree(root, artifact_root, dirs_exist_ok=True)
    return artifact_root, digest


def _snapshot(  # noqa: PLR0913
    root: Path,
    *,
    empty: bool = False,
    case_id: str = "cash",
    staged_tables: dict[str, list[Row]] | None = None,
    sequence_direction: str = "ascending",
    output_payloads: tuple[OutputPayload, ...] = (),
    extra_schemas: dict[str, dict] | None = None,
) -> str:
    case = load_specimen(case_id)
    tables = staged_tables if staged_tables is not None else case.tables
    row = tables["build"][0]
    configuration = canonical_bytes(
        {
            "schema_version": 1,
            "as_of": "2026-12-31",
            "source_authority": [],
            "rendering": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "declaration_source_order": [],
        },
        integer_strings=False,
    )
    sequence_registry = (
        {
            "schema_version": 1,
            "observation_fields": [
                {
                    "field_name": "source.sequence",
                    "value_type": "decimal",
                    "units": "ordinal",
                    "payload_schema": None,
                },
                {
                    "field_name": "source.sequence_domain",
                    "value_type": "text",
                    "units": "none",
                    "payload_schema": None,
                },
            ],
            "status_mappings": [],
            "source_sequences": [
                {
                    "source_scope_id": "synthetic-fixture",
                    "sequence_domain": "statement-events",
                    "field_name": "source.sequence",
                    "direction": sequence_direction,
                }
            ],
            "coverage_bases": [],
            "field_registry": [],
        }
        if case_id == "source-sequence"
        else None
    )
    registry_bytes = (
        canonical_bytes(sequence_registry, integer_strings=False)
        if sequence_registry is not None
        else None
    )
    definition_ids = sorted(
        {
            row["derivation_id"]
            for row in tables["provenance"]
            if row["derivation_id"] is not None and not empty
        },
        key=str,
    )
    definitions = {
        identity: {"version": 1, "meaning": "synthetic fixture determination"}
        for identity in definition_ids
    }
    definition_bytes = canonical_bytes(
        {"schema_version": 1, "definitions": definitions},
        integer_strings=False,
    )
    metadata = {
        "artifacts": [],
        "registries": (
            [
                {
                    "registry_id": "synthetic-sequence",
                    "schema_id": "registry_schema",
                    "path": "registry.json",
                    "byte_digest": hashlib.sha256(registry_bytes).hexdigest(),
                    "content_digest": hashlib.sha256(
                        canonical_bytes(sequence_registry)
                    ).hexdigest(),
                }
            ]
            if registry_bytes is not None
            else []
        ),
        "effective_declarations": [
            {"entity_id": row["entity_id"], "declaration_id": row["declaration_id"]}
            for row in tables["declarations"]
            if not empty and row["declaration_kind"] != "identity"
        ],
        "bindings": [
            {
                "entity_id": row["entity_id"],
                "source_scope_id": "synthetic-fixture",
                "importer_id": "synthetic-fixture",
                "account_mappings": [],
            }
        ]
        if not empty
        else [],
        "recovery_set": {
            name: []
            for name in (
                "vault_objects",
                "accession_records",
                "authored_history",
                "dependencies",
                "external_assets",
                "runtime",
            )
        },
        "configuration": {
            "path": "configuration.json",
            "byte_digest": hashlib.sha256(configuration).hexdigest(),
        },
        "derivation_definitions": {
            "path": "derivations.json",
            "byte_digest": hashlib.sha256(definition_bytes).hexdigest(),
        },
        "toolchain": [
            {"component": "fixture", "version": "1", "content_digest": "c" * 64}
        ],
        "derivations": [
            {
                "derivation_id": identity,
                "algorithm": "fixture-supplied-determinations",
                "version": "1",
                "declaration_ids": [],
                "units": "declared-components",
                "content_digest": hashlib.sha256(
                    canonical_bytes(definitions[identity], integer_strings=False)
                ).hexdigest(),
            }
            for identity in definition_ids
        ],
        "capabilities": [],
        "checks": [],
        "outputs": [{"sink": "tabular", "required_checks": []}],
        "inputs": [],
    }
    metadata["recovery_set"]["vault_objects"] = [
        {
            "object_id": digest,
            "path": "retained/" + digest,
            "digest_algorithm": "sha256",
            "digest": digest,
            "size_bytes": len(text.encode()),
        }
        for digest, text in sorted(
            cast("dict[str, str]", case.evidence["blobs"]).items()
        )
        if not empty
    ]
    result = assemble_snapshot(
        root,
        SnapshotInput(
            tables={
                name: [] if empty else rows
                for name, rows in tables.items()
                if name != "build"
            },
            builds=[
                BuildInput(
                    entity_id=cast("str", row["entity_id"]),
                    producer_version="independent-reader-test",
                    as_of=cast("str", row["as_of"]),
                    input_fingerprint="a" * 64,
                )
            ],
            metadata=metadata,
            resources={
                "configuration.json": configuration,
                "derivations.json": definition_bytes,
                **(
                    {"registry.json": registry_bytes}
                    if registry_bytes is not None
                    else {}
                ),
            },
            execution=ExecutionFacts(Producer("fixture", "1", "c" * 64), "d" * 64),
            writer_version="synthetic",
            output_payloads=output_payloads,
            extra_schemas=extra_schemas or {},
        ),
        WriterSettings(
            compression=Compression.ZSTD,
            row_group_size=1024,
            dictionary=False,
            statistics=True,
        ),
    )
    return result.descriptor_digest


def test_duplicate_extract_artifacts_are_not_duplicate_financial_evidence(
    tmp_path: Path,
) -> None:
    document = {
        "schema_version": 1,
        "source_scope_id": "selected-scope",
        "source_blob_digest": "sha256:synthetic",
        "importer_id": "ofx",
        "source_scope": {"sections": []},
        "status_mappings": [],
        "records": [],
        "unsupported_content": [],
    }
    payload = canonical_bytes(document, integer_strings=False)
    duplicate = OutputPayload(
        "extract",
        "extract-1.json",
        payload,
        hashlib.sha256(payload).hexdigest(),
        "extraction_schema",
    )
    original = OutputPayload(
        "extract",
        "extract-0.json",
        payload,
        hashlib.sha256(payload).hexdigest(),
        "extraction_schema",
    )
    digests = []
    for name, artifacts in (
        ("single", (original,)),
        ("duplicate", (original, duplicate)),
    ):
        root = tmp_path / name
        root.mkdir()
        descriptor_digest = _snapshot(root, empty=True, output_payloads=artifacts)
        snapshot = read_snapshot(
            root,
            descriptor_digest=descriptor_digest,
            expected_schema_digests=approved_schemas(),
        )
        digests.append(snapshot.financial_digest)
        assert len(snapshot.manifest["artifacts"]) == len(artifacts)
    assert digests[0] == digests[1]


def test_typed_empty_snapshot_is_independently_readable(artifact_root: Path) -> None:
    digest = _snapshot(artifact_root, empty=True)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert all(not rows for name, rows in snapshot.tables.items() if name != "build")
    assert len(snapshot.tables["build"]) == 1


def test_execution_observations_preserve_independent_financial_identity(
    artifact_root: Path,
) -> None:
    digests = []
    for name, runtime_version, distributions in (
        ("baseline", "3.14.0", []),
        ("observed", "3.14.1", [{"name": "extra", "version": "2"}]),
    ):
        root = artifact_root / name
        root.mkdir()
        content = canonical_bytes(
            {
                "schema_version": 1,
                "producer": {
                    "kind": "release",
                    "name": "jbt",
                    "version": "0.1.0a1",
                    "source_commit": "synthetic-commit",
                },
                "runtime": {
                    "implementation": "cpython",
                    "version": runtime_version,
                    "platform": "synthetic",
                    "architecture": "synthetic",
                },
                "distributions": distributions,
                "importers": [],
            },
            integer_strings=False,
        )
        digest = _snapshot(
            root,
            output_payloads=(
                OutputPayload(
                    "execution",
                    "execution.json",
                    content,
                    hashlib.sha256(content).hexdigest(),
                    "execution_record_schema",
                ),
            ),
        )
        snapshot = read_snapshot(
            root,
            descriptor_digest=digest,
            expected_schema_digests=approved_schemas(),
        )
        production = read_production_snapshot(root, digest)
        assert production.outputs["execution.json"] == content
        assert (
            snapshot.financial_digest == production.manifest["logical_content_digest"]
        )
        digests.append(snapshot.financial_digest)
    assert digests[0] == digests[1]


def test_independent_reader_accepts_opaque_inventory(artifact_root: Path) -> None:
    payload = b"opaque summary, not JSON"
    digest = _snapshot(
        artifact_root,
        empty=True,
        output_payloads=(
            OutputPayload(
                "summary", "summary.json", payload, hashlib.sha256(payload).hexdigest()
            ),
        ),
    )
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert snapshot.manifest["artifacts"][0]["path"] == "summary.json"


def test_extra_schema_requires_independent_pin_and_does_not_change_financial_content(
    tmp_path: Path,
) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"revision": {"type": "string"}},
        "required": ["revision"],
        "additionalProperties": False,
    }
    pins = {
        **approved_schemas(),
        "project_schema.json": hashlib.sha256(
            canonical_bytes(schema, integer_strings=False)
        ).hexdigest(),
    }
    snapshots = []
    for index, revision in enumerate(("first", "second")):
        root = tmp_path / f"snapshot_{index}"
        root.mkdir()
        payload = canonical_bytes({"revision": revision}, integer_strings=False)
        digest = _snapshot(
            root,
            empty=True,
            extra_schemas={"project_schema": schema},
            output_payloads=(
                OutputPayload(
                    "project",
                    "project.json",
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    "project_schema",
                ),
            ),
        )
        with pytest.raises(ReaderError, match="unsupported schema resource inventory"):
            read_snapshot(
                root,
                descriptor_digest=digest,
                expected_schema_digests=approved_schemas(),
            )
        snapshots.append(
            read_snapshot(
                root,
                descriptor_digest=digest,
                expected_schema_digests=pins,
            )
        )
        production = read_production_snapshot(
            root, digest, extra_schemas={"project_schema": schema}
        )
        assert (
            production.manifest["logical_content_digest"]
            == snapshots[-1].financial_digest
        )
    assert snapshots[0].financial_digest == snapshots[1].financial_digest


@pytest.mark.parametrize("case_id", ["compound-capitalized", "pools", "reference"])
def test_serialized_snapshot_replays_representative_financial_shapes(
    artifact_root: Path, case_id: str
) -> None:
    digest = _snapshot(artifact_root, case_id=case_id)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    production = read_production_snapshot(artifact_root, digest)
    assert production.manifest["logical_content_digest"] == snapshot.financial_digest
    case = load_case(case_id)
    entity = snapshot.tables["build"][0]["entity_id"]
    result = replay_rows(snapshot.tables, entity=entity)
    _assert_snapshot_oracles(case, snapshot.tables, result, entity=entity)
    if case_id == "reference":
        assert result.references == {}
        assert result.settlements[(entity, case.aliases["USD"])] == Fraction(
            case.expected_answers["settlement_total"]
        )


def _assert_snapshot_oracles(
    case: Case, tables: dict[str, list[Row]], result: ReplayResult, *, entity: str
) -> None:
    verified = _assert_position_oracles(case, result, entity=entity)
    verified += _assert_inventory_oracles(case, result)
    verified += _assert_special_oracles(case, tables, result, entity=entity)
    verified += _assert_time_loan_oracles(case, tables)
    assert verified, "no independent oracle applied to indexed fixture"


def _assert_position_oracles(case: Case, result: ReplayResult, *, entity: str) -> int:
    verified = 0
    for family in ("positions", "quantities"):
        for alias, expected in case.expected_answers.get(family, {}).items():
            position = case.aliases[alias]
            states = [
                state for key, state in result.inventory.items() if key[1] == position
            ]
            actual = (
                sum(state.units for state in states)
                if states
                else result.quantity_positions.get((entity, position), Fraction())
            )
            assert actual == Fraction(expected), (family, alias)
            verified += 1
    return verified


def _assert_inventory_oracles(case: Case, result: ReplayResult) -> int:
    verified = 0
    for family in ("inventory", "pools"):
        for alias, expected in case.expected_answers.get(family, {}).items():
            states = [
                state
                for key, state in result.inventory.items()
                if key[1 if family == "inventory" else 3] == case.aliases[alias]
            ]
            assert states, (family, alias)
            for component, amount in expected.items():
                values = [getattr(state, component) for state in states]
                if amount is None:
                    assert all(value is None for value in values), alias
                else:
                    assert all(value is not None for value in values), alias
                    assert sum(value for value in values if value is not None) == (
                        Fraction(amount)
                    ), (alias, component)
                verified += 1
    return verified


def _assert_special_oracles(
    case: Case, tables: dict[str, list[Row]], result: ReplayResult, *, entity: str
) -> int:
    verified = 0
    if "outcomes_before_resolution" in case.expected_answers:
        before = replay_rows(tables, entity=entity, effective_date=date(2026, 4, 1))
        for label, replay in (
            ("outcomes_before_resolution", before),
            ("outcomes_after_resolution", result),
        ):
            for commodity, expected in case.expected_answers[label].items():
                position = case.aliases["outcome-" + commodity.removeprefix("O")]
                assert replay.quantity_positions[(entity, position)] == Fraction(
                    expected
                )
                verified += 1
    if "settlement_total" in case.expected_answers:
        assert sum(result.settlements.values()) == Fraction(
            case.expected_answers["settlement_total"]
        )
        verified += 1
    if "same_day_units" in case.expected_answers:
        assert sum(
            state.units
            for key, state in result.inventory.items()
            if key[1] == case.aliases["AQ"]
        ) == Fraction(case.expected_answers["same_day_units"])
        verified += 1
    if "same_day_split_then_buy" in case.expected_answers:
        same_day = replay_rows(tables, entity=entity, effective_date=date(2026, 1, 30))
        assert sum(
            state.units
            for key, state in same_day.inventory.items()
            if key[1] == case.aliases["AQ"]
        ) == Fraction(case.expected_answers["same_day_split_then_buy"])
        verified += 1
    if "empty_count" in case.expected_answers:
        counts = {row["assertion_set_id"]: 0 for row in tables["balance_assertions"]}
        for row in tables["balances"]:
            counts[row["assertion_set_id"]] += 1
        assert sorted(counts.values()) == sorted(
            (
                case.expected_answers["empty_count"],
                case.expected_answers["selected_count"],
            )
        )
        verified += 1
    if "split_survivors" in case.expected_answers:
        assert (
            sum(
                row["identity_state"] == "active"
                for row in tables["economic_identities"]
            )
            == case.expected_answers["split_survivors"]
        )
        verified += 1
    return (
        verified
        + _assert_trade_chain(case, tables)
        + _assert_three_lot(case, tables)
        + _assert_corporate_variants(case, tables)
        + _assert_original_fees(case, tables)
        + _assert_source_sequence(case, tables)
    )


def _assert_trade_chain(case: Case, tables: dict[str, list[Row]]) -> int:
    if "chain_members" not in case.expected_answers:
        return 0
    members = [row for row in tables["links"] if row["link_kind"] == "trade_chain"]
    assert len(members) == int(case.expected_answers["chain_members"])
    assert len({row["member_id"] for row in members}) == len(members)
    assert sum(row["member_role"] == "roll" for row in members) == int(
        case.expected_answers["roll_members"]
    )
    allocations = {row["allocation_id"] for row in tables["disposals"]}
    released = sum(
        -Fraction(
            int(row["principal_delta_coefficient"]),
            10 ** row["principal_delta_scale"],
        )
        for row in tables["inventory_changes"]
        if row["allocation_id"] in allocations and row["change_role"] == "source"
    )
    assert released == Fraction(case.expected_answers["realized_principal"])
    return 1


def _assert_three_lot(case: Case, tables: dict[str, list[Row]]) -> int:
    if "selected_origins" not in case.expected_answers:
        return 0
    slices = [
        row
        for row in tables["inventory_changes"]
        if row["posting_id"] == case.aliases["sale-q"]
    ]
    assert len(slices) == case.expected_answers["selected_origins"]
    assert len({row["allocation_id"] for row in slices}) == len(slices)
    assert {
        row["lot_id"]: (
            Fraction(
                int(row["units_delta_coefficient"]), 10 ** row["units_delta_scale"]
            ),
            Fraction(
                int(row["principal_delta_coefficient"]),
                10 ** row["principal_delta_scale"],
            ),
        )
        for row in slices
    } == {
        case.aliases[alias]: values
        for alias, values in {
            "L1": (Fraction(-1), Fraction(-10)),
            "L2": (Fraction(-1), Fraction(-20)),
            "L3": (Fraction(-2), Fraction(-60)),
        }.items()
    }
    allocation_ids = {row["allocation_id"] for row in slices}
    disposals = [
        row for row in tables["disposals"] if row["allocation_id"] in allocation_ids
    ]
    assert len(disposals) == len(slices)
    proceeds = sum(
        (
            Fraction(
                int(row["proceeds_total_coefficient"]),
                10 ** row["proceeds_total_scale"],
            )
            for row in disposals
        ),
        Fraction(),
    )
    released = -sum(
        (
            Fraction(
                int(row["principal_delta_coefficient"]),
                10 ** row["principal_delta_scale"],
            )
            for row in slices
        ),
        Fraction(),
    )
    assert released == Fraction(case.expected_answers["released_principal"])
    assert proceeds == Fraction(case.expected_answers["proceeds"])
    assert proceeds - released == Fraction(case.expected_answers["gain"])
    return 1


def _assert_corporate_variants(case: Case, tables: dict[str, list[Row]]) -> int:
    if "pre_action_gain" not in case.expected_answers:
        return 0

    def amount(row: Row, field: str) -> Fraction:
        return Fraction(int(row[field + "_coefficient"]), 10 ** row[field + "_scale"])

    changes = {row["change_id"]: row for row in tables["inventory_changes"]}
    effects = {row["effect_id"]: row for row in tables["corporate_action_effects"]}
    applications = {row["effect_id"]: row for row in tables["action_applications"]}
    reverse = effects[case.aliases["reverse-units"]]
    assert amount(reverse, "ratio_numerator") == 1
    assert amount(reverse, "ratio_denominator") == 5
    assert amount(changes[case.aliases["reverse-target"]], "units_delta") == Fraction(
        "1.6"
    )
    for effect, target, principal in (
        ("spinoff-parent-basis", "spinoff-parent", 60),
        ("spinoff-child-basis", "spinoff-child", 20),
        ("capital-basis-effect", "capital-target", 40),
    ):
        application = applications[case.aliases[effect]]
        assert application["target_kind"] == "inventory_change"
        assert application["target_id"] == case.aliases[target]
        assert amount(changes[application["target_id"]], "principal_delta") == principal
    disposal = tables["disposals"][0]
    sale = next(
        row
        for row in tables["inventory_changes"]
        if row["allocation_id"] == disposal["allocation_id"]
    )
    assert amount(disposal, "proceeds_total") + amount(
        sale, "principal_delta"
    ) == Fraction(case.expected_answers["pre_action_gain"])
    assert -amount(changes[case.aliases["capital-source"]], "principal_delta") - amount(
        changes[case.aliases["capital-target"]], "principal_delta"
    ) == Fraction(case.expected_answers["return_of_capital"])
    lots = {row["lot_id"]: row for row in tables["lots"]}
    assert (
        lots[case.aliases["child"]]["acquisition_date"]
        == (case.expected_answers["original_acquisition_date"])
    )
    assert lots[case.aliases["reinvested"]]["acquisition_date"] == "2026-03-11"
    reinvestment = changes[case.aliases["reinvest-target"]]
    assert amount(reinvestment, "units_delta") == Fraction("0.5")
    assert amount(reinvestment, "principal_delta") == 10
    denomination = effects[case.aliases["redenomination-units"]]
    assert denomination["input_commodity_id"] == case.aliases["OLD"]
    assert denomination["resulting_commodity_id"] == case.aliases["NEW"]
    assert amount(denomination, "ratio_numerator") == 1
    assert amount(denomination, "ratio_denominator") == 2
    assert case.aliases["OLD"] != case.aliases["NEW"]
    assert {row["commodity_id"] for row in tables["commodity_symbols"]} == {
        case.aliases["Q"]
    }
    assert {row["symbol"] for row in tables["commodity_symbols"]} == {
        "Q",
        "Q-NEW",
        "Q-é",
    }
    return 1


def _assert_original_fees(case: Case, tables: dict[str, list[Row]]) -> int:
    if "original_fees" not in case.expected_answers:
        return 0
    for currency, expected in case.expected_answers["original_fees"].items():
        source = case.aliases["variant-" + currency.lower() + "-fee"]
        allocations = [
            row for row in tables["fee_allocations"] if row["fee_posting_id"] == source
        ]
        assert len(allocations) == 2
        assert {row["target_id"] for row in allocations} == {
            case.aliases["variant-one"],
            case.aliases["variant-three"],
        }
        assert all(
            row["commodity_id"] == case.aliases[currency]
            and row["treatment"] == "attribution_only"
            and row["book_amount_coefficient"] is None
            for row in allocations
        )
        attributed = -sum(
            (
                Fraction(int(row["amount_coefficient"]), 10 ** row["amount_scale"])
                for row in allocations
            ),
            Fraction(),
        )
        assert attributed == Fraction(expected)
        changes = [
            row
            for row in tables["inventory_fee_changes"]
            if row["fee_allocation_id"]
            in {allocation["fee_allocation_id"] for allocation in allocations}
        ]
        assert len(changes) == len(allocations)
        assert all(row["book_amount_delta_coefficient"] is None for row in changes)
    return 1


def _assert_source_sequence(case: Case, tables: dict[str, list[Row]]) -> int:
    if "credit-sequence" not in case.aliases:
        return 0
    observations = {row["observation_id"]: row for row in tables["observations"]}
    sources = {row["source_record_id"]: row for row in tables["source_records"]}
    for alias, expected in (("credit", 10), ("debit", 11)):
        sequence = observations[case.aliases[alias + "-sequence"]]
        domain = observations[case.aliases[alias + "-domain"]]
        assert sequence["decimal_value_scale"] == 0
        assert int(sequence["decimal_value_coefficient"]) == expected
        assert domain["text_value"] == "statement-events"
        assert (
            sources[sequence["source_record_id"]]["source_scope_id"]
            == (sources[domain["source_record_id"]]["source_scope_id"])
            == "synthetic-fixture"
        )
    assert any(
        row["constraint_kind"] == "source_sequence"
        and row["before_step_id"] == case.aliases["step:credit"]
        and row["after_step_id"] == case.aliases["step:debit"]
        for row in tables["book_step_dependencies"]
    )
    return 1


def _assert_time_loan_oracles(case: Case, tables: dict[str, list[Row]]) -> int:
    if "fold_utc" not in case.expected_answers:
        return 0
    choice = case.evidence["consumer"]["fold_resolution"]
    assert choice == {
        "tzdb_version": "2026a",
        "candidate_offsets": [-240, -300],
        "decision": "later",
        "offset_minutes": -300,
        "shift_minutes": 0,
    }
    for alias, expected in (
        ("fold-deposit", "fold_utc"),
        ("late-local-credit", "late_local_utc"),
    ):
        observed = next(
            row
            for row in tables["event_times"]
            if row["record_id"] == case.aliases[alias] and row["date_role"] == "posted"
        )
        offset = observed["timestamp_offset_minutes"]
        if alias == "fold-deposit":
            assert offset == choice["offset_minutes"]
        instant = datetime.fromisoformat(
            observed["timestamp_date"] + "T" + observed["timestamp_local"]
        ).replace(tzinfo=timezone(timedelta(minutes=offset)))
        assert (
            instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            == (case.expected_answers[expected])
        )
    account = next(
        row for row in tables["accounts"] if row["account_id"] == case.aliases["P"]
    )
    declaration = next(
        row for row in tables["declarations"] if row["declaration_key"] == "P"
    )
    periods = json.loads(declaration["payload_json"])["expected_periods"]
    statements = {row["statement_id"]: row for row in tables["statements"]}
    assert (
        statements[case.aliases["irregular-first"]]["period_end"]
        == (case.expected_answers["first_period_end"])
    )
    assert periods[-1] == {
        key: case.expected_answers["empty_period"][key]
        for key in ("start", "end", "due")
    }
    empty_statement = statements[case.aliases["irregular-empty"]]
    assert (
        empty_statement["period_start"],
        empty_statement["period_end"],
    ) == (
        case.expected_answers["empty_period"]["start"],
        case.expected_answers["empty_period"]["end"],
    )
    transactions = {row["txn_id"]: row for row in tables["transactions"]}
    assert sum(
        1
        for row in tables["postings"]
        if row["account_id"] == case.aliases["P"]
        and empty_statement["period_start"]
        <= transactions[row["txn_id"]]["date_posted"]
        <= empty_statement["period_end"]
    ) == int(case.expected_answers["empty_period"]["movements"])
    assert account["closed_date"] == case.expected_answers["closed_date"]
    assert tables["build"][0]["as_of"] == case.expected_answers["as_of"]
    postings = {row["posting_id"]: row for row in tables["postings"]}
    gross = postings[case.aliases["mortgage-cash"]]
    assert -Fraction(
        int(gross["amount_coefficient"]), 10 ** gross["amount_scale"]
    ) == Fraction(case.expected_answers["known_gross_payment"])
    for alias, expected in (
        ("mortgage-principal", "known_principal_repaid"),
        ("mortgage-interest", "known_interest_expense"),
    ):
        row = postings[case.aliases[alias]]
        assert Fraction(
            int(row["amount_coefficient"]), 10 ** row["amount_scale"]
        ) == Fraction(case.expected_answers[expected])
    pending_rows = [
        row
        for row in tables["postings"]
        if row["txn_id"] == case.aliases["unallocated-loan-observation"]
    ]
    assert len(pending_rows) == 1
    pending = pending_rows[0]
    assert pending["step_id"] is None
    assert Fraction(
        int(pending["amount_coefficient"]), 10 ** pending["amount_scale"]
    ) == -Fraction(case.expected_answers["unallocated_gross"])
    assert case.expected_answers["unallocated_principal"] is None
    assert case.expected_answers["unallocated_interest"] is None
    return 1


def test_read_exact_pinned_snapshot_with_producer_imports_denied(
    cash_snapshot: tuple[Path, str],
) -> None:
    root, digest = cash_snapshot
    script = """
import importlib.abc
import json
import sys
class DenyProducer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jbt" or fullname.startswith("jbt."):
            raise ImportError("producer imports prohibited")
sys.meta_path.insert(0, DenyProducer())
from pathlib import Path
from tests.integration.conformance.reader import read_snapshot, replay_rows
snapshot = read_snapshot(
    Path(sys.argv[1]), descriptor_digest=sys.argv[2],
    expected_schema_digests=json.loads(sys.argv[3]),
)
entity = snapshot.tables["build"][0]["entity_id"]
result = replay_rows(snapshot.tables, entity=entity)
assert list(result.quantity_positions.values()) == [115]
assert not any(name == "jbt" or name.startswith("jbt.") for name in sys.modules)
print(snapshot.financial_digest)
"""
    result = subprocess.run(  # noqa: S603 — fixed local script, no shell.
        [
            sys.executable,
            "-c",
            script,
            str(root),
            digest,
            json.dumps(approved_schemas()),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    snapshot = read_snapshot(
        root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert result.stdout.strip() == snapshot.financial_digest
    production = read_production_snapshot(root, digest)
    assert production.manifest["logical_content_digest"] == snapshot.financial_digest


@pytest.mark.parametrize(
    "mutation", ["bytes", "extra", "missing", "symlink", "version"]
)
def test_pinned_snapshot_rejects_corruption(
    mutable_cash_snapshot: tuple[Path, str], mutation: str
) -> None:
    artifact_root, digest = mutable_cash_snapshot
    if mutation == "bytes":
        (artifact_root / "accounts.parquet").write_bytes(b"changed")
    elif mutation == "extra":
        (artifact_root / "unexpected.json").write_text("{}")
    elif mutation == "missing":
        (artifact_root / "accounts.parquet").unlink()
    elif mutation == "symlink":
        (artifact_root / "accounts.parquet").unlink()
        (artifact_root / "accounts.parquet").symlink_to(
            artifact_root / "postings.parquet"
        )
    else:
        descriptor = json.loads((artifact_root / "descriptor.json").read_bytes())
        descriptor["schema_version"] = "2"
        content = canonical_bytes(descriptor)
        (artifact_root / "descriptor.json").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
    with pytest.raises(ReaderError):
        read_snapshot(
            artifact_root,
            descriptor_digest=digest,
            expected_schema_digests=approved_schemas(),
        )


def test_version_one_schema_cannot_redefine_approved_table_semantics(
    mutable_cash_snapshot: tuple[Path, str],
) -> None:
    artifact_root, _ = mutable_cash_snapshot
    schema_path = artifact_root / "tabular_schema.json"
    schema = json.loads(schema_path.read_bytes())
    schema["tables"][0]["columns"][0]["nullable"] = True
    schema_path.write_bytes(canonical_bytes(schema, integer_strings=False))
    descriptor_path = artifact_root / "descriptor.json"
    descriptor = json.loads(descriptor_path.read_bytes())
    schema_entry = next(
        entry
        for entry in descriptor["payloads"]
        if entry["path"] == "tabular_schema.json"
    )
    schema_entry["byte_digest"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    descriptor_bytes = canonical_bytes(descriptor)
    descriptor_path.write_bytes(descriptor_bytes)
    with pytest.raises(ReaderError, match="unapproved schema semantics"):
        read_snapshot(
            artifact_root,
            descriptor_digest=hashlib.sha256(descriptor_bytes).hexdigest(),
            expected_schema_digests=approved_schemas(),
        )


def test_rechecks_semantics_after_mechanical_checksums_are_recomputed(
    mutable_cash_snapshot: tuple[Path, str],
) -> None:
    artifact_root, digest = mutable_cash_snapshot
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    postings = snapshot.tables["postings"]
    postings.append(deepcopy(postings[-1]))
    mutated_digest = _rehash_mechanical_checksums(
        artifact_root, snapshot, table="postings"
    )
    with pytest.raises(ReaderError, match="duplicate key: postings"):
        read_snapshot(
            artifact_root,
            descriptor_digest=mutated_digest,
            expected_schema_digests=approved_schemas(),
        )


def _rehash_mechanical_checksums(
    root: Path,
    snapshot: Snapshot,
    *,
    table: str | None = None,
    resource: str | None = None,
) -> str:
    """Reseal custody; semantic rejection must precede financial identity."""
    settings = WriterSettings(
        compression=Compression.ZSTD,
        row_group_size=1024,
        dictionary=False,
        statistics=True,
    )
    specifications = {table.name: table for table in catalog()}
    manifest = snapshot.manifest
    if table is not None:
        write_table(
            root / (table + ".parquet"),
            specifications[table],
            snapshot.tables[table],
            settings,
        )
        entry = next(row for row in manifest["tables"] if row["name"] == table)
        columns = next(
            item["columns"]
            for item in snapshot.schema["tables"]
            if item["name"] == table
        )
        entry["byte_digest"] = hashlib.sha256(
            (root / (table + ".parquet")).read_bytes()
        ).hexdigest()
        entry["logical_digest"] = logical_table_digest(
            columns, snapshot.tables[table], version=1
        )
        entry["row_count"] = str(len(snapshot.tables[table]))
    if resource is not None:
        entry = next(
            row
            for row in [manifest["configuration"], *manifest["registries"]]
            if row["path"] == resource
        )
        entry["byte_digest"] = hashlib.sha256(
            (root / resource).read_bytes()
        ).hexdigest()
    manifest_bytes = canonical_bytes(manifest)
    (root / "manifest.json").write_bytes(manifest_bytes)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    for row in snapshot.tables["build"]:
        row["manifest_digest"] = manifest_digest
    write_table(
        root / "build.parquet",
        specifications["build"],
        snapshot.tables["build"],
        settings,
    )
    descriptor = json.loads((root / "descriptor.json").read_bytes())
    descriptor["manifest_digest"] = manifest_digest
    for payload in descriptor["payloads"]:
        payload["byte_digest"] = hashlib.sha256(
            (root / payload["path"]).read_bytes()
        ).hexdigest()
    descriptor_bytes = canonical_bytes(descriptor)
    (root / "descriptor.json").write_bytes(descriptor_bytes)
    return hashlib.sha256(descriptor_bytes).hexdigest()


def test_reversed_source_ordinals_fail_after_snapshot_is_mechanically_rehashed(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root, case_id="source-sequence")
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    case = load_case("source-sequence")
    observations = snapshot.tables["observations"]
    for alias, replacement in (("credit-sequence", "11"), ("debit-sequence", "10")):
        row = next(
            row for row in observations if row["observation_id"] == case.aliases[alias]
        )
        row["decimal_value_coefficient"] = replacement
    mutated_digest = _rehash_mechanical_checksums(
        artifact_root, snapshot, table="observations"
    )
    with pytest.raises(
        ReaderError, match="source sequence contradicts registered direction"
    ):
        read_snapshot(
            artifact_root,
            descriptor_digest=mutated_digest,
            expected_schema_digests=approved_schemas(),
        )


def test_independent_snapshot_rejects_overlapping_source_authority_after_rehash(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    case = load_case("cash")
    configuration_path = artifact_root / "configuration.json"
    configuration = json.loads(configuration_path.read_bytes())

    def authority(source: str, start: str, end: str) -> Row:
        return {
            "account_id": case.aliases["A"],
            "source_scope_id": source,
            "period_start": start,
            "period_end": end,
            "fact_kind": "movement",
            "coverage_basis_id": "settled-units",
            "mode": "confirming",
        }

    configuration["source_authority"] = [
        authority("credit", "2026-01-01", "2026-01-15"),
        authority("debit", "2026-01-14", "2026-01-31"),
    ]
    configuration_path.write_bytes(
        canonical_bytes(configuration, integer_strings=False)
    )
    mutated_digest = _rehash_mechanical_checksums(
        artifact_root, snapshot, resource="configuration.json"
    )
    with pytest.raises(ReaderError, match="overlapping source authority"):
        read_snapshot(
            artifact_root,
            descriptor_digest=mutated_digest,
            expected_schema_digests=approved_schemas(),
        )


def test_canonical_logical_digest_preserves_types_scales_order_and_duplicates() -> None:
    columns = [
        {
            "name": "amount_coefficient",
            "kind": "string",
            "nullable": False,
            "text_semantics": "identifier",
        },
        {"name": "amount_scale", "kind": "integer", "nullable": False},
        {"name": "amount_source_scale", "kind": "integer", "nullable": True},
    ]
    rows = [{"amount_coefficient": "842", "amount_scale": 1, "amount_source_scale": 2}]
    baseline = logical_table_digest(columns, rows, version=1)
    changed = deepcopy(rows)
    changed[0]["amount_source_scale"] = 1
    assert logical_table_digest(columns, changed, version=1) != baseline
    assert logical_table_digest(columns, rows * 2, version=1) != baseline
    assert logical_table_digest(list(reversed(columns)), rows, version=1) != baseline
    assert (
        canonical_bytes({"integer": 3, "boolean": True})
        == b'{"boolean":true,"integer":"3"}\n'
    )
    with pytest.raises(ReaderError, match="floating-point"):
        canonical_bytes({"amount": 0.1})


@pytest.mark.parametrize(
    ("text", "row_bytes", "row_digest", "guard_digest"),
    [
        (
            "  Caf\u00e9\n  ",
            b'["  Caf\xc3\xa9\\n  "]\n',
            "65d3315a4cb988eb5333c3582674a0ce330bc69485edcb8f8b694b88788923fb",
            "f02d77aae6352be167dc3b9fb730dd810600995d142c36e32ad8f5c7a4889632",
        ),
        (
            "  Cafe\u0301\n  ",
            b'["  Cafe\xcc\x81\\n  "]\n',
            "6fec5046f0840affa01df4abbe7798609e4282fe81bd52a9c6c330b142df8181",
            "61d0a9308b4a226973a53be956b9a718c3981e0969607d541cd94e67eb22dc3c",
        ),
    ],
)
def test_exact_text_has_literal_canonical_row_and_guard_vectors(
    text: str, row_bytes: bytes, row_digest: str, guard_digest: str
) -> None:
    columns = [
        {
            "name": "description_as_stated",
            "kind": "string",
            "nullable": False,
            "text_semantics": "descriptive",
        }
    ]
    assert canonical_bytes([text]) == row_bytes
    assert (
        logical_table_digest(columns, [{"description_as_stated": text}], version=1)
        == row_digest
    )
    # Guard framing does not acquire the catalog-only column annotation.
    guard_header = {
        "schema_version": 1,
        "columns": [
            {"name": "description_as_stated", "kind": "string", "nullable": False}
        ],
    }
    assert canonical_bytes(guard_header) == (
        b'{"columns":[{"kind":"string","name":"description_as_stated",'
        b'"nullable":false}],"schema_version":"1"}\n'
    )
    assert hashlib.sha256(canonical_bytes(guard_header) + row_bytes).hexdigest() == (
        guard_digest
    )
    assert (
        _source_key_event_id(
            "entity", ["feed", "account-\u00e9", "FITID", "007"], "movement"
        )
        == "a630143025f422bf7de5c922f5118f450805f122f532df1bcc50deacb8c70d3e"
    )
    tables = source_key_support_tables()
    tables["transactions"][0]["description_as_stated"] = text
    _validate_event_support(tables)
    assert tables["transactions"][0]["txn_id"] == (
        "a630143025f422bf7de5c922f5118f450805f122f532df1bcc50deacb8c70d3e"
    )


@pytest.mark.parametrize(
    ("kind", "text", "error"),
    [
        ("string", "", "empty identifier"),
        ("list", "e\u0301", "non-NFC identifier"),
        ("string", "key\n", "control character"),
        ("list", "\ud800", "invalid Unicode"),
    ],
)
def test_identifier_columns_reject_without_repair(
    kind: str, text: str, error: str
) -> None:
    column = {
        "name": "key",
        "kind": kind,
        "nullable": False,
        "text_semantics": "identifier",
    }
    value = [text] if kind == "list" else text
    with pytest.raises(ReaderError, match=error):
        _validate_cell(column, value)
    assert value == ([text] if kind == "list" else text)


@pytest.mark.parametrize("kind", ["string", "list"])
def test_descriptive_columns_preserve_empty_and_control_text(kind: str) -> None:
    column = {
        "name": "text",
        "kind": kind,
        "nullable": False,
        "text_semantics": "descriptive",
    }
    for text in ("", "  e\u0301\n\u0000\u0085\u200d  "):
        value = [text] if kind == "list" else text
        _validate_cell(column, value)
        assert value == ([text] if kind == "list" else text)
    with pytest.raises(ReaderError, match="invalid Unicode"):
        _validate_cell(column, ["\udfff"] if kind == "list" else "\udfff")


@pytest.mark.parametrize("semantics", [None, "text"])
@pytest.mark.parametrize("empty", [False, True])
def test_missing_or_invalid_text_semantics_is_never_defaulted(
    semantics: object, *, empty: bool
) -> None:
    case = load_case("cash")
    schema = tabular_schema()
    table = next(table for table in schema["tables"] if table["name"] == "transactions")
    column = next(
        column
        for column in table["columns"]
        if column["name"] == "description_as_stated"
    )
    if semantics is None:
        column.pop("text_semantics")
    else:
        column["text_semantics"] = semantics
    if empty:
        case.tables["transactions"] = []
    with pytest.raises(ReaderError, match="missing or invalid text_semantics"):
        validate_rows(case.tables, schema)


def test_nontext_columns_reject_text_semantics() -> None:
    with pytest.raises(ReaderError, match="nontext column"):
        _validate_cell(
            {
                "name": "count",
                "kind": "integer",
                "nullable": False,
                "text_semantics": "descriptive",
            },
            1,
        )


@pytest.mark.parametrize("key", ["", "e\u0301", "key\n", "\ud800"])
def test_canonical_json_keys_are_identifiers_not_normalized(key: str) -> None:
    document = {key: "value"}
    with pytest.raises(ReaderError):
        canonical_bytes(document)
    assert tuple(document) == (key,)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"text":"\xff"}',
        b'{"text":"\xed\xa0\x80"}',
        b'{"text":"\\ud800"}',
        b'{"text":"\\udfff"}',
    ],
)
def test_json_rejects_invalid_utf8_and_surrogates(payload: bytes) -> None:
    with pytest.raises(ReaderError, match=r"invalid UTF-8|invalid Unicode"):
        _json(payload)


def test_retained_json_permits_descriptions_without_relaxing_reference_keys() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "identifier"},
            "description": {"type": "string", "text_semantics": "descriptive"},
        },
        "required": ["id", "description"],
        "additionalProperties": False,
    }
    description = "  Cafe\u0301\n\u0000  "
    validate_json({"id": "  authored-key  ", "description": description}, schema)
    assert canonical_bytes({"description": description}) == (
        b'{"description":"  Cafe\xcc\x81\\n\\u0000  "}\n'
    )
    for identifier in ("", "e\u0301", "key\n", "\ud800"):
        with pytest.raises(ReaderError):
            validate_json({"id": identifier, "description": description}, schema)


def test_exact_text_snapshot_retains_bytes_and_independent_financial_answers(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root, case_id="exact-text")
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    production = read_production_snapshot(artifact_root, digest)
    assert production.manifest["logical_content_digest"] == snapshot.financial_digest
    case = load_case("exact-text")
    debit = next(
        row
        for row in snapshot.tables["transactions"]
        if row["txn_id"] == case.aliases["debit"]
    )
    for field, expected in (
        ("description_as_stated", b"  Cafe\xcc\x81\nTwo notebooks\x00  "),
        ("payee", b"  Cafe\xcc\x81\nShop  "),
        ("narration", b"\tAme\xcc\x81lie\npaid\xc2\x85  "),
    ):
        assert debit[field].encode("utf-8") == expected
    assert [value.encode("utf-8") for value in debit["tags"]] == [
        b"",
        b"  e\xcc\x81\n",
        b"\xc3\xa9",
        b"\xe2\x80\x8d",
    ]
    account = next(
        row
        for row in snapshot.tables["accounts"]
        if row["account_id"] == case.aliases["A"]
    )
    assert (
        account["institution"].encode("utf-8") == b"  Cafe\xcc\x81\nInstitution\x00  "
    )
    declaration = next(
        row
        for row in snapshot.tables["declarations"]
        if row["declaration_id"] == account["declaration_id"]
    )
    assert _json(declaration["payload_json"].encode())["institution"].encode() == (
        b"  Cafe\xcc\x81\nInstitution\x00  "
    )
    entity = snapshot.tables["build"][0]["entity_id"]
    result = replay_rows(snapshot.tables, entity=entity)
    assert result.quantity_positions[(entity, case.aliases["cash-A"])] == 110
    assert case.expected_answers["cash_flow"] == {
        "external_credit": "20",
        "expense": "10",
    }
    for category, expected in (("income", -20), ("expense", 10)):
        assert (
            sum(
                Fraction(int(row["amount_coefficient"]), 10 ** row["amount_scale"])
                for row in snapshot.tables["postings"]
                if row["category_id"] == case.aliases[category]
            )
            == expected
        )
    _assert_snapshot_oracles(case, snapshot.tables, result, entity=entity)
    baseline = load_case("keyed-overlap")
    assert case.aliases["debit"] == baseline.aliases["debit"]
