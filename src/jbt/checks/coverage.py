"""Civil-period expectations and lifecycle checks over projected evidence."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

from jbt.checks.quantity import Check, CheckKind, CheckStatus
from jbt.domain.canonical import encode_json
from jbt.domain.ids import SemanticKey

if TYPE_CHECKING:
    from jbt.domain.time import TimeRole

_DAY = timedelta(days=1)


class Cadence(StrEnum):
    """Supported civil schedules, independent of evidence arrival."""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"
    MONTHLY_CYCLE = "monthly_cycle"


@dataclass(frozen=True, slots=True, order=True)
class Period:
    """A nonempty half-open civil interval."""

    start: date
    end: date

    def __post_init__(self) -> None:
        """Reject empty or inverted expectations at the owning boundary."""
        if self.start >= self.end:
            message = "nonempty_coverage_period_required"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ExpectedPeriod:
    """One explicit interval and its earliest publication date."""

    period: Period
    due: date


@dataclass(frozen=True, slots=True)
class Policy:
    """One effective account revision, not the account's holding lifetime."""

    account: SemanticKey
    declaration: SemanticKey
    valid_from: date | None
    valid_to: date | None
    opened: date | None
    closed: date | None
    coverage_start: date | None
    cadence: Cadence | None
    bases: tuple[tuple[SemanticKey, TimeRole], ...]
    lag_days: int
    explicit: tuple[ExpectedPeriod, ...]
    cycle_end_day: int | None
    tolerance_days: int

    def applies(self, when: date) -> bool:
        """Resolve policy without confusing revision validity with lifecycle."""
        return (self.valid_from is None or self.valid_from <= when) and (
            self.valid_to is None or when < self.valid_to
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """A live completeness segment from one statement movement part."""

    statement: SemanticKey
    account: SemanticKey
    basis: SemanticKey
    role: TimeRole
    period: Period


@dataclass(frozen=True, slots=True)
class Movement:
    """A projected position movement, including its resolved civil date."""

    identity: SemanticKey
    account: SemanticKey
    when: date | None


def gaps(period: Period, coverage: tuple[Period, ...]) -> tuple[Period, ...]:
    """Subtract an interval union, preserving exact uncovered boundaries."""
    cursor = period.start
    missing = []
    for candidate in sorted(coverage):
        start, end = max(period.start, candidate.start), min(period.end, candidate.end)
        if end <= cursor or start >= period.end:
            continue
        if start > cursor:
            missing.append(Period(cursor, start))
        cursor = max(cursor, end)
    if cursor < period.end:
        missing.append(Period(cursor, period.end))
    return tuple(missing)


def _month(index: int, day: int) -> date:
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day, monthrange(year, month + 1)[1]))


def _calendar(policy: Policy, horizon: date) -> tuple[ExpectedPeriod, ...]:
    if policy.coverage_start is None:
        return ()
    start = max(policy.coverage_start, policy.valid_from or date.min)
    closed_end = policy.closed + _DAY if policy.closed is not None else date.max
    horizon_end = horizon + _DAY
    stop = min(horizon_end, closed_end, policy.valid_to or date.max)
    if start >= stop:
        return ()
    if policy.cadence == Cadence.IRREGULAR:
        candidates = policy.explicit
    else:
        if policy.cadence not in {Cadence.MONTHLY, Cadence.QUARTERLY, Cadence.ANNUAL}:
            message = "calendar_cadence_required"
            raise ValueError(message)
        step = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUAL: 12}[
            policy.cadence
        ]
        index = start.year * 12 + start.month - 1
        index -= index % step
        candidates = tuple(
            ExpectedPeriod(
                Period(_month(i, 1), _month(i + step, 1)),
                _month(i + step, 1) - _DAY + timedelta(days=policy.lag_days),
            )
            for i in range(index, stop.year * 12 + stop.month, step)
            if i + step < 10000 * 12
        )
    results = []
    for candidate in candidates:
        end = min(candidate.period.end, closed_end)
        left, right = max(start, candidate.period.start), min(stop, end)
        due = max(
            candidate.due if end == candidate.period.end else end - _DAY,
            end - _DAY + timedelta(days=policy.lag_days),
        )
        if left < right and end <= horizon_end and due <= horizon:
            results.append(ExpectedPeriod(Period(left, right), due))
    return tuple(results)


def _wire(periods: tuple[Period, ...]) -> list[dict[str, str]]:
    return [
        {"start": period.start.isoformat(), "end": period.end.isoformat()}
        for period in periods
    ]


def _text(value: object) -> str:
    return encode_json(value, integer_strings=False).decode().strip()


def _result(  # noqa: PLR0913 - exact expectation and cycle count are independent
    policy: Policy,
    basis: SemanticKey,
    period: Period,
    *,
    missing: tuple[Period, ...],
    reason: str,
    matches: int | None = None,
) -> Check:
    return Check(
        SemanticKey(
            f"coverage:{policy.account.value}:{basis.value}:"
            f"{period.start}:{period.end}:{policy.declaration.value}"
        ),
        CheckKind.COVERAGE,
        CheckStatus.PASS
        if not missing and matches in {None, 1} and reason != "cycle_overlap"
        else CheckStatus.FAIL,
        _text({"basis": basis.value, "period": _wire((period,))[0]}),
        _text(
            {"gaps": _wire(missing), "reason": reason, "matching_statements": matches}
        ),
        None,
        policy.account,
        (),
        policies=(policy.declaration,),
    )


