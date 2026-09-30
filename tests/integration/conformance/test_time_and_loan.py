import json
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, date, datetime
from fractions import Fraction

import pytest
from tests.integration.conformance.corpus import FixtureCase, load_case

from jbt.contracts.catalog import catalog
from jbt.contracts.declarations import validate_declaration
from jbt.contracts.validation import ContractError, validate_tables
from jbt.domain.errors import TemporalError
from jbt.domain.time import (
    Precision,
    TimeObservation,
    ZoneDecision,
    ZoneResolution,
    resolve_instant,
)

pytestmark = [pytest.mark.integration, pytest.mark.golden]


def _number(row: dict, field: str) -> Fraction | None:
    coefficient = row[field + "_coefficient"]
    if coefficient is None:
        return None
    return Fraction(int(coefficient), 10 ** row[field + "_scale"])


def _time(case: FixtureCase, alias: str) -> TimeObservation:
    row = next(
        row
        for row in case.tables["event_times"]
        if row["record_id"] == case.aliases[alias] and row["date_role"] == "posted"
    )
    civil_date = row["timestamp_date"]
    local = row["timestamp_local"]
    zone = row["timestamp_zone"]
    offset = row["timestamp_offset_minutes"]
    fraction_digits = row["timestamp_fraction_digits"]
    precision = row["timestamp_precision"]
    assert isinstance(civil_date, str)
    assert isinstance(local, str)
    assert isinstance(zone, str)
    assert isinstance(offset, int)
    assert fraction_digits is None or isinstance(fraction_digits, int)
    assert isinstance(precision, str)
    return TimeObservation(
        date.fromisoformat(civil_date),
        local,
        offset,
        zone,
        Precision(precision),
        fraction_digits,
    )


def _pinned_resolution(
    case: FixtureCase, observation: TimeObservation
) -> ZoneResolution:
    spec = case.evidence["consumer"]["fold_resolution"]
    return ZoneResolution(
        observation=observation,
        tzdb_version=spec["tzdb_version"],
        candidate_offsets=tuple(spec["candidate_offsets"]),
        decision=ZoneDecision(spec["decision"]),
        offset_minutes=spec["offset_minutes"],
        shift_minutes=spec["shift_minutes"],
    )


