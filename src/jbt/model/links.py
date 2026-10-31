"""Guarded transfer structure and evidence relations after enrichment."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from jbt.domain.decisions import (
    DocumentLinkBody,
    EventReviewRef,
    LifecycleLinkBody,
    TransferBody,
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
from jbt.domain.identity import child_id
from jbt.domain.ids import RecordKind, SemanticKey
from jbt.domain.quantity import EventState, LegKind, equal, total
from jbt.domain.review import compound_candidates, compound_references
from jbt.domain.rules import RuleField
from jbt.model.corrections import Decision, Fold, Receipt, targets, update_inventory
from jbt.model.quantity import (
    Event,
    EventKind,
    Finding,
    FindingCode,
    Inputs,
    Leg,
    Link,
    LinkKind,
    LinkMember,
    MemberKind,
    MemberRole,
    PositionPurpose,
    Resolution,
    ResolutionStatus,
)
from jbt.model.rules import AssignmentOutcome

if TYPE_CHECKING:
    from jbt.domain.ids import EntityId, RecordId


class _InvalidLinkError(ValueError):
    def __init__(self, code: FindingCode) -> None:
        self.code = code
        super().__init__(code.value)


def document_targets(
    entity: EntityId, documents: tuple[str, ...]
) -> tuple[GuardTarget, ...]:
    """Expose only explicitly selected, retained archive-only blobs."""
    return tuple(
        GuardTarget(
            GuardReference(entity, SemanticKey("review_documents"), (digest,)),
            (
                GuardField("archive_only", "boolean", False, True),  # noqa: FBT003
                GuardField("blob_digest", "string", False, digest),  # noqa: FBT003
            ),
            active=True,
        )
        for digest in sorted(set(documents))
    )


def _leg_id(event: Event, leg: Leg) -> RecordId:
    return child_id(event.identity, RecordKind.LEG, leg.key)


def _transfer_members(
    resolution: Resolution, body: TransferBody
) -> tuple[tuple[Event, Leg], ...]:
    locations = {
        _leg_id(event, leg): (event, leg)
        for event in resolution.events
        for leg in event.legs
    }
    members = (*body.outgoing, *body.incoming)
    selected = tuple(locations[member] for member in members)
    if any(leg.transfer is not None for _, leg in selected):
        raise _InvalidLinkError(FindingCode.TRANSFER_OWNERSHIP)
    for member, (event, leg) in zip(members, selected, strict=True):
        if (
            event.state != EventState.RECOGNIZED
            or event.kind != EventKind.TRANSACTION
            or leg.kind != LegKind.POSITION
            or leg.source_leg is not None
            or leg.commodity != body.commodity
            or leg.account is None
            or (
                int(leg.amount.coefficient) >= 0
                if member in body.outgoing
                else int(leg.amount.coefficient) <= 0
            )
        ):
            raise _InvalidLinkError(FindingCode.TRANSFER_INVALID)
    if body.outgoing and body.incoming:
        if not equal(total(leg.amount for _, leg in selected), total(())):
            raise _InvalidLinkError(FindingCode.TRANSFER_INVALID)
        outgoing_accounts = {locations[member][1].account for member in body.outgoing}
        incoming_accounts = {locations[member][1].account for member in body.incoming}
        if outgoing_accounts & incoming_accounts:
            raise _InvalidLinkError(FindingCode.TRANSFER_INVALID)
    return selected


def _transit(
    inputs: Inputs, event: Event, leg: Leg, decision: Decision
) -> tuple[Leg, Leg]:
    balancing = tuple(
        candidate for candidate in event.legs if candidate.source_leg == leg.key
    )
    if (
        len(balancing) != 1
        or balancing[0].kind != LegKind.BOUNDARY
        or balancing[0].commodity != leg.commodity
        or not equal(total((leg.amount, balancing[0].amount)), total(()))
    ):
        raise _InvalidLinkError(FindingCode.TRANSFER_INVALID)
    boundary = balancing[0]
    if "category_id" in dict(boundary.corrections):
        raise _InvalidLinkError(FindingCode.CONFLICT)
    policies = {
        (position.key, position.account): position
        for position in inputs.positions
        if position.purpose == PositionPurpose.IN_TRANSIT
        and position.commodity == leg.commodity
        and position.validity.applies(event.when)
    }
    if len(policies) != 1:
        raise _InvalidLinkError(FindingCode.TRANSIT)
    policy = next(iter(policies.values()))
    if policy.account == leg.account:
        raise _InvalidLinkError(FindingCode.TRANSIT)
    return boundary, replace(
        boundary,
        kind=LegKind.POSITION,
        account=policy.account,
        position=policy.key,
        category=None,
        policy=policy.declaration,
        rules=(),
        transfer=decision.entry.revision_id,
        transfer_key=decision.entry.key,
    )


def _transfer(inputs: Inputs, resolution: Resolution, decision: Decision) -> Resolution:
    body, entry = decision.body, decision.entry
    if not isinstance(body, TransferBody):
        message = "transfer_body_required"
        raise TypeError(message)
    selected = _transfer_members(resolution, body)
    replacements: dict[RecordId, Event] = {}
    findings = list(resolution.findings)
    shadowed: set[RecordId] = set()
    for original, leg in selected:
        event = replacements.get(original.identity, original)
        boundary, transit = _transit(inputs, event, leg, decision)
        shadowed.update(boundary.rules)
        marked = replace(leg, transfer=entry.revision_id, transfer_key=entry.key)
        replacements[event.identity] = replace(
            event,
            legs=tuple(
                marked
                if candidate.key == leg.key
                else transit
                if candidate.key == boundary.key
                else candidate
                for candidate in event.legs
            ),
        )
    findings.extend(
        Finding(FindingCode.RULE_SHADOWED, rule, blocking=False)
        for rule in sorted(shadowed, key=lambda rule: rule.value)
    )
    transferred = {
        _leg_id(event, leg)
        for event in replacements.values()
        for leg in event.legs
        if leg.source_leg is not None and leg.transfer_key == entry.key
    }
    rules = tuple(
        replace(
            evaluation,
            assignments=tuple(
                replace(assignment, outcome=AssignmentOutcome.SHADOWED, selected=False)
                if assignment.field == RuleField.CATEGORY
                else assignment
                for assignment in evaluation.assignments
            ),
        )
        if evaluation.leg in transferred
        else evaluation
        for evaluation in resolution.rules
    )
    links = resolution.links
    if body.outgoing and body.incoming:
        links = (
            *links,
            Link(
                entry.key,
                LinkKind.TRANSFER,
                tuple(
                    LinkMember(role, MemberKind.POSTING, member)
                    for role, side in (
                        (MemberRole.OUTGOING, body.outgoing),
                        (MemberRole.INCOMING, body.incoming),
                    )
                    for member in side
                ),
                entry.revision_id,
            ),
        )
    return replace(
        resolution,
        determinations=tuple(
            replace(determination, event=replacements[determination.identity])
            if determination.identity in replacements
            else determination
            for determination in resolution.determinations
        ),
        findings=tuple(findings),
        rules=rules,
        links=links,
    )


def _link(resolution: Resolution, decision: Decision) -> Resolution:
    body, entry = decision.body, decision.entry
    if isinstance(body, DocumentLinkBody):
        subject = body.target
        link = Link(
            entry.key,
            LinkKind.DOCUMENT,
            (
                LinkMember(
                    MemberRole.SUBJECT,
                    MemberKind.TRANSACTION
                    if isinstance(subject, EventReviewRef)
                    else MemberKind.POSTING,
                    subject.event
                    if isinstance(subject, EventReviewRef)
                    else subject.leg,
                ),
                *(
                    LinkMember(MemberRole.EVIDENCE, MemberKind.BLOB, digest)
                    for digest in body.documents
                ),
            ),
            entry.revision_id,
        )
    elif isinstance(body, LifecycleLinkBody):
        events = {event.identity: event for event in resolution.events}
        attempt, fee = events[body.attempt.event], events[body.fee.event]
        if (
            attempt.kind != EventKind.TRANSACTION
            or fee.kind != EventKind.TRANSACTION
            or fee.state != EventState.RECOGNIZED
        ):
            raise _InvalidLinkError(FindingCode.LINK)
        fee_legs = tuple(
            leg
            for leg in fee.legs
            if leg.kind == LegKind.POSITION and leg.source_leg is None
        )
        if not fee_legs or any(int(leg.amount.coefficient) >= 0 for leg in fee_legs):
            raise _InvalidLinkError(FindingCode.LINK)
        link = Link(
            entry.key,
            LinkKind.LIFECYCLE,
            (
                LinkMember(
                    MemberRole.PREDECESSOR,
                    MemberKind.TRANSACTION,
                    attempt.identity,
                ),
                *(
                    LinkMember(MemberRole.FEE, MemberKind.POSTING, _leg_id(fee, leg))
                    for leg in fee_legs
                ),
            ),
            entry.revision_id,
        )
    else:
        message = "link_body_required"
        raise TypeError(message)
    return replace(resolution, links=(*resolution.links, link))


def _blocked_records(
    references: tuple[GuardReference, ...],
    inventory: dict[GuardReference, tuple[GuardTarget, ...]],
) -> set[str]:
    return {
        str(reference.key[0])
        for reference in references
        if reference.table.value == "review_events"
    } | {
        str(field.value)
        for reference in references
        for target in inventory.get(reference, ())
        for field in target.fields
        if field.name == "parent"
    }


def fold(
    inputs: Inputs,
    resolution: Resolution,
    decisions: tuple[Decision, ...],
    documents: tuple[str, ...],
) -> Fold:
    """Derive structural legs only after exact compound applicability succeeds."""
    inventory: dict[GuardReference, tuple[GuardTarget, ...]] = {}
    for target in (*targets(resolution), *document_targets(inputs.entity, documents)):
        inventory[target.reference] = (*inventory.get(target.reference, ()), target)
    receipts: list[Receipt] = []
    blocked: set[str] = set()
    for decision in decisions:
        body, expected, entry = decision.body, decision.expected, decision.entry
        if not isinstance(body, TransferBody | DocumentLinkBody | LifecycleLinkBody):
            message = "link_stage_decision_required"
            raise TypeError(message)
        if compound_references(expected) != structural_references(body):
            message = "decision_basis_operands"
            raise ValueError(message)
        candidates = compound_candidates(
            expected,
            tuple(
                target
                for reference in structural_references(body)
                for target in inventory.get(reference, ())
            ),
        )
        fields = ("members",)
        evaluation = evaluate_guard(
            Guard(expected.reference, fields, projection_digest(expected, fields)),
            candidates,
        )
        receipt = Receipt(entry, expected, candidates, evaluation, ())
        if evaluation.outcome != GuardOutcome.APPLIED:
            resolution = replace(
                resolution,
                findings=(
                    *resolution.findings,
                    Finding(FindingCode.GUARD, entry.revision_id, blocking=True),
                ),
            )
            blocked.update(_blocked_records(structural_references(body), inventory))
        else:
            before = resolution
            try:
                resolution = (
                    _transfer(inputs, resolution, decision)
                    if isinstance(body, TransferBody)
                    else _link(resolution, decision)
                )
            except _InvalidLinkError as error:
                resolution = replace(
                    resolution,
                    findings=(
                        *resolution.findings,
                        Finding(error.code, entry.revision_id, blocking=True),
                    ),
                )
                blocked.update(_blocked_records(structural_references(body), inventory))
            receipt = replace(
                receipt,
                restore=_changed_targets(inventory, before, resolution),
            )
        receipts.append(receipt)
    resolution = replace(
        resolution,
        determinations=tuple(
            replace(determination, status=ResolutionStatus.BLOCKED)
            if determination.identity.value in blocked
            else determination
            for determination in resolution.determinations
        ),
    )
    if blocked:
        restore = dict(receipts[-1].restore)
        for reference, before in update_inventory(
            inventory,
            tuple(
                reference
                for reference in inventory
                if reference.table.value in {"review_events", "review_legs"}
            ),
            targets(resolution),
        ):
            restore.setdefault(reference, before)
        receipts[-1] = replace(receipts[-1], restore=tuple(restore.items()))
    findings = tuple(
        finding
        for finding in resolution.findings
        if finding.code != FindingCode.UNCLASSIFIED
    )
    findings += tuple(
        Finding(FindingCode.UNCLASSIFIED, event.identity, blocking=False)
        for event in resolution.events
        if event.state == EventState.RECOGNIZED
        and any(
            leg.kind == LegKind.BOUNDARY and leg.category is None for leg in event.legs
        )
    )
    return Fold(
        replace(resolution, findings=findings),
        tuple(receipts),
        tuple(target for group in inventory.values() for target in group),
    )


def _changed_targets(
    inventory: dict[GuardReference, tuple[GuardTarget, ...]],
    before: Resolution,
    after: Resolution,
) -> tuple[tuple[GuardReference, tuple[GuardTarget, ...]], ...]:
    changed = tuple(
        (original, updated)
        for original, updated in zip(
            before.determinations, after.determinations, strict=True
        )
        if original != updated
    )
    return update_inventory(
        inventory,
        tuple(
            target.reference
            for target in targets(
                replace(
                    before, determinations=tuple(original for original, _ in changed)
                )
            )
        ),
        targets(
            replace(after, determinations=tuple(updated for _, updated in changed))
        ),
    )