def _statement_windows(claims: tuple[Claim, ...]) -> tuple[Period, ...]:
    by_statement: dict[SemanticKey, list[Period]] = {}
    for claim in claims:
        by_statement.setdefault(claim.statement, []).append(claim.period)
    windows = []
    for spans in by_statement.values():
        ordered = sorted(spans)
        window = Period(ordered[0].start, ordered[-1].end)
        if not gaps(window, tuple(ordered)):
            windows.append(window)
    return tuple(windows)


def _cycle_result(
    policy: Policy,
    basis: SemanticKey,
    period: Period,
    chain: Period,
    claims: tuple[Claim, ...],
) -> Check:
    windows = _statement_windows(claims)
    fragment = period.end < chain.end
    candidates = tuple(
        window
        for window in windows
        if window.end > period.start
        and abs(window.end - chain.end) <= timedelta(days=policy.tolerance_days)
    )
    latest = candidates[0].end if not fragment and len(candidates) == 1 else period.end
    continuity = gaps(
        Period(chain.start, latest), tuple(claim.period for claim in claims)
    )
    overlapping = sorted(
        window
        for window in windows
        if window.start < latest and window.end > chain.start
    )
    overlaps = any(left.end > right.start for left, right in pairwise(overlapping))
    return _result(
        policy,
        basis,
        period,
        missing=continuity,
        reason="cycle_overlap"
        if overlaps
        else "cycle_fragment"
        if fragment
        else "cycle"
        if len(candidates) == 1
        else "cycle_count",
        matches=None if fragment else len(candidates),
    )


def _cycles(
    policy: Policy, basis: SemanticKey, claims: tuple[Claim, ...], horizon: date
) -> tuple[Check, ...]:
    if policy.coverage_start is None or policy.cycle_end_day is None:
        message = "cycle_policy_required"
        raise ValueError(message)
    start = max(policy.coverage_start, policy.valid_from or date.min)
    stop = min(horizon, policy.closed or date.max, policy.valid_to or date.max)
    results = []
    for index in range(
        start.year * 12 + start.month - 1, stop.year * 12 + stop.month + 1
    ):
        nominal = _month(index, policy.cycle_end_day)
        end = min(nominal, policy.closed or date.max)
        left = max(start, _month(index - 1, policy.cycle_end_day) + _DAY)
        right = min(end + _DAY, policy.valid_to or date.max)
        due = end + timedelta(days=policy.tolerance_days + policy.lag_days)
        if left < right and due <= horizon:
            results.append(
                _cycle_result(
                    policy,
                    basis,
                    Period(left, right),
                    Period(start, nominal + _DAY),
                    claims,
                )
            )
    return tuple(results)


def coverage_checks(
    policies: tuple[Policy, ...], claims: tuple[Claim, ...], *, horizon: date
) -> tuple[Check, ...]:
    """Report each due expectation; missing evidence is an evaluable failure."""
    results = []
    for policy in policies:
        if policy.cadence is None:
            continue
        for basis, role in policy.bases:
            selected = tuple(
                claim
                for claim in claims
                if (claim.account, claim.basis, claim.role)
                == (policy.account, basis, role)
            )
            if policy.cadence == Cadence.MONTHLY_CYCLE:
                results.extend(_cycles(policy, basis, selected, horizon))
            else:
                results.extend(
                    _result(
                        policy,
                        basis,
                        expected.period,
                        missing=gaps(
                            expected.period, tuple(c.period for c in selected)
                        ),
                        reason="interval_union",
                    )
                    for expected in _calendar(policy, horizon)
                )
    return tuple(results) or (
        Check(
            SemanticKey("coverage:no_due_periods"),
            CheckKind.COVERAGE,
            CheckStatus.PASS,
            0,
            0,
            None,
            None,
            (),
            policies=tuple(policy.declaration for policy in policies),
        ),
    )


def lifecycle_checks(
    policies: tuple[Policy, ...], movements: tuple[Movement, ...]
) -> tuple[Check, ...]:
    """Never waive missing applicable policy or an out-of-lifetime movement."""
    failures = []
    for movement in movements:
        applicable = tuple(
            policy
            for policy in policies
            if policy.account == movement.account
            and movement.when is not None
            and policy.applies(movement.when)
        )
        if len(applicable) == 1:
            policy = applicable[0]
            if (
                movement.when is not None
                and (policy.opened is None or policy.opened <= movement.when)
                and (policy.closed is None or movement.when <= policy.closed)
            ):
                continue
        failures.append(
            Check(
                SemanticKey(f"lifecycle:{movement.identity.value}"),
                CheckKind.LIFECYCLE,
                CheckStatus.FAIL
                if movement.when is not None
                else CheckStatus.NOT_EVALUABLE,
                "movement within the applicable account open interval",
                movement.when.isoformat()
                if movement.when is not None
                else "unknown_date",
                None,
                movement.account,
                (),
                policies=tuple(
                    policy.declaration
                    for policy in policies
                    if policy.account == movement.account
                ),
            )
        )
    return tuple(failures) or (
        Check(
            SemanticKey("lifecycle"),
            CheckKind.LIFECYCLE,
            CheckStatus.PASS,
            0,
            0,
            None,
            None,
            (),
            policies=tuple(policy.declaration for policy in policies),
        ),
    )
