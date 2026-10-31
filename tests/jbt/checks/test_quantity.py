from dataclasses import replace
from datetime import date

import pytest

from jbt.checks.quantity import (
    AssertionKind,
    CheckKind,
    CheckStatus,
    Measurement,
    ModelEvidence,
    ProjectedAssertion,
    ProjectedBalance,
    ProjectedPosition,
    ProjectedPosting,
    ScopeKind,
    SourceInterval,
    SourceMovement,
    SourcePoint,
    model_checks,
    source_checks,
)
from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind


@pytest.mark.parametrize(
    ("posting_position", "status"),
    [
        ("cash", CheckStatus.NOT_EVALUABLE),
        ("other-account", CheckStatus.PASS),
    ],
)
def test_model_equation_cannot_ignore_an_undated_movement_in_its_scope(
    posting_position: str, status: CheckStatus
) -> None:
    account, commodity = SemanticKey("checking"), SemanticKey("USD")
    position = ProjectedPosition(SemanticKey("cash"), account, commodity)
    other_position = ProjectedPosition(
        SemanticKey("other-account"), SemanticKey("savings"), commodity
    )
    assertion = ProjectedAssertion(
        SemanticKey("closing"),
        account,
        ScopeKind.ACCOUNT_NET,
        Measurement.UNITS,
        date(2026, 2, 1),
        complete=True,
        positions=(position,),
        balances=(ProjectedBalance(None, commodity, ExactDecimal.parse("0")),),
        evaluable=True,
    )
    posting = ProjectedPosting(
        SemanticKey("undated"),
        SemanticKey(posting_position),
        commodity,
        ExactDecimal.parse("5"),
        EventState.RECOGNIZED,
        LegKind.POSITION,
        None,
        None,
    )
    (check,) = model_checks(
        ModelEvidence((assertion,), (posting,), (), (), (position, other_position)),
        frozenset({CheckKind.MODEL}),
    )
    assert check.status == status
    assert check.observed == (
        None if status == CheckStatus.NOT_EVALUABLE else ExactDecimal("0", 0, None)
    )


@pytest.mark.parametrize(
    ("measurement", "status"),
    [
        (Measurement.UNITS, CheckStatus.PASS),
        (Measurement.VALUE, CheckStatus.NOT_EVALUABLE),
    ],
)
def test_empty_complete_assertions_do_not_invent_valuation_support(
    measurement: Measurement, status: CheckStatus
) -> None:
    assertion = ProjectedAssertion(
        SemanticKey("empty"),
        SemanticKey("checking"),
        ScopeKind.ACCOUNT_NET,
        measurement,
        date(2026, 2, 1),
        complete=True,
        positions=(),
        balances=(),
        evaluable=True,
    )
    (check,) = model_checks(
        ModelEvidence((assertion,), (), (), ()), frozenset({CheckKind.MODEL})
    )
    assert check.status == status
    assert check.observed == (0 if measurement == Measurement.UNITS else None)


@pytest.mark.parametrize(
    ("supported", "status", "observed"),
    [
        (("first", "second"), CheckStatus.PASS, 2),
        (("first",), CheckStatus.FAIL, 1),
        (("first", "second", "first"), CheckStatus.FAIL, 1),
        (("first", "second", "unexpected"), CheckStatus.NOT_EVALUABLE, None),
    ],
)
def test_occurrence_accounting_requires_exactly_one_support_per_occurrence(
    supported: tuple[str, ...], status: CheckStatus, observed: int | None
) -> None:
    evidence = ModelEvidence(
        (),
        (),
        (SemanticKey("first"), SemanticKey("second")),
        tuple(SemanticKey(key) for key in supported),
    )
    (check,) = model_checks(evidence, frozenset({CheckKind.OCCURRENCE}))
    assert (check.status, check.expected, check.observed) == (status, 2, observed)


