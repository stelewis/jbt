"""Pure, original-position enrichment of the resolved quantity model."""

# GuardField's positional booleans are physical schema values, not switches.
# ruff: noqa: FBT003
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    AssignBody,
    Body,
    EventDateAssignment,
    EventReviewAssignment,
    EventReviewRef,
    EventStateAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    LegCategoryAssignment,
    LegDateAssignment,
    LegReviewRef,
    SplitBody,
    assignment_field,
)
from jbt.domain.errors import GuardError
from jbt.domain.guards import (
    Guard,
    GuardEvaluation,
    GuardField,
    GuardOutcome,
    GuardReference,
    GuardTarget,
    evaluate_guard,
    projection_digest,
)
from jbt.domain.identity import child_id
from jbt.domain.ids import RecordKind, SemanticKey
from jbt.domain.quantity import EventState, LegKind, ReviewState, total
from jbt.model.quantity import (
    DATE_ROLES,
    Event,
    Finding,
    FindingCode,
    Inputs,
    Leg,
    Resolution,
    ResolutionStatus,
    event_dates,
    leg_dates,
)

if TYPE_CHECKING:
    from jbt.domain.journal import JournalEntry


@dataclass(frozen=True, slots=True)
class Decision:
    """A selected custody revision with authenticated reviewed evidence."""

    entry: JournalEntry
    body: Body
    expected: GuardTarget


@dataclass(frozen=True, slots=True)
class Receipt:
    """Applicability against complete, retained pre-entry candidates."""

    entry: JournalEntry
    expected: GuardTarget
    candidates: tuple[GuardTarget, ...]
    evaluation: GuardEvaluation
    restore: tuple[tuple[GuardReference, tuple[GuardTarget, ...]], ...]


@dataclass(frozen=True, slots=True)
class Fold:
    """Financial result plus the exact boundaries correction commands use."""

    resolution: Resolution
    receipts: tuple[Receipt, ...]
    tail: tuple[GuardTarget, ...]


class _InvalidCorrectionError(ValueError):
    """A well-formed decision cannot operate on this native model state."""

    def __init__(self, cause: FindingCode) -> None:
        self.cause = cause
        super().__init__(cause.value)


def _wire(value: object) -> str:
    return encode_json(value, integer_strings=False).decode().strip()


def leg_facts(leg: Leg) -> dict[str, object]:
    """Project economic leg structure without policy or lexical precision."""
    return {
        "key": leg.key.value,
        "kind": leg.kind.value,
        "account": leg.account.value if leg.account else None,
        "position": leg.position.value if leg.position else None,
        "commodity": leg.commodity.value,
        "amount": {"coefficient": leg.amount.coefficient, "scale": leg.amount.scale},
        "source_leg": leg.source_leg.value if leg.source_leg else None,
        "transfer": leg.transfer_key.value if leg.transfer_key else None,
        "original_commodity": leg.original_commodity.value
        if leg.original_commodity is not None
        else None,
        "fx_rate": {
            "coefficient": leg.fx_rate.coefficient,
            "scale": leg.fx_rate.scale,
        }
        if leg.fx_rate is not None
        else None,
        "fx_base_commodity": leg.fx_base_commodity.value
        if leg.fx_base_commodity is not None
        else None,
        "fx_quote_commodity": leg.fx_quote_commodity.value
        if leg.fx_quote_commodity is not None
        else None,
        "dates": [
            {
                "mode": value.mode.value,
                "date": value.value.isoformat() if value.value else None,
            }
            for value in leg_dates(leg)
        ],
    }


