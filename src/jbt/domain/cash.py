"""Quantity-only cash determinations with independent evidence checks."""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from jbt.domain.identity import (
    AcquiredAnchor,
    AuthoredAnchor,
    Occurrence,
    event_id,
)
from jbt.domain.ids import EntityId, RecordId, SemanticKey
from jbt.domain.numbers import BoundedRational, ExactDecimal


class CashCode(StrEnum):
    """Closed rejection reasons for the quantity-only processor."""

    BOUNDARY = "statement_boundary"
    OVERLAP = "overlapping_sources_unsupported"
    OUTSIDE = "movement_outside_interval"
    DUPLICATE = "duplicate_occurrence"
    TREATMENT = "external_treatment_required"


class CashError(ValueError):
    """A supported-input or determination failure without source contents."""

    def __init__(self, code: CashCode, location: str) -> None:
        """Keep the failure code separate from safe source references."""
        self.code = code
        self.location = location
        super().__init__(f"{code}: {location}")


class MatchField(StrEnum):
    """The source fields supported by the initial equality rule processor."""

    NAME = "transactions.payee"
    MEMO = "transactions.narration"


@dataclass(frozen=True, slots=True)
class Predicate:
    """An exact comparison against a retained source field."""

    field: MatchField
    value: str


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """An ordered declaration assigning external boundary treatment."""

    declaration_id: str
    predicates: tuple[Predicate, ...]
    category_id: str


@dataclass(frozen=True, slots=True)
class CashObservation:
    """A posted source movement before classification or balancing."""

    anchor: AcquiredAnchor
    external_id: str
    posted: date
    amount: ExactDecimal
    transaction_type: str
    symbol_as_stated: str | None
    name: str | None
    memo: str | None

    def matches(self, predicate: Predicate) -> bool:
        """Compare the stated value, without trimming or case folding."""
        return {
            MatchField.NAME: self.name,
            MatchField.MEMO: self.memo,
        }[predicate.field] == predicate.value


@dataclass(frozen=True, slots=True)
class CashStatement:
    """A half-open civil interval with evidence at its exclusive end."""

    source_id: str
    start: date
    end: date
    balance_date: date
    balance: ExactDecimal
    complete: bool
    movements: tuple[CashObservation, ...]


@dataclass(frozen=True, slots=True)
class Opening:
    """Authored initialization and its separately retained balance evidence."""

    declaration_key: str
    declaration_id: str
    event_key: str
    posted: date
    amount: ExactDecimal
    category_id: str
    evidence_id: str
    evidence_date: date
    evidence_amount: ExactDecimal


@dataclass(frozen=True, slots=True)
class CashEvent:
    """One recognized event with an explicit balancing category."""

    identity: RecordId
    posted: date
    amount: ExactDecimal
    category_id: str
    policy_id: str
    observation: CashObservation | None


class CheckStatus(StrEnum):
    """Financial check outcomes; missing evidence is not success."""

    PASS = "pass"  # noqa: S105 # nosec B105 - financial check status
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True, slots=True)
class CashCheck:
    """Expected and observed are exact values or an occurrence count."""

    key: str
    kind: str
    status: CheckStatus
    expected: ExactDecimal | int
    observed: ExactDecimal | int | None
    evidence_ids: tuple[str, ...]


def total(values: tuple[ExactDecimal, ...]) -> ExactDecimal:
    """Accumulate with the shared bounded arithmetic, without rounding."""
    result = BoundedRational(0, 1, 0)
    for value in values:
        result = result.add(value.rational())
    return result.exact_decimal()


def _same(left: ExactDecimal, right: ExactDecimal) -> bool:
    return left.rational().subtract(right.rational()).numerator == 0


def _value_check(
    key: str,
    kind: str,
    expected: ExactDecimal,
    observed: ExactDecimal | None,
    evidence: tuple[str, ...],
) -> CashCheck:
    status = (
        CheckStatus.NOT_EVALUABLE
        if observed is None
        else CheckStatus.PASS
        if _same(expected, observed)
        else CheckStatus.FAIL
    )
    return CashCheck(key, kind, status, expected, observed, evidence)