def test_occurrence_accounting_does_not_rescan_support_for_each_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = tuple(SemanticKey(f"occurrence-{index}") for index in range(128))
    comparisons = 0
    original_equal = SemanticKey.__eq__

    def counted_equal(left: SemanticKey, right: object) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original_equal(left, right)

    monkeypatch.setattr(SemanticKey, "__eq__", counted_equal)
    evidence = ModelEvidence((), (), keys, keys)
    (check,) = model_checks(evidence, frozenset({CheckKind.OCCURRENCE}))
    assert check.status == CheckStatus.PASS
    assert comparisons <= 4 * len(keys)


def test_each_source_equation_uses_only_its_own_movement_membership() -> None:
    scope, section = SemanticKey("feed"), SemanticKey("account")
    start, end = date(2026, 1, 1), date(2026, 2, 1)
    opening = SourcePoint(
        scope,
        section,
        "USD",
        start,
        ExactDecimal.parse("100"),
        SemanticKey("prior"),
        account=SemanticKey("checking"),
        basis=SemanticKey("ledger"),
        scope_kind=ScopeKind.ACCOUNT_NET,
        measurement=Measurement.UNITS,
        kind=AssertionKind.POINT,
        source_position_key=None,
    )
    closing = SourcePoint(
        scope,
        section,
        "USD",
        end,
        ExactDecimal.parse("90"),
        SemanticKey("closing"),
        account=SemanticKey("checking"),
        basis=SemanticKey("ledger"),
        scope_kind=ScopeKind.ACCOUNT_NET,
        measurement=Measurement.UNITS,
        kind=AssertionKind.POINT,
        source_position_key=None,
    )
    movements = tuple(
        SourceMovement(
            date(2026, 1, 10),
            "USD",
            ExactDecimal.parse("-5"),
            SemanticKey(key),
            recognized=True,
            source_position_key=None,
        )
        for key in ("A", "B")
    )
    intervals = tuple(
        SourceInterval(
            scope,
            section,
            start,
            end,
            SemanticKey(key),
            movements,
            (closing,),
            account=SemanticKey("checking"),
        )
        for key in ("first", "second")
    )
    assert [
        c.observed for c in source_checks(intervals, (opening, closing), unavailable=())
    ] == [ExactDecimal("90", 0, None), ExactDecimal("90", 0, None)]
    assert all(
        c.status == CheckStatus.PASS
        for c in source_checks(intervals, (opening, closing), unavailable=())
    )
    assert all(
        c.status == CheckStatus.NOT_EVALUABLE
        for c in source_checks(intervals, (closing,), unavailable=())
    )


@pytest.mark.parametrize(
    "difference", ["scope", "section", "basis", "kind", "scope_kind", "measurement"]
)
def test_other_stream_or_assertion_meaning_cannot_supply_opening(
    difference: str,
) -> None:
    opening = SourcePoint(
        SemanticKey("feed"),
        SemanticKey("section"),
        "USD",
        date(2026, 1, 1),
        ExactDecimal.parse("100"),
        SemanticKey("opening"),
        account=SemanticKey("checking"),
        basis=SemanticKey("ledger"),
        scope_kind=ScopeKind.ACCOUNT_NET,
        measurement=Measurement.UNITS,
        kind=AssertionKind.POINT,
        source_position_key=None,
    )
    closing = replace(
        opening,
        when=date(2026, 2, 1),
        amount=ExactDecimal.parse("95"),
        evidence=SemanticKey("closing"),
    )
    replacements = {
        "scope": replace(opening, scope=SemanticKey("other-feed")),
        "section": replace(opening, section=SemanticKey("other-section")),
        "basis": replace(opening, basis=SemanticKey("available")),
        "kind": replace(opening, kind=AssertionKind.CLOSING),
        "scope_kind": replace(opening, scope_kind=ScopeKind.POSITIONS),
        "measurement": replace(opening, measurement=Measurement.VALUE),
    }
    interval = SourceInterval(
        opening.scope,
        opening.section,
        opening.when,
        closing.when,
        SemanticKey("extract"),
        (
            SourceMovement(
                date(2026, 1, 10),
                "USD",
                ExactDecimal.parse("-5"),
                SemanticKey("movement"),
                recognized=True,
                source_position_key=None,
            ),
        ),
        (closing,),
        account=opening.account,
    )
    (check,) = source_checks(
        (interval,), (replacements[difference], closing), unavailable=()
    )
    assert check.status == CheckStatus.NOT_EVALUABLE


