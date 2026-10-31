import json
from dataclasses import replace
from datetime import date

import pytest

from jbt.checks.coverage import (
    Cadence,
    Claim,
    ExpectedPeriod,
    Movement,
    Period,
    Policy,
    coverage_checks,
    gaps,
    lifecycle_checks,
)
from jbt.checks.quantity import CheckStatus
from jbt.domain.ids import SemanticKey
from jbt.domain.time import TimeRole

ACCOUNT = SemanticKey("card")
BASIS = SemanticKey("posted")


def details(value: object) -> dict:
    assert isinstance(value, str)
    return json.loads(value)


def period(start: str, end: str) -> Period:
    return Period(date.fromisoformat(start), date.fromisoformat(end))


def policy() -> Policy:
    return Policy(
        ACCOUNT,
        SemanticKey("revision"),
        None,
        None,
        date(2025, 1, 1),
        None,
        date(2026, 1, 1),
        Cadence.MONTHLY,
        ((BASIS, TimeRole.POSTED),),
        2,
        (),
        None,
        0,
    )


def claim(key: str, start: str, end: str) -> Claim:
    return Claim(SemanticKey(key), ACCOUNT, BASIS, TimeRole.POSTED, period(start, end))


def test_union_reports_exact_gaps_without_double_counting() -> None:
    assert gaps(
        period("2026-01-01", "2026-02-01"),
        (
            period("2025-12-01", "2026-01-03"),
            period("2026-01-02", "2026-01-10"),
            period("2026-01-11", "2026-01-20"),
            period("2026-01-18", "2026-01-30"),
        ),
    ) == (
        period("2026-01-10", "2026-01-11"),
        period("2026-01-30", "2026-02-01"),
    )


