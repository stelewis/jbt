"""Original-position reviewed correspondence and scoped evidence replacement."""

# Diagnostic codes and physical schema booleans are intentional.
# ruff: noqa: EM101, FBT003
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    DecisionError,
    IdentityBody,
    IdentityMode,
    StatementRevisionBody,
    structural_references,
)
from jbt.domain.guards import (
    Guard,
    GuardField,
    GuardOutcome,
    GuardReference,
    GuardTarget,
    evaluate_guard,
    projection_digest,
)
from jbt.domain.identity import Occurrence, SourceKeyAnchor, event_id
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.review import compound_candidates, compound_references
from jbt.model.corrections import Decision, Receipt, leg_facts
from jbt.model.merge import occurrence_evidence, source_membership, statement_evidence
from jbt.model.quantity import (
    Determination,
    Event,
    EventKind,
    Finding,
    FindingCode,
    IdentitySelection,
    Inputs,
    Resolution,
    ResolutionStatus,
    StatementReplacement,
    Support,
    SupportKind,
    Validity,
    merge,
)

if TYPE_CHECKING:
    from jbt.domain.time import TimeRole
    from jbt.model.quantity import StatementPart


def _wire(value: object) -> str:
    return encode_json(value, integer_strings=False).decode().strip()


def _period(value: Validity | None) -> list[str | None] | None:
    return (
        [
            value.start.isoformat() if value.start else None,
            value.end.isoformat() if value.end else None,
        ]
        if value is not None
        else None
    )


def _replacement(value: StatementReplacement) -> dict[str, object]:
    return {
        "replaced": [key.value for key in value.replaced],
        "replacement": [key.value for key in value.replacement],
        "account": value.account.value,
        "fact_kind": value.fact_kind.value,
        "basis": value.basis.value,
        "date_role": value.date_role.value,
        "period": _period(value.validity),
    }


def native_targets(inputs: Inputs) -> tuple[GuardTarget, ...]:
    """Review source economics independently of unresolved correspondence."""
    occurrences = occurrence_evidence(inputs)
    source_targets = []
    facts_by_source = {}
    for evidence in occurrences:
        movement = evidence.movement
        facts = {
            "source_record": movement.source_record.value,
            "source_anchor": asdict(movement.anchor),
            "source_identity": asdict(
                Occurrence(movement.source_key or movement.anchor, movement.event_key)
            ),
            "state": movement.state.value,
            "effective_date": movement.when.isoformat(),
            "effective_role": movement.effective_role.value,
            "dates": [value.isoformat() if value else None for value in movement.dates],
            "legs": [leg_facts(leg) for leg in evidence.legs],
        }
        facts_by_source[movement.source_record.value] = facts
        source_targets.append(
            GuardTarget(
                GuardReference(
                    inputs.entity,
                    SemanticKey("review_sources"),
                    (movement.source_record.value,),
                ),
                (
                    GuardField("facts", "string", False, _wire(facts)),
                    GuardField(
                        "membership",
                        "string",
                        False,
                        _wire(
                            {
                                "canonical": asdict(evidence.canonical),
                                "identity": evidence.identity.value,
                                "disposition": (
                                    evidence.disposition.value
                                    if evidence.disposition is not None
                                    and evidence.disposition
                                    in {SupportKind.ECLIPSED, SupportKind.REPLACED}
                                    else "live"
                                ),
                            }
                        ),
                    ),
                ),
                active=not any(finding.blocking for finding in evidence.findings),
            )
        )
    statement_targets = []
    for statement, replacements in statement_evidence(inputs):
        facts = {
            "statement": statement.identity.value,
            "scope": statement.scope.value,
            "section": statement.section.value,
            "blob_digest": statement.blob_digest,
            "account": statement.account.value if statement.account else None,
            "parts": [
                {
                    "key": part.key.value,
                    "fact_kind": part.fact_kind.value,
                    "basis": part.basis.value,
                    "date_role": part.date_role.value,
                    "period": _period(part.validity),
                    "complete": part.complete,
                    "point": (
                        {
                            "date": part.point.date.isoformat(),
                            "local": part.point.local,
                            "offset_minutes": part.point.offset_minutes,
                            "zone": part.point.zone,
                            "precision": part.point.precision.value,
                            "fraction_digits": part.point.fraction_digits,
                        }
                        if part.point
                        else None
                    ),
                    "source_records": [record.value for record in part.source_records],
                    "movements": [
                        facts_by_source[record.value]
                        for record in part.source_records
                        if record.value in facts_by_source
                    ],
                    "balances": [
                        {
                            "source_record": balance.source_record.value,
                            "key": balance.observation_key.value,
                            "account": balance.account.value,
                            "position": balance.position.value
                            if balance.position
                            else None,
                            "commodity": balance.commodity.value,
                            "amount": {
                                "coefficient": balance.amount.coefficient,
                                "scale": balance.amount.scale,
                            },
                        }
                        for balance in part.balances
                    ],
                }
                for part in statement.parts
            ],
        }
        statement_targets.append(
            GuardTarget(
                GuardReference(
                    inputs.entity,
                    SemanticKey("review_statements"),
                    (statement.identity.value,),
                ),
                (
                    GuardField("facts", "string", False, _wire(facts)),
                    GuardField(
                        "membership",
                        "string",
                        False,
                        _wire([_replacement(value) for value in replacements]),
                    ),
                ),
                active=statement.account is not None,
            )
        )
    return tuple(
        sorted(
            (*source_targets, *statement_targets),
            key=lambda target: (target.reference.table.value, target.reference.key),
        )
    )


