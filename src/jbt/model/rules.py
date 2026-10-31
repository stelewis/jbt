"""One complete pre-enrichment rule fold and its explanation evidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from jbt.domain.decisions import (
    EventReviewAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    LegCategoryAssignment,
)
from jbt.domain.identity import child_id
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.quantity import LegKind
from jbt.domain.rules import MatchValue, Predicate, RuleField, matches

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jbt.model.quantity import Event, Inputs, Leg, Movement, Resolution, Validity

type RuleAssignment = (
    EventTextAssignment
    | EventTagsAssignment
    | EventReviewAssignment
    | LegCategoryAssignment
)

MATCH_FIELDS = frozenset(
    {
        "transactions.payee",
        "transactions.narration",
        "transactions.description_as_stated",
        "transactions.event_kind",
        "transactions.event_state",
        "transactions.review_state",
        "transactions.tags",
        "postings.amount",
        "postings.commodity_id",
        "postings.account_id",
        "postings.category_id",
        *(
            f"transactions.date_{role}"
            for role in ("authorized", "traded", "posted", "settled", "value")
        ),
        *(
            f"postings.date_{role}"
            for role in ("authorized", "traded", "posted", "settled", "value")
        ),
    }
)


@dataclass(frozen=True, slots=True)
class Rule:
    """A declaration evaluated in order against every balancing leg."""

    key: SemanticKey
    declaration: RecordId
    order: int
    validity: Validity
    predicates: tuple[Predicate, ...]
    assignments: tuple[RuleAssignment, ...]

    def __post_init__(self) -> None:
        """Require immutable, decoded predicates and permitted assignments."""
        if (
            not isinstance(self.key, SemanticKey)
            or not isinstance(self.declaration, RecordId)
            or type(self.order) is not int
            or self.order < 0
            or type(self.predicates) is not tuple
            or not all(
                isinstance(predicate, Predicate) for predicate in self.predicates
            )
            or type(self.assignments) is not tuple
            or not all(
                isinstance(
                    assignment,
                    (
                        EventTextAssignment,
                        EventTagsAssignment,
                        EventReviewAssignment,
                        LegCategoryAssignment,
                    ),
                )
                for assignment in self.assignments
            )
        ):
            message = "immutable_typed_rule_required"
            raise TypeError(message)
        fields = [assignment_value(assignment)[0] for assignment in self.assignments]
        if len(fields) != len(set(fields)):
            message = "unique_rule_assignment_fields_required"
            raise ValueError(message)


class AssignmentOutcome(StrEnum):
    """The contribution of one matching assignment."""

    APPLIED = "applied"
    SHADOWED = "shadowed"
    REDUNDANT = "redundant"


@dataclass(frozen=True, slots=True)
class PredicateResult:
    """The exact operand, observed fact and comparison used by the fold."""

    predicate: Predicate
    observed: MatchValue
    matches: bool


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    """A competing assignment and whether it contributes to the result."""

    field: RuleField
    value: MatchValue
    outcome: AssignmentOutcome
    selected: bool


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One rule at one native balancing-leg evaluation unit."""

    event: RecordId
    leg: RecordId
    rule: Rule
    eligible: bool
    predicates: tuple[PredicateResult, ...]
    assignments: tuple[AssignmentResult, ...]

    @property
    def matched(self) -> bool:
        """Eligibility and every predicate must hold."""
        return self.eligible and all(result.matches for result in self.predicates)


def assignment_value(assignment: RuleAssignment) -> tuple[RuleField, MatchValue]:
    """Use the same typed assignments as reviewed corrections."""
    match assignment:
        case EventTextAssignment():
            return RuleField(f"transactions.{assignment.field.value}"), assignment.value
        case EventTagsAssignment():
            return RuleField.TAGS, assignment.tags
        case EventReviewAssignment():
            return RuleField.REVIEW, assignment.state.value
        case LegCategoryAssignment():
            return RuleField.CATEGORY, assignment.category


def _view(
    movements: Mapping[RecordId, Movement], event: Event, leg: Leg
) -> dict[SemanticKey, MatchValue]:
    # Quantity imports the fold; these helpers are needed only at evaluation.
    from jbt.model.quantity import DATE_ROLES, event_dates, leg_dates  # noqa: PLC0415

    selected = next(
        (
            support.source_record
            for support in event.support
            if support.kind.value == "selected"
        ),
        None,
    )
    source = movements.get(selected) if selected is not None else None
    accounts = {item.account for item in event.legs if item.account is not None}
    fields: dict[str, MatchValue] = {
        "transactions.payee": event.payee,
        "transactions.narration": event.narration,
        "transactions.description_as_stated": event.description,
        "transactions.event_kind": SemanticKey(event.kind.value),
        "transactions.event_state": SemanticKey(event.state.value),
        "transactions.review_state": SemanticKey(event.review.value),
        "transactions.tags": event.tags,
        "postings.amount": leg.amount,
        "postings.commodity_id": leg.commodity,
        "postings.account_id": next(iter(accounts)) if len(accounts) == 1 else None,
        "postings.category_id": leg.category,
    }
    dates = event_dates(event)
    for role, event_date, leg_date in zip(
        DATE_ROLES, dates, leg_dates(leg), strict=True
    ):
        fields[f"transactions.date_{role}"] = event_date
        fields[f"postings.date_{role}"] = (
            event_date if leg_date.mode.value == "inherit" else leg_date.value
        )
    return {
        **{SemanticKey(field): value for field, value in fields.items()},
        **(dict(source.observations) if source is not None else {}),
    }