def _event_targets(
    event: Event, *, active: bool, reference: GuardReference | None = None
) -> tuple[GuardTarget, ...]:
    common = (
        GuardField("source_identity", "string", False, _wire(asdict(event.occurrence))),
        GuardField("event_kind", "string", False, event.kind.value),
        GuardField("event_state", "string", False, event.state.value),
        GuardField("effective_date", "string", False, event.when.isoformat()),
        GuardField("effective_role", "string", False, event.effective_role.value),
        GuardField(
            "dates",
            "string",
            False,
            _wire(
                [value.isoformat() if value else None for value in event_dates(event)]
            ),
        ),
        GuardField(
            "accounts",
            "list",
            False,
            tuple(sorted({leg.account.value for leg in event.legs if leg.account})),
        ),
    )
    event_reference = EventReviewRef(event.identity).guard_reference
    event_targets = (
        (
            GuardTarget(
                event_reference,
                tuple(
                    sorted(
                        (
                            *common,
                            GuardField(
                                "legs",
                                "string",
                                False,
                                _wire([leg_facts(leg) for leg in event.legs]),
                            ),
                        ),
                        key=lambda field: field.name,
                    )
                ),
                active,
            ),
        )
        if reference is None or reference == event_reference
        else ()
    )
    return (
        *event_targets,
        *(
            GuardTarget(
                LegReviewRef(
                    child_id(event.identity, RecordKind.LEG, leg.key)
                ).guard_reference,
                tuple(
                    sorted(
                        (
                            *common,
                            GuardField("parent", "string", False, event.identity.value),
                            GuardField(
                                "leg",
                                "string",
                                False,
                                _wire(
                                    {
                                        **leg_facts(leg),
                                        "balancing": [
                                            leg_facts(candidate)
                                            for candidate in event.legs
                                            if candidate.source_leg == leg.key
                                        ],
                                    }
                                ),
                            ),
                        ),
                        key=lambda field: field.name,
                    )
                ),
                active,
            )
            for leg in event.legs
            if reference is None
            or reference
            == LegReviewRef(
                child_id(event.identity, RecordKind.LEG, leg.key)
            ).guard_reference
        ),
    )


def targets(resolution: Resolution) -> tuple[GuardTarget, ...]:
    """Project native candidates, never synthetic published rows."""
    return tuple(
        target
        for determination in resolution.determinations
        for event in (
            (determination.event,)
            if determination.event is not None
            else determination.candidates
        )
        for target in _event_targets(
            event,
            active=determination.event is not None
            and determination.status == ResolutionStatus.RESOLVED
            and determination.identity not in resolution.retired,
        )
    )


def profile(target: GuardTarget) -> tuple[str, ...]:
    """Own the complete profile; bodies cannot request weaker fields."""
    schemas = {
        "accounts": ("list", False),
        "dates": ("string", False),
        "event_kind": ("string", False),
        "event_state": ("string", False),
        "effective_date": ("string", False),
        "effective_role": ("string", False),
        "source_identity": ("string", False),
    }
    if target.reference.table.value == "review_events":
        schemas["legs"] = ("string", False)
    elif target.reference.table.value == "review_legs":
        schemas.update({"parent": ("string", False), "leg": ("string", False)})
    else:
        code = "native_guard_profile_required"
        raise GuardError(code, "correction.guard")
    if {field.name: (field.kind, field.nullable) for field in target.fields} != schemas:
        code = "native_guard_profile_required"
        raise GuardError(code, "correction.guard")
    return tuple(sorted(schemas))


def _meaning(event: Event) -> Event:
    return replace(
        event,
        corrections=(),
        legs=tuple(replace(leg, corrections=()) for leg in event.legs),
    )


def _rule_redundant(event: Event, body: AssignBody) -> bool:
    if isinstance(body.target, EventReviewRef):
        policies = dict(event.rules)
        return all(
            any(
                field.value == f"transactions.{assignment_field(assignment)}"
                for field in policies
            )
            and assignment_field(assignment) not in dict(event.corrections)
            for assignment in body.assignments
        )
    if not isinstance(body.target, LegReviewRef) or not all(
        isinstance(assignment, LegCategoryAssignment) for assignment in body.assignments
    ):
        return False
    return any(
        child_id(event.identity, RecordKind.LEG, leg.key) == body.target.leg
        and bool(leg.rules)
        and "category_id" not in dict(leg.corrections)
        for leg in event.legs
    )


def _event_assignment(
    event: Event, body: AssignBody, entry: JournalEntry, inputs: Inputs
) -> Event:
    revision = entry.revision_id
    changes = dict(event.corrections)
    for assignment in body.assignments:
        field = assignment_field(assignment)
        changes[field] = revision
        match assignment:
            case EventTextAssignment():
                event = replace(event, **{assignment.field.value: assignment.value})
            case EventReviewAssignment():
                event = replace(event, review=ReviewState(assignment.state.value))
            case EventStateAssignment():
                event = replace(event, state=assignment.state)
            case EventTagsAssignment():
                event = replace(event, tags=assignment.tags)
            case EventDateAssignment():
                values = list(event_dates(event))
                values[DATE_ROLES.index(assignment.role.value)] = assignment.value
                is_effective = assignment.role.value == event.effective_role.value
                if is_effective and (
                    assignment.value is None or assignment.value > inputs.horizon
                ):
                    raise _InvalidCorrectionError(FindingCode.CORRECTION_DATE)
                event = replace(
                    event,
                    dates=tuple(values),
                    when=assignment.value if is_effective else event.when,
                    times=tuple(
                        (role, time)
                        for role, time in event.times
                        if role.value != assignment.role.value
                        or time.date == assignment.value
                    ),
                )
    return replace(event, corrections=tuple(sorted(changes.items())))