@pytest.mark.parametrize(
    ("cadence", "horizon", "expected"),
    [
        (
            Cadence.MONTHLY,
            "2026-03-03",
            [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01")],
        ),
        (Cadence.QUARTERLY, "2026-04-03", [("2026-01-01", "2026-04-01")]),
        (Cadence.ANNUAL, "2027-01-03", [("2026-01-01", "2027-01-01")]),
        (Cadence.IRREGULAR, "2026-03-03", [("2026-01-05", "2026-01-08")]),
    ],
)
def test_due_periods_use_civil_calendar_and_explicit_schedule(
    cadence: Cadence,
    horizon: str,
    expected: list[tuple[str, str]],
) -> None:
    current = replace(
        policy(),
        cadence=cadence,
        explicit=(
            ExpectedPeriod(period("2026-01-05", "2026-01-08"), date(2026, 1, 12)),
        ),
    )
    checks = coverage_checks((current,), (), horizon=date.fromisoformat(horizon))
    assert [details(check.expected)["period"] for check in checks] == [
        {"start": start, "end": end} for start, end in expected
    ]
    assert all(check.status == CheckStatus.FAIL for check in checks)


def test_adoption_lag_closure_and_revisions_do_not_create_lifetimes() -> None:
    first = replace(
        policy(), coverage_start=date(2026, 1, 10), valid_to=date(2026, 1, 16)
    )
    second = replace(
        first,
        declaration=SemanticKey("later"),
        valid_from=first.valid_to,
        valid_to=None,
        closed=date(2026, 1, 25),
        lag_days=3,
    )
    claims = (claim("empty-complete", "2026-01-10", "2026-01-26"),)
    checks = coverage_checks((first, second), claims, horizon=date(2026, 2, 3))
    assert [details(check.expected)["period"] for check in checks] == [
        {"start": "2026-01-10", "end": "2026-01-16"},
        {"start": "2026-01-16", "end": "2026-01-26"},
    ]
    assert all(check.status == CheckStatus.PASS for check in checks)
    assert (
        coverage_checks((second,), (), horizon=date(2026, 1, 27))[0].status
        == CheckStatus.PASS
    )
    assert (
        coverage_checks((second,), (), horizon=date(2026, 1, 28))[0].status
        == CheckStatus.FAIL
    )
    assert (
        lifecycle_checks(
            (first, second),
            (
                Movement(SemanticKey("before-adoption"), ACCOUNT, date(2025, 2, 1)),
                Movement(SemanticKey("revision-boundary"), ACCOUNT, date(2026, 1, 16)),
            ),
        )[0].status
        == CheckStatus.PASS
    )
    failures = lifecycle_checks(
        (first, second),
        (Movement(SemanticKey("closed"), ACCOUNT, date(2026, 1, 26)),),
    )
    assert failures[0].status == CheckStatus.FAIL


@pytest.mark.parametrize("end", ["2026-01-31", "2026-02-02"])
def test_cycle_early_or_late_closing_uses_actual_end_without_false_tail_gap(
    end: str,
) -> None:
    current = replace(
        policy(), cycle_end_day=31, cadence=Cadence.MONTHLY_CYCLE, tolerance_days=1
    )
    checks = coverage_checks(
        (current,),
        (claim("january", "2026-01-01", end),),
        horizon=date(2026, 2, 3),
    )
    assert len(checks) == 1
    assert checks[0].status == CheckStatus.PASS
    assert details(checks[0].observed)["gaps"] == []


def test_cycle_clamping_missing_duplicates_and_discontinuity() -> None:
    current = replace(
        policy(), cycle_end_day=31, cadence=Cadence.MONTHLY_CYCLE, tolerance_days=1
    )
    january = claim("january", "2026-01-01", "2026-01-31")
    february = claim("february", "2026-01-31", "2026-03-01")
    checks = coverage_checks((current,), (january, february), horizon=date(2026, 3, 3))
    assert all(check.status == CheckStatus.PASS for check in checks)
    missing = coverage_checks((current,), (january,), horizon=date(2026, 3, 3))
    assert missing[-1].status == CheckStatus.FAIL
    assert details(missing[-1].observed)["matching_statements"] == 0
    discontinuity = coverage_checks(
        (current,),
        (january, replace(february, period=period("2026-02-01", "2026-03-01"))),
        horizon=date(2026, 3, 3),
    )
    assert details(discontinuity[-1].observed)["gaps"] == [
        {"start": "2026-01-31", "end": "2026-02-01"},
    ]
    duplicate = coverage_checks(
        (current,),
        (january, february, replace(february, statement=SemanticKey("duplicate"))),
        horizon=date(2026, 3, 3),
    )
    assert duplicate[-1].status == CheckStatus.FAIL
    assert details(duplicate[-1].observed)["matching_statements"] == 2
    overlap = coverage_checks(
        (current,),
        (january, replace(february, period=period("2026-01-30", "2026-03-01"))),
        horizon=date(2026, 3, 3),
    )
    assert overlap[-1].status == CheckStatus.FAIL
    assert details(overlap[-1].observed)["reason"] == "cycle_overlap"


def test_cycle_revision_preserves_the_prior_fragment_and_its_deadline() -> None:
    first = replace(
        policy(),
        cadence=Cadence.MONTHLY_CYCLE,
        cycle_end_day=31,
        valid_to=date(2026, 1, 16),
        lag_days=0,
    )
    second = replace(
        first,
        declaration=SemanticKey("calendar"),
        valid_from=first.valid_to,
        valid_to=None,
        cadence=Cadence.MONTHLY,
        cycle_end_day=None,
    )
    later_claim = claim("later", "2026-01-16", "2026-02-01")
    assert coverage_checks((first, second), (later_claim,), horizon=date(2026, 1, 30))[
        0
    ].key == SemanticKey("coverage:no_due_periods")
    checks = coverage_checks((first, second), (later_claim,), horizon=date(2026, 1, 31))
    assert [check.status for check in checks] == [CheckStatus.FAIL, CheckStatus.PASS]
    assert details(checks[0].observed)["gaps"] == [
        {"start": "2026-01-01", "end": "2026-01-16"},
    ]
    assert all(
        check.status == CheckStatus.PASS
        for check in coverage_checks(
            (first, second),
            (claim("earlier", "2026-01-01", "2026-01-16"), later_claim),
            horizon=date(2026, 1, 31),
        )
    )


def test_adopted_terminal_cycle_requires_evidence_between_nominal_closes() -> None:
    current = replace(
        policy(),
        cadence=Cadence.MONTHLY_CYCLE,
        cycle_end_day=15,
        coverage_start=date(2026, 1, 20),
        closed=date(2026, 1, 25),
        tolerance_days=1,
    )
    assert coverage_checks((current,), (), horizon=date(2026, 1, 27))[0].key == (
        SemanticKey("coverage:no_due_periods")
    )
    (missing,) = coverage_checks((current,), (), horizon=date(2026, 1, 28))
    assert missing.status == CheckStatus.FAIL
    assert details(missing.observed)["gaps"] == [
        {"start": "2026-01-20", "end": "2026-01-26"},
    ]
    (covered,) = coverage_checks(
        (current,),
        (claim("final", "2026-01-20", "2026-01-26"),),
        horizon=date(2026, 1, 28),
    )
    assert covered.status == CheckStatus.PASS