def test_extract_can_cover_points_strictly_inside_its_download_window() -> None:
    opening = SourcePoint(
        SemanticKey("feed"),
        SemanticKey("section"),
        "USD",
        date(2026, 1, 10),
        ExactDecimal.parse("100"),
        SemanticKey("opening"),
        account=SemanticKey("checking"),
        basis=SemanticKey("ledger"),
        scope_kind=ScopeKind.ACCOUNT_NET,
        measurement=Measurement.UNITS,
        kind=AssertionKind.POINT,
        source_position_key=None,
    )
    closing = replace(
        opening,
        when=date(2026, 1, 20),
        amount=ExactDecimal.parse("95"),
        evidence=SemanticKey("closing"),
    )
    movements = tuple(
        SourceMovement(
            when,
            "USD",
            ExactDecimal.parse(amount),
            SemanticKey(key),
            recognized=True,
            source_position_key=None,
        )
        for key, when, amount in (
            ("before", date(2026, 1, 9), "80"),
            ("at-opening", date(2026, 1, 10), "-3"),
            ("inside", date(2026, 1, 19), "-2"),
            ("at-closing", date(2026, 1, 20), "-8"),
        )
    )
    interval = SourceInterval(
        opening.scope,
        opening.section,
        date(2026, 1, 1),
        date(2026, 2, 1),
        SemanticKey("extract"),
        movements,
        (closing,),
        account=opening.account,
    )
    (check,) = source_checks((interval,), (opening, closing), unavailable=())
    assert check.status == CheckStatus.PASS
    assert check.observed == ExactDecimal("95", 0, None)
    assert {e.value for e in check.evidence} == {
        "opening",
        "closing",
        "at-opening",
        "inside",
    }


def test_source_position_points_do_not_net_other_same_commodity_positions() -> None:
    opening = SourcePoint(
        SemanticKey("feed"),
        SemanticKey("section"),
        "USD",
        date(2026, 1, 1),
        ExactDecimal.parse("100"),
        SemanticKey("opening"),
        account=SemanticKey("checking"),
        basis=SemanticKey("ledger"),
        scope_kind=ScopeKind.POSITIONS,
        measurement=Measurement.UNITS,
        kind=AssertionKind.POINT,
        source_position_key=SemanticKey("cash"),
    )
    closing = replace(
        opening,
        when=date(2026, 2, 1),
        amount=ExactDecimal.parse("95"),
        evidence=SemanticKey("closing"),
    )
    movements = tuple(
        SourceMovement(
            date(2026, 1, 10),
            "USD",
            ExactDecimal.parse(amount),
            SemanticKey(key),
            recognized=True,
            source_position_key=SemanticKey(key),
        )
        for key, amount in (("cash", "-5"), ("claim", "-100"))
    )
    interval = SourceInterval(
        opening.scope,
        opening.section,
        opening.when,
        closing.when,
        SemanticKey("extract"),
        movements,
        (closing,),
        account=opening.account,
    )
    (check,) = source_checks((interval,), (opening, closing), unavailable=())
    assert check.status == CheckStatus.PASS
    assert check.observed == ExactDecimal("95", 0, None)
    wrong_position = replace(opening, source_position_key=SemanticKey("claim"))
    (unavailable,) = source_checks(
        (interval,), (wrong_position, closing), unavailable=()
    )
    assert unavailable.status == CheckStatus.NOT_EVALUABLE