def _current(event: Event, leg: Leg, field: RuleField) -> MatchValue:
    match field:
        case RuleField.CATEGORY:
            return leg.category
        case RuleField.PAYEE:
            return event.payee
        case RuleField.NARRATION:
            return event.narration
        case RuleField.REVIEW:
            return event.review.value
        case RuleField.TAGS:
            return event.tags


def _assign(
    event: Event, leg: Leg, assignment: RuleAssignment, revision: RecordId
) -> tuple[Event, Leg]:
    field, value = assignment_value(assignment)
    if isinstance(assignment, LegCategoryAssignment):
        return event, replace(leg, category=assignment.category, rules=(revision,))
    policies = dict(event.rules)
    policies[field] = (*policies.get(field, ()), revision)
    match assignment:
        case EventTextAssignment():
            event = replace(event, **{assignment.field.value: value})
        case EventReviewAssignment():
            event = replace(event, review=assignment.state)
        case EventTagsAssignment():
            event = replace(
                event, tags=tuple(sorted(set(event.tags) | set(assignment.tags)))
            )
    return replace(event, rules=tuple(sorted(policies.items()))), leg


def _enrich_event(
    inputs: Inputs, movements: Mapping[RecordId, Movement], event: Event
) -> tuple[Event, tuple[Evaluation, ...]]:
    original = event
    evaluations: list[Evaluation] = []
    assigned: set[tuple[RecordId, RuleField]] = set()
    legs = [
        replace(
            leg,
            rule_basis=child_id(original.identity, RecordKind.LEG, leg.key),
        )
        if leg.kind == LegKind.BOUNDARY
        else leg
        for leg in original.legs
    ]
    views = {
        index: _view(movements, original, leg)
        for index, leg in enumerate(legs)
        if leg.kind == LegKind.BOUNDARY
    }
    for rule in sorted(inputs.rules, key=lambda item: (item.order, item.key.value)):
        for index, view in views.items():
            leg = legs[index]
            leg_id = child_id(original.identity, RecordKind.LEG, leg.key)
            eligible = rule.validity.applies(original.when)
            predicates = tuple(
                PredicateResult(
                    predicate,
                    view.get(predicate.field),
                    matches(predicate, view.get(predicate.field)),
                )
                for predicate in rule.predicates
            )
            results = []
            if eligible and all(result.matches for result in predicates):
                for assignment in rule.assignments:
                    field, value = assignment_value(assignment)
                    target = leg_id if field == RuleField.CATEGORY else event.identity
                    current = _current(event, leg, field)
                    if field == RuleField.TAGS:
                        selected = isinstance(value, tuple) and bool(
                            set(value) - set(event.tags)
                        )
                        outcome = (
                            AssignmentOutcome.APPLIED
                            if selected
                            else AssignmentOutcome.REDUNDANT
                        )
                    else:
                        selected = (target, field) not in assigned
                        outcome = (
                            AssignmentOutcome.REDUNDANT
                            if current == value
                            else AssignmentOutcome.APPLIED
                            if selected
                            else AssignmentOutcome.SHADOWED
                        )
                    if selected:
                        event, leg = _assign(event, leg, assignment, rule.declaration)
                    assigned.add((target, field))
                    results.append(AssignmentResult(field, value, outcome, selected))
            evaluations.append(
                Evaluation(
                    original.identity,
                    leg_id,
                    rule,
                    eligible,
                    predicates,
                    tuple(results),
                )
            )
            legs[index] = leg
    return replace(event, legs=tuple(legs)), tuple(evaluations)


def enrich(inputs: Inputs, resolution: Resolution) -> Resolution:
    """Retain all evaluations; diagnostics aggregate the same contributions."""
    from jbt.model.quantity import Finding, FindingCode  # noqa: PLC0415

    determinations = []
    evaluations = []
    movements = {movement.source_record: movement for movement in inputs.movements}
    for determination in resolution.determinations:
        if determination.event is None:
            determinations.append(determination)
            continue
        event, evidence = _enrich_event(inputs, movements, determination.event)
        determinations.append(replace(determination, event=event))
        evaluations.extend(evidence)
    findings = list(resolution.findings)
    findings.extend(
        Finding(FindingCode.UNCLASSIFIED, determination.identity, blocking=False)
        for determination in determinations
        if determination.event is not None
        and any(
            leg.kind == LegKind.BOUNDARY and leg.category is None
            for leg in determination.event.legs
        )
    )
    matching_by_rule: dict[RecordId, list[Evaluation]] = defaultdict(list)
    for evaluation in evaluations:
        if evaluation.matched:
            matching_by_rule[evaluation.rule.declaration].append(evaluation)
    for rule in inputs.rules:
        matching = matching_by_rule[rule.declaration]
        if not matching:
            findings.append(
                Finding(FindingCode.RULE_UNUSED, rule.declaration, blocking=False)
            )
        elif not any(
            result.selected
            for evaluation in matching
            for result in evaluation.assignments
        ):
            findings.append(
                Finding(FindingCode.RULE_SHADOWED, rule.declaration, blocking=False)
            )
        if any(evaluation.assignments for evaluation in matching) and all(
            result.outcome == AssignmentOutcome.REDUNDANT
            for evaluation in matching
            for result in evaluation.assignments
        ):
            findings.append(
                Finding(FindingCode.RULE_REDUNDANT, rule.declaration, blocking=False)
            )
    return replace(
        resolution,
        determinations=tuple(determinations),
        findings=tuple(findings),
        rules=tuple(evaluations),
    )