def _identity_selection(inputs: Inputs, body: IdentityBody) -> IdentitySelection:
    movements = {movement.source_record: movement for movement in inputs.movements}
    if any(record not in movements for record in body.members):
        raise DecisionError("identity_member_missing", "decision.identity")
    canonical = movements[body.canonical]
    if any(
        movements[record].event_key != canonical.event_key for record in body.members
    ):
        raise DecisionError("same_component_required", "decision.identity")
    if body.mode == IdentityMode.OCCURRENCE:
        return IdentitySelection(
            Occurrence(canonical.anchor, canonical.event_key),
            source_records=body.members,
        )
    membership = source_membership(inputs)
    destination = membership[body.canonical]
    if canonical.source_key is None or not isinstance(
        destination.anchor, SourceKeyAnchor
    ):
        raise DecisionError("canonical_source_key_required", "decision.identity")
    families: set[SourceKeyAnchor] = set()
    records = []
    for record in body.members:
        anchor = membership[record].anchor
        if isinstance(anchor, SourceKeyAnchor):
            if anchor != destination.anchor:
                families.add(anchor)
        else:
            records.append(record)
    return IdentitySelection(
        destination,
        source_records=tuple(records),
        families=tuple(sorted(families, key=lambda value: _wire(asdict(value)))),
    )


def _matching_parts(part: StatementPart, body: StatementRevisionBody) -> bool:
    if part.fact_kind != body.fact_kind or part.basis != body.basis:
        return False
    if part.point is not None:
        return body.start <= part.point.date < body.end
    period = part.validity
    return (
        period is not None
        and (period.start is None or period.start < body.end)
        and (period.end is None or body.start < period.end)
    )


def _revision_selection(
    inputs: Inputs, body: StatementRevisionBody
) -> StatementReplacement:
    statements = {
        statement.identity.value: statement for statement in inputs.statements
    }
    roles: set[TimeRole] = set()
    for identifier in (*body.replaced, *body.replacement):
        statement = statements.get(identifier)
        if statement is None or statement.account != body.account:
            raise DecisionError(
                "revision_statement_scope", "decision.statement_revision"
            )
        parts = tuple(part for part in statement.parts if _matching_parts(part, body))
        if not parts:
            raise DecisionError("revision_basis_scope", "decision.statement_revision")
        roles.update(part.date_role for part in parts)
    if len(roles) != 1:
        raise DecisionError(
            "revision_date_role_ambiguous", "decision.statement_revision"
        )
    if body.fact_kind == FactKind.MOVEMENT:
        periods = sorted(
            (
                part.validity
                for identifier in body.replacement
                for part in statements[identifier].parts
                if _matching_parts(part, body)
                and part.complete
                and part.validity is not None
            ),
            key=lambda period: period.start or body.start,
        )
        covered = body.start
        for period in periods:
            if period.start is not None and period.start > covered:
                break
            covered = max(covered, period.end or body.end)
        if covered < body.end:
            raise DecisionError(
                "complete_revision_scope_required", "decision.statement_revision"
            )
    return StatementReplacement(
        tuple(SemanticKey(value) for value in body.replaced),
        tuple(SemanticKey(value) for value in body.replacement),
        body.account,
        body.fact_kind,
        body.basis,
        next(iter(roles)),
        Validity(body.start, body.end),
    )