def source_checks(
    opening: Opening, statements: tuple[CashStatement, ...]
) -> tuple[CashCheck, ...]:
    """Reconcile source movements without reading model determinations."""
    previous_date = opening.evidence_date
    previous_amount = opening.evidence_amount
    previous_id = opening.evidence_id
    results = []
    for statement in sorted(statements, key=lambda item: item.start):
        contiguous = previous_date == statement.start
        observed = (
            total((previous_amount, *(m.amount for m in statement.movements)))
            if contiguous
            and statement.balance_date == statement.end
            and all(
                statement.start < movement.posted < statement.end
                for movement in statement.movements
            )
            else None
        )
        results.append(
            _value_check(
                f"source:{statement.source_id}",
                "source_reconciliation",
                statement.balance,
                observed,
                (previous_id, statement.source_id),
            )
        )
        previous_date, previous_amount = statement.balance_date, statement.balance
        previous_id = statement.source_id
    return tuple(results)


def resolve_cash(
    entity: EntityId,
    opening: Opening,
    statements: tuple[CashStatement, ...],
    rules: tuple[CategoryRule, ...],
) -> tuple[CashEvent, ...]:
    """Resolve singleton anchors and explicitly classified external cash."""
    events = [
        CashEvent(
            event_id(
                entity,
                Occurrence(
                    AuthoredAnchor(SemanticKey(opening.declaration_key)),
                    SemanticKey(opening.event_key),
                ),
            ),
            opening.posted,
            opening.amount,
            opening.category_id,
            opening.declaration_id,
            None,
        )
    ]
    seen: set[AcquiredAnchor] = set()
    previous_end: date | None = None
    for statement in sorted(statements, key=lambda item: item.start):
        if statement.start >= statement.end or statement.balance_date != statement.end:
            raise CashError(CashCode.BOUNDARY, statement.source_id)
        if previous_end is not None and previous_end > statement.start:
            raise CashError(CashCode.OVERLAP, statement.source_id)
        previous_end = statement.end
        for observation in statement.movements:
            if not statement.start < observation.posted < statement.end:
                raise CashError(CashCode.OUTSIDE, statement.source_id)
            if observation.anchor in seen:
                raise CashError(CashCode.DUPLICATE, statement.source_id)
            seen.add(observation.anchor)
            matches = tuple(
                rule
                for rule in rules
                if all(observation.matches(p) for p in rule.predicates)
            )
            if not matches:
                raise CashError(CashCode.TREATMENT, statement.source_id)
            rule = matches[0]
            events.append(
                CashEvent(
                    event_id(
                        entity,
                        Occurrence(observation.anchor, SemanticKey("transaction")),
                    ),
                    observation.posted,
                    observation.amount,
                    rule.category_id,
                    rule.declaration_id,
                    observation,
                )
            )
    return tuple(
        sorted(
            events,
            key=lambda event: (
                event.posted,
                event.observation is not None,
                event.identity.value,
            ),
        )
    )


def model_checks(
    opening: Opening,
    statements: tuple[CashStatement, ...],
    events: tuple[CashEvent, ...],
    *,
    expected_periods: tuple[tuple[date, date], ...],
    as_of: date,
) -> tuple[CashCheck, ...]:
    """Check resolved movements against unchanged assertions and occurrences."""
    results = [
        _value_check(
            "opening",
            "model_reconciliation",
            opening.evidence_amount,
            total(tuple(e.amount for e in events if e.observation is None)),
            (opening.evidence_id,),
        )
    ]
    for statement in statements:
        observed = total(tuple(e.amount for e in events if e.posted < statement.end))
        results.append(
            _value_check(
                f"model:{statement.source_id}",
                "model_reconciliation",
                statement.balance,
                observed,
                (statement.source_id,),
            )
        )
    expected = tuple(m.anchor for s in statements for m in s.movements)
    observed = tuple(e.observation.anchor for e in events if e.observation is not None)
    exact_occurrences = (
        len(set(observed)) == len(observed)
        and set(observed) == set(expected)
        and sum(e.observation is None for e in events) == 1
    )
    results.append(
        CashCheck(
            "occurrences",
            "occurrence_accounting",
            CheckStatus.PASS if exact_occurrences else CheckStatus.FAIL,
            len(expected),
            len(observed),
            tuple(s.source_id for s in statements),
        )
    )
    qualifying = {
        (s.start, s.end - timedelta(days=1)) for s in statements if s.complete
    }
    due = {period for period in expected_periods if period[1] <= as_of}
    covered = due <= qualifying
    results.append(
        CashCheck(
            "coverage",
            "coverage",
            CheckStatus.PASS if covered else CheckStatus.NOT_EVALUABLE,
            len(due),
            len(due & qualifying),
            tuple(s.source_id for s in statements),
        )
    )
    return tuple(results)
