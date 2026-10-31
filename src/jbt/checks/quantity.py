"""Source equations never read model output; model equations read only tables."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind, equal, total

if TYPE_CHECKING:
    from datetime import date


class ScopeKind(StrEnum):
    """Distinguish position sets from account-level commodity aggregates."""

    POSITIONS = "positions"
    ACCOUNT_NET = "account_net"


class Measurement(StrEnum):
    """Separate stated quantities from monetary valuations."""

    UNITS = "units"
    VALUE = "value"


class AssertionKind(StrEnum):
    """Preserve the source point's declared comparison meaning."""

    POINT = "point"
    OPENING = "opening"
    CLOSING = "closing"


class CheckKind(StrEnum):
    """Independent check selectors; running a stage does not imply a pass."""

    SOURCE = "source_reconciliation"
    MODEL = "model_reconciliation"
    OCCURRENCE = "occurrence_accounting"
    CLASSIFICATION = "classification"
    COVERAGE = "coverage"
    TRANSFER = "transfer_conservation"
    LIFECYCLE = "account_lifecycle"


class CheckStatus(StrEnum):
    """Unavailable comparisons are distinct from matching or conflicting facts."""

    PASS = "pass"  # noqa: S105 # nosec B105
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True, slots=True)
class Check:
    """Exact expected and observed values with their independent evidence."""

    key: SemanticKey
    kind: CheckKind
    status: CheckStatus
    expected: ExactDecimal | int | str
    observed: ExactDecimal | int | str | None
    commodity: SemanticKey | None
    account: SemanticKey | None
    evidence: tuple[SemanticKey, ...]
    policies: tuple[SemanticKey, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class SourcePoint:
    """A dated balance point in one stream with explicit comparison meaning."""

    scope: SemanticKey
    section: SemanticKey
    symbol: str
    when: date
    amount: ExactDecimal
    evidence: SemanticKey
    account: SemanticKey = field(kw_only=True)
    basis: SemanticKey = field(kw_only=True)
    scope_kind: ScopeKind = field(kw_only=True)
    measurement: Measurement = field(kw_only=True)
    kind: AssertionKind = field(kw_only=True)
    source_position_key: SemanticKey | None = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class SourceMovement:
    """One retained extract's movement membership, never a merged download set."""

    when: date
    symbol: str
    amount: ExactDecimal
    evidence: SemanticKey
    recognized: bool
    source_position_key: SemanticKey | None = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class SourceInterval:
    """The half-open window over which this extract supplies movement evidence."""

    scope: SemanticKey
    section: SemanticKey
    start: date
    end: date
    evidence: SemanticKey
    movements: tuple[SourceMovement, ...]
    closing: tuple[SourcePoint, ...]
    account: SemanticKey = field(kw_only=True)


def _check(  # noqa: PLR0913 - check values and evidence remain explicit
    key: SemanticKey,
    kind: CheckKind,
    expected: ExactDecimal | int,
    observed: ExactDecimal | int | None,
    *,
    commodity: SemanticKey | None,
    account: SemanticKey | None,
    evidence: tuple[SemanticKey, ...],
) -> Check:
    matches = (
        equal(expected, observed)
        if isinstance(expected, ExactDecimal) and isinstance(observed, ExactDecimal)
        else expected == observed
    )
    return Check(
        key,
        kind,
        CheckStatus.NOT_EVALUABLE
        if observed is None
        else CheckStatus.PASS
        if matches
        else CheckStatus.FAIL,
        expected,
        observed,
        commodity,
        account,
        evidence,
    )


def source_checks(
    intervals: tuple[SourceInterval, ...],
    points: tuple[SourcePoint, ...],
    *,
    unavailable: tuple[SemanticKey, ...],
) -> tuple[Check, ...]:
    """Compare compatible points within one extract's covered movement span."""
    results = [
        _check(
            SemanticKey(f"source:{identifier.value}:unavailable"),
            CheckKind.SOURCE,
            1,
            None,
            commodity=None,
            account=None,
            evidence=(identifier,),
        )
        for identifier in unavailable
    ]
    for interval in intervals:
        if not interval.closing and interval.movements:
            results.append(
                _check(
                    SemanticKey(
                        f"source:{interval.evidence.value}:closing-unavailable"
                    ),
                    CheckKind.SOURCE,
                    1,
                    None,
                    commodity=None,
                    account=None,
                    evidence=(interval.evidence,),
                )
            )
        for closing in interval.closing:
            opening = _opening_points(interval, closing, points)
            if not opening and not interval.movements:
                continue
            amounts = {p.amount.rational() for p in opening}
            movements = tuple(
                m
                for m in interval.movements
                if m.recognized
                and m.symbol == closing.symbol
                and (
                    closing.scope_kind == ScopeKind.ACCOUNT_NET
                    or m.source_position_key == closing.source_position_key
                )
                and opening
                and opening[0].when <= m.when < closing.when
            )
            observed = (
                total((opening[0].amount, *(m.amount for m in movements)))
                if len(amounts) == 1
                else None
            )
            results.append(
                _check(
                    SemanticKey(
                        f"source:{interval.evidence.value}:"
                        f"{closing.evidence.value}:{closing.symbol}"
                    ),
                    CheckKind.SOURCE,
                    closing.amount,
                    observed,
                    commodity=None,
                    account=interval.account,
                    evidence=tuple(
                        sorted(
                            {
                                closing.evidence,
                                *(p.evidence for p in opening),
                                *(m.evidence for m in movements),
                            },
                            key=lambda k: k.value,
                        )
                    ),
                )
            )
    if not results:
        results.append(
            _check(
                SemanticKey("source:unavailable"),
                CheckKind.SOURCE,
                1,
                None,
                commodity=None,
                account=None,
                evidence=(),
            )
        )
    return tuple(results)


def _opening_points(
    interval: SourceInterval,
    closing: SourcePoint,
    points: tuple[SourcePoint, ...],
) -> tuple[SourcePoint, ...]:
    if (
        closing.scope != interval.scope
        or closing.section != interval.section
        or closing.account != interval.account
        or not interval.start < closing.when <= interval.end
        or closing.measurement != Measurement.UNITS
        or (
            closing.scope_kind == ScopeKind.POSITIONS
            and closing.source_position_key is None
        )
    ):
        return ()
    candidates = tuple(
        p
        for p in points
        if p.scope == closing.scope
        and p.section == closing.section
        and p.account == closing.account
        and p.symbol == closing.symbol
        and p.basis == closing.basis
        and p.kind == closing.kind
        and p.scope_kind == closing.scope_kind
        and p.measurement == closing.measurement
        and (
            closing.scope_kind == ScopeKind.ACCOUNT_NET
            or p.source_position_key == closing.source_position_key
        )
        and interval.start <= p.when < closing.when
    )
    latest = max((p.when for p in candidates), default=None)
    return tuple(p for p in candidates if p.when == latest)


@dataclass(frozen=True, slots=True)
class ProjectedPosition:
    """Published account and commodity identity used for scope selection."""

    identity: SemanticKey
    account: SemanticKey
    commodity: SemanticKey
    purpose: str = "general"


@dataclass(frozen=True, slots=True)
class ProjectedPosting:
    """Published signed units with recognition and effective posting date."""

    event: SemanticKey
    position: SemanticKey | None
    commodity: SemanticKey
    amount: ExactDecimal
    state: EventState
    kind: LegKind
    category: SemanticKey | None
    when: date | None
    identity: SemanticKey | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class ProjectedTransferMember:
    """A named participant, not an inferred balancing movement."""

    identity: SemanticKey
    role: str
    kind: str


@dataclass(frozen=True, slots=True)
class ProjectedTransfer:
    """One complete published relationship with its shared-field validity."""

    identity: SemanticKey
    members: tuple[ProjectedTransferMember, ...]
    valid_metadata: bool


@dataclass(frozen=True, slots=True)
class ProjectedBalance:
    """An asserted quantity, optionally targeting a particular position."""

    position: SemanticKey | None
    commodity: SemanticKey
    amount: ExactDecimal


@dataclass(frozen=True, slots=True)
class ProjectedAssertion:
    """Published assertion with explicit completeness and comparison scope."""

    identity: SemanticKey
    account: SemanticKey
    scope_kind: ScopeKind
    measurement: Measurement
    when: date
    complete: bool
    positions: tuple[ProjectedPosition, ...]
    balances: tuple[ProjectedBalance, ...]
    evaluable: bool


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    """Table-derived inputs; no model internals or source-equation results."""

    assertions: tuple[ProjectedAssertion, ...]
    postings: tuple[ProjectedPosting, ...]
    required_occurrences: tuple[SemanticKey, ...]
    supported_occurrences: tuple[SemanticKey, ...]
    positions: tuple[ProjectedPosition, ...] = ()
    transfers: tuple[ProjectedTransfer, ...] = ()


def transfer_checks(evidence: ModelEvidence) -> tuple[Check, ...]:
    """Prove complete links using only their named source position legs."""
    postings = {p.identity: p for p in evidence.postings if p.identity is not None}
    positions = {p.identity: p for p in evidence.positions}
    claims = Counter(
        m.identity for transfer in evidence.transfers for m in transfer.members
    )
    results = []
    for transfer in evidence.transfers:
        members = transfer.members
        selected = tuple(postings.get(m.identity) for m in members)
        commodities = {p.commodity for p in selected if p is not None}
        valid = (
            transfer.valid_metadata
            and {m.role for m in members} == {"outgoing", "incoming"}
            and len(commodities) == 1
            and all(m.kind == "posting" and claims[m.identity] == 1 for m in members)
        )
        for member, posting in zip(members, selected, strict=True):
            position = positions.get(posting.position) if posting else None
            valid = (
                valid
                and posting is not None
                and posting.kind == LegKind.POSITION
                and posting.state == EventState.RECOGNIZED
                and position is not None
                and position.purpose != "in_transit"
                and position.commodity == posting.commodity
                and (
                    int(posting.amount.coefficient) < 0
                    if member.role == "outgoing"
                    else int(posting.amount.coefficient) > 0
                )
            )
        results.append(
            _check(
                SemanticKey(f"transfer:{transfer.identity.value}"),
                CheckKind.TRANSFER,
                ExactDecimal("0", 0, None) if valid else 1,
                total(p.amount for p in selected if p is not None) if valid else 0,
                commodity=next(iter(commodities)) if len(commodities) == 1 else None,
                account=None,
                evidence=tuple(m.identity for m in members),
            )
        )
    return tuple(results)


def _assertion_checks(
    assertion: ProjectedAssertion, postings: tuple[ProjectedPosting, ...]
) -> tuple[Check, ...]:
    expected = {(b.position, b.commodity): b.amount for b in assertion.balances}
    if assertion.complete:
        for position in assertion.positions:
            key = (
                position.identity
                if assertion.scope_kind == ScopeKind.POSITIONS
                else None,
                position.commodity,
            )
            expected.setdefault(key, ExactDecimal("0", 0, None))
    if not expected:
        return (
            _check(
                SemanticKey(f"model:{assertion.identity.value}:empty"),
                CheckKind.MODEL,
                0,
                0
                if assertion.complete
                and assertion.evaluable
                and assertion.measurement == Measurement.UNITS
                else None,
                commodity=None,
                account=assertion.account,
                evidence=(),
            ),
        )
    results = []
    for (position, commodity), amount in expected.items():
        candidates = tuple(
            p.identity
            for p in assertion.positions
            if p.commodity == commodity and (position is None or p.identity == position)
        )
        movements = tuple(
            p
            for p in postings
            if p.kind == LegKind.POSITION
            and p.position in candidates
            and p.commodity == commodity
            and p.state == EventState.RECOGNIZED
        )
        evaluable = (
            assertion.evaluable
            and assertion.measurement == Measurement.UNITS
            and (assertion.scope_kind == ScopeKind.ACCOUNT_NET or len(candidates) == 1)
            and all(p.when is not None for p in movements)
        )
        observed = (
            total(
                p.amount
                for p in movements
                if p.when is not None and p.when < assertion.when
            )
            if evaluable
            else None
        )
        position_key = position.value if position is not None else "net"
        results.append(
            _check(
                SemanticKey(
                    f"model:{assertion.identity.value}:{position_key}:{commodity.value}"
                ),
                CheckKind.MODEL,
                amount,
                observed,
                commodity=commodity,
                account=assertion.account,
                evidence=(),
            )
        )
    return tuple(results)


def model_checks(
    evidence: ModelEvidence, selected: frozenset[CheckKind]
) -> tuple[Check, ...]:
    """Evaluate immutable facts decoded solely from published table grains."""
    results = list(transfer_checks(evidence))
    if CheckKind.MODEL in selected:
        results.extend(
            c
            for assertion in evidence.assertions
            for c in _assertion_checks(assertion, evidence.postings)
        )
        if not evidence.assertions:
            results.append(
                _check(
                    SemanticKey("model:unavailable"),
                    CheckKind.MODEL,
                    1,
                    None,
                    commodity=None,
                    account=None,
                    evidence=(),
                )
            )
    if CheckKind.OCCURRENCE in selected:
        required = set(evidence.required_occurrences)
        supported = Counter(evidence.supported_occurrences)
        observed = sum(supported[identifier] == 1 for identifier in required)
        unexpected = supported.keys() - required
        results.append(
            _check(
                SemanticKey("occurrences"),
                CheckKind.OCCURRENCE,
                len(required),
                observed if not unexpected else None,
                commodity=None,
                account=None,
                evidence=(),
            )
        )
    if CheckKind.CLASSIFICATION in selected:
        count = sum(
            p.kind == LegKind.BOUNDARY
            and p.category is None
            and p.state == EventState.RECOGNIZED
            for p in evidence.postings
        )
        results.append(
            _check(
                SemanticKey("classification"),
                CheckKind.CLASSIFICATION,
                0,
                count,
                commodity=None,
                account=None,
                evidence=(),
            )
        )
    return tuple(results)