def _apply(inputs: Inputs, body: IdentityBody | StatementRevisionBody) -> Inputs:
    if isinstance(body, IdentityBody):
        return replace(
            inputs,
            identity_selections=(
                *inputs.identity_selections,
                _identity_selection(inputs, body),
            ),
        )
    return replace(
        inputs,
        statement_replacements=(
            *inputs.statement_replacements,
            _revision_selection(inputs, body),
        ),
    )


def _changes(
    before: tuple[GuardTarget, ...], after: tuple[GuardTarget, ...]
) -> tuple[tuple[GuardReference, tuple[GuardTarget, ...]], ...]:
    old = {target.reference: target for target in before}
    new = {target.reference: target for target in after}
    return tuple(
        (reference, (old[reference],) if reference in old else ())
        for reference in sorted(
            old.keys() | new.keys(),
            key=lambda reference: (reference.table.value, reference.key),
        )
        if old.get(reference) != new.get(reference)
    )


def _retirement_history(inputs: Inputs, resolution: Resolution) -> Resolution:
    if not resolution.retired:
        return resolution
    groups = {}
    for evidence in occurrence_evidence(inputs):
        movement = evidence.movement
        for occurrence in {
            Occurrence(movement.source_key or movement.anchor, movement.event_key),
            Occurrence(movement.anchor, movement.event_key),
        }:
            identity = event_id(inputs.entity, occurrence)
            if identity in resolution.retired:
                groups.setdefault(identity, []).append((evidence, occurrence))
    history = []
    present = {determination.identity for determination in resolution.determinations}
    for identity, members in groups.items():
        if identity in present:
            continue
        evidence, occurrence = min(
            members, key=lambda item: item[0].movement.source_record.value
        )
        movement = evidence.movement
        event = Event(
            identity,
            occurrence,
            movement.when,
            EventKind.TRANSACTION,
            EventState.RETRACTED,
            ReviewState.UNREVIEWED,
            movement.payee,
            movement.narration,
            evidence.legs,
            tuple(
                Support(item.movement.source_record, SupportKind.REPLACED)
                for item, _ in sorted(
                    members, key=lambda pair: pair[0].movement.source_record.value
                )
            ),
            None,
            effective_role=movement.effective_role,
            dates=movement.dates,
        )
        history.append(Determination(identity, ResolutionStatus.RESOLVED, event))
    return replace(
        resolution,
        determinations=tuple(
            sorted(
                (*resolution.determinations, *history),
                key=lambda value: value.identity.value,
            )
        ),
    )


def _lineage(
    resolution: Resolution,
    origins: dict[RecordId, RecordId],
    identity_origins: dict[RecordId, RecordId],
    participation: dict[RecordId, tuple[int, RecordId]],
) -> Resolution:
    determinations = []
    for determination in resolution.determinations:
        event = determination.event
        if event is not None:
            support = tuple(
                replace(member, decision=origins.get(member.source_record))
                for member in event.support
            )
            corrections = dict(event.corrections)
            decisions = tuple(
                participation[member.source_record]
                for member in support
                if member.source_record in participation
            )
            if event.state == EventState.RETRACTED:
                if (
                    event.identity in resolution.retired
                    and event.identity in identity_origins
                ):
                    corrections["event_state"] = identity_origins[event.identity]
                elif decisions:
                    corrections["event_state"] = max(
                        decisions, key=lambda value: value[0]
                    )[1]
            event = replace(
                event,
                support=support,
                identity_decision=identity_origins.get(event.identity),
                corrections=tuple(sorted(corrections.items())),
            )
        determinations.append(
            replace(determination, event=event) if event is not None else determination
        )
    return replace(resolution, determinations=tuple(determinations))