def _assignment(
    event: Event, body: AssignBody, entry: JournalEntry, inputs: Inputs
) -> Event:
    if isinstance(body.target, EventReviewRef):
        return _event_assignment(event, body, entry, inputs)
    legs = []
    for original in event.legs:
        leg = original
        if child_id(event.identity, RecordKind.LEG, leg.key) != body.target.leg:
            legs.append(leg)
            continue
        changes = dict(leg.corrections)
        for assignment in body.assignments:
            field = assignment_field(assignment)
            changes[field] = entry.revision_id
            match assignment:
                case LegCategoryAssignment():
                    if leg.kind != LegKind.BOUNDARY:
                        raise _InvalidCorrectionError(FindingCode.CATEGORY_TARGET)
                    if (
                        assignment.category is not None
                        and assignment.category not in inputs.categories
                    ):
                        raise _InvalidCorrectionError(FindingCode.CATEGORY_UNKNOWN)
                    leg = replace(
                        leg,
                        category=SemanticKey(assignment.category.value)
                        if assignment.category
                        else None,
                    )
                case LegDateAssignment():
                    dates = list(leg_dates(leg))
                    dates[DATE_ROLES.index(assignment.role.value)] = assignment.date
                    leg = replace(leg, dates=tuple(dates))
        legs.append(replace(leg, corrections=tuple(sorted(changes.items()))))
    return replace(event, legs=tuple(legs))


def _split(event: Event, body: SplitBody, entry: JournalEntry) -> Event:
    legs = []
    for leg in event.legs:
        if child_id(event.identity, RecordKind.LEG, leg.key) != body.parent.leg:
            legs.append(leg)
            continue
        amount = total(child.amount for child in body.children)
        if leg.kind != LegKind.BOUNDARY or (amount.coefficient, amount.scale) != (
            leg.amount.coefficient,
            leg.amount.scale,
        ):
            raise _InvalidCorrectionError(FindingCode.SPLIT)
        for child in body.children:
            key = SemanticKey(
                "split:"
                + _wire([leg.key.value, entry.key.value, child.child_key.value])
            )
            legs.append(
                replace(
                    leg,
                    key=key,
                    amount=child.amount,
                    corrections=tuple(
                        sorted(
                            {
                                **dict(leg.corrections),
                                "amount": entry.revision_id,
                            }.items()
                        )
                    ),
                )
            )
    return replace(event, legs=tuple(legs))


def _references(event: Event) -> tuple[GuardReference, ...]:
    return tuple(
        dict.fromkeys(
            (
                EventReviewRef(event.identity).guard_reference,
                *(
                    LegReviewRef(
                        child_id(event.identity, RecordKind.LEG, leg.key)
                    ).guard_reference
                    for leg in event.legs
                ),
            )
        )
    )


def update_inventory(
    inventory: dict[GuardReference, tuple[GuardTarget, ...]],
    references: tuple[GuardReference, ...],
    updated: tuple[GuardTarget, ...],
) -> tuple[tuple[GuardReference, tuple[GuardTarget, ...]], ...]:
    """Replace affected native addresses and retain their exact reverse delta."""
    replacements: dict[GuardReference, tuple[GuardTarget, ...]] = {}
    for target in updated:
        replacements[target.reference] = (
            *replacements.get(target.reference, ()),
            target,
        )
    restore = []
    for reference in dict.fromkeys((*references, *replacements)):
        before = inventory.get(reference, ())
        after = replacements.get(reference, ())
        if before != after:
            restore.append((reference, before))
            if after:
                inventory[reference] = after
            else:
                inventory.pop(reference, None)
    return tuple(restore)