def _utc_text(case: FixtureCase, alias: str) -> str:
    observation = _time(case, alias)
    resolution = (
        _pinned_resolution(case, observation) if alias == "fold-deposit" else None
    )
    instant = resolve_instant(observation, resolution=resolution)
    return datetime.fromtimestamp(instant.unix_seconds, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_time_and_loan_is_complete_valid_retained_corpus() -> None:
    case = load_case("time-and-loan")
    validate_tables(case.tables)
    for declaration in case.tables["declarations"]:
        payload_json = declaration["payload_json"]
        assert isinstance(payload_json, str)
        validate_declaration(json.loads(payload_json))
    assert set(case.tables) == {table.name for table in catalog()}
    assert len(case.domain_records) == 7
    assert {mutation["id"] for mutation in case.negative_cases} >= {
        "unresolved-fold",
        "conflicting-fold",
        "utc-period-substitution",
        "invent-loan-allocation",
    }


def test_ambiguous_fold_preserves_civil_observation_and_pinned_later_choice() -> None:
    case = load_case("time-and-loan")
    fold = _time(case, "fold-deposit")
    assert fold.local == "01:30"
    assert fold.zone == "America/New_York"
    assert fold.offset_minutes == -300
    choice = _pinned_resolution(case, fold)
    assert choice.tzdb_version == "2026a"
    assert choice.decision == ZoneDecision.LATER
    assert choice.candidate_offsets == (-240, -300)
    assert _utc_text(case, "fold-deposit") == case.expected_answers["fold_utc"]

    unresolved = TimeObservation(
        fold.date, fold.local, None, fold.zone, fold.precision, fold.fraction_digits
    )
    with pytest.raises(TemporalError, match="required_instant_unresolved"):
        resolve_instant(unresolved, resolution=None)
    conflicting = TimeObservation(
        fold.date, fold.local, -240, fold.zone, fold.precision, fold.fraction_digits
    )
    with pytest.raises(TemporalError, match="zone_offset_disagreement"):
        resolve_instant(conflicting, resolution=_pinned_resolution(case, conflicting))


def test_irregular_empty_period_and_closed_account_horizon_are_explicit() -> None:
    case = load_case("time-and-loan")
    account = next(
        row for row in case.tables["accounts"] if row["account_id"] == case.aliases["P"]
    )
    declaration = next(
        row for row in case.tables["declarations"] if row["declaration_key"] == "P"
    )
    payload_json = declaration["payload_json"]
    assert isinstance(payload_json, str)
    payload = json.loads(payload_json)
    periods = payload["expected_periods"]
    assert payload["period_rule"] == "explicit"
    assert payload["statement_cadence"] == "irregular"
    assert payload["civil_zone"] == "America/New_York"
    assert periods == [
        {"start": "2026-10-20", "end": "2026-11-01", "due": "2026-11-04"},
        {"start": "2026-11-02", "end": "2026-11-08", "due": "2026-11-10"},
    ]
    assert account["closed_date"] == case.expected_answers["closed_date"]
    assert periods[-1]["end"] == account["closed_date"]
    assert case.tables["build"][0]["as_of"] == case.expected_answers["as_of"]
    assert periods[-1]["due"] < case.tables["build"][0]["as_of"]
    assert (
        len(
            [
                row
                for row in case.tables["statements"]
                if row["account_id"] == case.aliases["P"]
            ]
        )
        == 2
    )

    empty = case.expected_answers["empty_period"]
    assert empty["movements"] == "0"
    notice = next(
        row
        for row in case.tables["source_records"]
        if row["source_record_id"] == case.aliases["source:empty-period-notice"]
    )
    assert notice["statement_id"] == case.aliases["irregular-empty"]
    assert not any(
        row["step_id"] == case.aliases["step:empty-period-notice"]
        for row in case.tables["book_steps"]
    )
    period_events = set()
    for row in case.tables["transactions"]:
        posted = row["date_posted"]
        if isinstance(posted, str) and empty["start"] <= posted <= empty["end"]:
            period_events.add(row["txn_id"])
    assert not any(
        row["position_id"] == case.aliases["cash-P"] and row["txn_id"] in period_events
        for row in case.tables["postings"]
    )


def test_local_end_of_period_is_not_classified_by_utc_date() -> None:
    case = load_case("time-and-loan")
    local = _time(case, "late-local-credit")
    assert local.date.isoformat() == case.expected_answers["first_period_end"]
    assert (
        _utc_text(case, "late-local-credit") == case.expected_answers["late_local_utc"]
    )
    assert local.date.isoformat() != _utc_text(case, "late-local-credit")[:10]
    statement = next(
        row
        for row in case.tables["statements"]
        if row["statement_id"] == case.aliases["irregular-first"]
    )
    assert isinstance(statement["period_start"], str)
    assert isinstance(statement["period_end"], str)
    assert (
        statement["period_start"] <= local.date.isoformat() <= statement["period_end"]
    )
    assert _utc_text(case, "late-local-credit")[:10] > statement["period_end"]


def test_known_loan_split_replays_but_unallocated_observation_does_not_book() -> None:
    case = load_case("time-and-loan")
    postings = defaultdict(list)
    for posting in case.tables["postings"]:
        postings[posting["txn_id"]].append(posting)
    payment = postings[case.aliases["mortgage-payment"]]
    components = {row["posting_id"]: _number(row, "amount") for row in payment}
    assert components[case.aliases["mortgage-cash"]] == -12
    assert components[case.aliases["mortgage-principal"]] == Fraction(
        case.expected_answers["known_principal_repaid"]
    )
    assert components[case.aliases["mortgage-interest"]] == Fraction(
        case.expected_answers["known_interest_expense"]
    )
    assert all(value is not None for value in components.values())
    assert sum(value for value in components.values() if value is not None) == 0

    pending = case.aliases["unallocated-loan-observation"]
    unallocated = postings[pending]
    assert len(unallocated) == 1
    assert _number(unallocated[0], "amount") == -Fraction(
        case.expected_answers["unallocated_gross"]
    )
    assert unallocated[0]["step_id"] is None
    assert case.expected_answers["unallocated_principal"] is None
    assert case.expected_answers["unallocated_interest"] is None
    assert not any(step["txn_id"] == pending for step in case.tables["book_steps"])
    recognized = [
        row
        for row in case.tables["postings"]
        if row["position_id"] == case.aliases["debt"] and row["step_id"] is not None
    ]
    assert sum(_number(row, "amount") or 0 for row in recognized) == 0


def test_irregular_periods_cannot_overlap_or_precede_due_date() -> None:
    case = load_case("time-and-loan")
    declaration = next(
        row for row in case.tables["declarations"] if row["declaration_key"] == "P"
    )
    payload_json = declaration["payload_json"]
    assert isinstance(payload_json, str)
    payload = json.loads(payload_json)
    overlapping = deepcopy(payload)
    overlapping["expected_periods"][1]["start"] = "2026-11-01"
    with pytest.raises(ContractError, match="period_overlap"):
        validate_declaration(overlapping)
    premature = deepcopy(payload)
    premature["expected_periods"][0]["due"] = "2026-10-31"
    with pytest.raises(ContractError, match="period_dates"):
        validate_declaration(premature)


def test_unallocated_payment_cannot_become_a_recognized_book_step() -> None:
    case = load_case("time-and-loan")
    tables = deepcopy(case.tables)
    fabricated = deepcopy(tables["book_steps"][0])
    fabricated["step_id"] = case.aliases["step:unallocated-loan-observation"]
    fabricated["txn_id"] = case.aliases["unallocated-loan-observation"]
    fabricated["sequence"] = len(tables["book_steps"])
    tables["book_steps"].append(fabricated)
    with pytest.raises(ContractError, match="recognized_steps"):
        validate_tables(tables)