def _changed_lineage(
    before: Inputs,
    after: Inputs,
    decision: Decision,
    position: int,
    changes: tuple[tuple[GuardReference, tuple[GuardTarget, ...]], ...],
) -> tuple[
    dict[RecordId, RecordId],
    dict[RecordId, RecordId],
    dict[RecordId, tuple[int, RecordId]],
]:
    sources = {
        RecordId(
            before.entity, RecordKind.SOURCE_RECORD, str(reference.key[0])
        ): decision.entry.revision_id
        for reference, _ in changes
        if reference.table.value == "review_sources"
    }
    identities = {}
    participation = {}
    if isinstance(decision.body, IdentityBody):
        old, new = source_membership(before), source_membership(after)
        for record, canonical in new.items():
            if canonical != old[record]:
                for occurrence in (old[record], canonical):
                    identities[event_id(before.entity, occurrence)] = (
                        decision.entry.revision_id
                    )
    else:
        participation = {
            record: (position, revision) for record, revision in sources.items()
        }
    return sources, identities, participation


@dataclass(frozen=True, slots=True)
class MergeFold:
    """Source determinations and exact guard boundaries before enrichment."""

    inputs: Inputs
    resolution: Resolution
    receipts: tuple[Receipt, ...]
    tail: tuple[GuardTarget, ...]


def fold(inputs: Inputs, decisions: tuple[Decision, ...]) -> MergeFold:
    """Execute only active merge decisions in first-record logical order."""
    current = inputs
    inventory = native_targets(current)
    receipts = []
    findings = []
    blocked: set[RecordId] = set()
    origins: dict[RecordId, RecordId] = {}
    identity_origins: dict[RecordId, RecordId] = {}
    participation: dict[RecordId, tuple[int, RecordId]] = {}
    for position, decision in enumerate(decisions):
        body, expected = decision.body, decision.expected
        if not isinstance(body, IdentityBody | StatementRevisionBody):
            raise TypeError("merge_decision_required")
        if compound_references(expected) != structural_references(body):
            raise DecisionError("decision_basis_operands", "decision.merge")
        guard = Guard(
            expected.reference, ("members",), projection_digest(expected, ("members",))
        )
        candidates = compound_candidates(expected, inventory)
        evaluation = evaluate_guard(guard, candidates)
        restore = ()
        if evaluation.outcome != GuardOutcome.APPLIED:
            findings.append(
                Finding(FindingCode.GUARD, decision.entry.revision_id, blocking=True)
            )
            if isinstance(body, IdentityBody):
                membership = source_membership(current)
                blocked.update(
                    event_id(inputs.entity, membership[record])
                    for record in body.members
                    if record in membership
                )
        else:
            try:
                updated = _apply(current, body)
            except DecisionError:
                findings.append(
                    Finding(
                        FindingCode.DECISION_INVALID,
                        decision.entry.revision_id,
                        blocking=True,
                    )
                )
            else:
                new_inventory = native_targets(updated)
                restore = _changes(inventory, new_inventory)
                source_changes, identity_changes, state_changes = _changed_lineage(
                    current, updated, decision, position, restore
                )
                origins.update(source_changes)
                identity_origins.update(identity_changes)
                participation.update(state_changes)
                current, inventory = updated, new_inventory
        receipts.append(
            Receipt(decision.entry, expected, candidates, evaluation, restore)
        )
    resolution = _lineage(
        _retirement_history(current, merge(current)),
        origins,
        identity_origins,
        participation,
    )
    resolution = replace(
        resolution,
        determinations=tuple(
            replace(determination, status=ResolutionStatus.BLOCKED)
            if determination.identity in blocked
            else determination
            for determination in resolution.determinations
        ),
        findings=(*resolution.findings, *findings),
    )
    return MergeFold(current, resolution, tuple(receipts), inventory)