def fold(  # noqa: C901, PLR0912, PLR0915 - original-position applicability and mutation
    inputs: Inputs, resolution: Resolution, decisions: tuple[Decision, ...]
) -> Fold:
    """Apply active revisions in supplied first-record logical order."""
    receipts: list[Receipt] = []
    findings = list(resolution.findings)
    assigned: set[tuple[GuardReference, str]] = set()
    determinations = list(resolution.determinations)
    locations: dict[GuardReference, list[tuple[int, int | None]]] = {}
    inventory: dict[GuardReference, tuple[GuardTarget, ...]] = {}
    for target in targets(resolution):
        inventory[target.reference] = (*inventory.get(target.reference, ()), target)

    for index, determination in enumerate(determinations):
        events = (
            ((None, determination.event),)
            if determination.event is not None
            else tuple(enumerate(determination.candidates))
        )
        for candidate_index, event in events:
            for reference in _references(event):
                locations.setdefault(reference, []).append((index, candidate_index))
    for decision in decisions:
        entry, body, expected = decision.entry, decision.body, decision.expected
        if not isinstance(body, AssignBody | SplitBody):
            message = "enrich_decision_required"
            raise TypeError(message)
        if isinstance(body, AssignBody):
            fields = {
                (expected.reference, assignment_field(item))
                for item in body.assignments
            }
            if fields & assigned:
                findings.append(
                    Finding(FindingCode.CONFLICT, entry.revision_id, blocking=True)
                )
            assigned.update(fields)
        matching = locations.get(expected.reference, ())
        candidates = inventory.get(expected.reference, ())
        fields = profile(expected)
        guard = Guard(expected.reference, fields, projection_digest(expected, fields))
        evaluation = evaluate_guard(guard, candidates)
        receipts.append(Receipt(entry, expected, candidates, evaluation, ()))
        if evaluation.outcome != GuardOutcome.APPLIED:
            findings.append(
                Finding(FindingCode.GUARD, entry.revision_id, blocking=True)
            )
            continue
        reference = body.target if isinstance(body, AssignBody) else body.parent
        if reference.guard_reference != expected.reference:
            message = "decision_basis_target_mismatch"
            raise ValueError(message)
        if len(matching) != 1:
            message = "applied_guard_without_unique_event"
            raise RuntimeError(message)
        index, candidate_index = matching[0]
        determination = determinations[index]
        event = determination.event
        if candidate_index is not None or event is None:
            message = "applied_guard_without_resolved_event"
            raise RuntimeError(message)
        try:
            updated = (
                _assignment(event, body, entry, inputs)
                if isinstance(body, AssignBody)
                else _split(event, body, entry)
            )
        except _InvalidCorrectionError as error:
            findings.append(Finding(error.cause, entry.revision_id, blocking=True))
            updated = event
        if (
            isinstance(body, AssignBody)
            and _rule_redundant(event, body)
            and _meaning(updated) == _meaning(event)
        ):
            findings.append(
                Finding(FindingCode.REDUNDANT, entry.revision_id, blocking=False)
            )
        if isinstance(body, SplitBody):
            for reference in _references(event):
                locations[reference].remove((index, None))
            for reference in _references(updated):
                locations.setdefault(reference, []).append((index, None))
        if updated != event:
            receipts[-1] = replace(
                receipts[-1],
                restore=update_inventory(
                    inventory,
                    _references(event),
                    _event_targets(
                        updated,
                        active=determination.status == ResolutionStatus.RESOLVED,
                    ),
                ),
            )
        determinations[index] = replace(determination, event=updated)
    resolution = replace(resolution, determinations=tuple(determinations))
    # Classification findings describe the enriched result, not the rule tail.
    findings = [
        finding for finding in findings if finding.code != FindingCode.UNCLASSIFIED
    ]
    findings.extend(
        Finding(FindingCode.UNCLASSIFIED, event.identity, blocking=False)
        for event in resolution.events
        if event.state == EventState.RECOGNIZED
        and any(
            leg.kind == LegKind.BOUNDARY and leg.category is None for leg in event.legs
        )
    )
    blocking_revisions = {finding.scope for finding in findings if finding.blocking}
    blocked_records = set()
    for receipt in receipts:
        if receipt.entry.revision_id not in blocking_revisions:
            continue
        reference = receipt.expected.reference
        parent = (
            reference.key[0]
            if reference.table.value == "review_events"
            else next(
                field.value
                for field in receipt.expected.fields
                if field.name == "parent"
            )
        )
        blocked_records.add(parent)
    for determination in resolution.determinations:
        if (
            determination.identity.value in blocked_records
            and determination.event is not None
            and determination.status == ResolutionStatus.RESOLVED
        ):
            event = determination.event
            restore = dict(receipts[-1].restore)
            for reference, before in update_inventory(
                inventory, _references(event), _event_targets(event, active=False)
            ):
                restore.setdefault(reference, before)
            receipts[-1] = replace(receipts[-1], restore=tuple(restore.items()))
    resolution = replace(
        resolution,
        determinations=tuple(
            replace(determination, status=ResolutionStatus.BLOCKED)
            if determination.identity.value in blocked_records
            else determination
            for determination in resolution.determinations
        ),
    )
    return Fold(
        replace(resolution, findings=tuple(findings)),
        tuple(receipts),
        tuple(target for candidates in inventory.values() for target in candidates),
    )
