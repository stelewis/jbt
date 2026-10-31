from dataclasses import replace
from datetime import date

import pytest

from jbt.domain.decisions import (
    DocumentLinkBody,
    EventReviewRef,
    LifecycleLinkBody,
    ReviewBasisRef,
    StructuralBody,
    TransferBody,
    decision_kind,
    structural_references,
)
from jbt.domain.identity import AuthoredAnchor, Occurrence, child_id, event_id
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.journal import JournalEntry, JournalOperation
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind, ReviewState, negate, total
from jbt.domain.review import compound_target
from jbt.domain.time import TimeRole
from jbt.model.corrections import Decision, targets
from jbt.model.links import document_targets, fold
from jbt.model.quantity import (
    Determination,
    Event,
    EventKind,
    Finding,
    FindingCode,
    Inputs,
    Leg,
    MemberRole,
    Position,
    PositionPurpose,
    Resolution,
    ResolutionStatus,
    Validity,
)

ENTITY = EntityId("invented-owner")
REVIEW = ReviewBasisRef("a" * 64, "b" * 64)


def _event(key: str, amount: str, *, when: date = date(2026, 1, 31)) -> Event:
    occurrence = Occurrence(AuthoredAnchor(SemanticKey(key)), SemanticKey("main"))
    source = Leg(
        SemanticKey("cash"),
        LegKind.POSITION,
        SemanticKey("USD"),
        ExactDecimal.parse(amount),
        SemanticKey(key),
        SemanticKey(key),
        None,
        None,
        None,
    )
    return Event(
        event_id(ENTITY, occurrence),
        occurrence,
        when,
        EventKind.TRANSACTION,
        EventState.RECOGNIZED,
        ReviewState.UNREVIEWED,
        None,
        None,
        (
            source,
            replace(
                source,
                key=SemanticKey("boundary:cash"),
                kind=LegKind.BOUNDARY,
                amount=negate(source.amount),
                account=None,
                position=None,
                source_leg=source.key,
            ),
        ),
        (),
        None,
        effective_role=TimeRole.POSTED,
    )


def _resolution(*events: Event) -> Resolution:
    return Resolution(
        tuple(
            Determination(event.identity, ResolutionStatus.RESOLVED, event)
            for event in events
        ),
        (),
    )


def _inputs() -> Inputs:
    revision = RecordId(ENTITY, RecordKind.DECLARATION_REVISION, "f" * 64)
    return Inputs(
        ENTITY,
        date(2026, 2, 28),
        (),
        (),
        (),
        (
            Position(
                SemanticKey("transit"),
                SemanticKey("internal"),
                SemanticKey("USD"),
                None,
                None,
                None,
                Validity(None, None),
                revision,
                PositionPurpose.IN_TRANSIT,
            ),
        ),
        (),
        (),
        (),
        (),
        (),
    )


def _leg(event: Event) -> RecordId:
    return child_id(event.identity, RecordKind.LEG, event.legs[0].key)


def _transfer(outgoing: tuple[Event, ...], incoming: tuple[Event, ...]) -> TransferBody:
    return TransferBody(
        ENTITY,
        REVIEW,
        tuple(
            sorted((_leg(event) for event in outgoing), key=lambda value: value.value)
        ),
        tuple(
            sorted((_leg(event) for event in incoming), key=lambda value: value.value)
        ),
        SemanticKey("USD"),
    )


def _decision(
    body: StructuralBody,
    resolution: Resolution,
    *,
    sequence: int = 1,
    documents: tuple[str, ...] = (),
) -> Decision:
    references = structural_references(body)
    expected = compound_target(
        tuple(
            target
            for target in (*targets(resolution), *document_targets(ENTITY, documents))
            if target.reference in references
        )
    )
    entry = JournalEntry(
        sequence,
        None if sequence == 1 else "d" * 64,
        ENTITY,
        SemanticKey(f"decision-{sequence}"),
        decision_kind(body),
        JournalOperation.RECORD,
        None,
        "a" * 64,
        "Synthetic review",
        "c" * 64,
    )
    return Decision(entry, body, expected)


@pytest.mark.parametrize("arrival", [date(2026, 1, 31), date(2026, 2, 2)])
def test_aggregate_transfer_preserves_source_and_balancing_ids_across_month_end(
    arrival: date,
) -> None:
    outgoing = _event("checking", "-15")
    incoming = (
        _event("card-a", "10", when=arrival),
        _event("card-b", "5", when=arrival),
    )
    initial = _resolution(outgoing, *incoming)
    one_sided = fold(
        _inputs(), initial, (_decision(_transfer((outgoing,), ()), initial),), ()
    )
    assert not one_sided.resolution.links
    transit = tuple(
        leg
        for event in one_sided.resolution.events
        for leg in event.legs
        if leg.position == SemanticKey("transit")
    )
    assert total(leg.amount for leg in transit) == ExactDecimal("15", 0, None)
    completed = fold(
        _inputs(), initial, (_decision(_transfer((outgoing,), incoming), initial),), ()
    )
    assert not completed.resolution.findings
    assert len(completed.resolution.links) == 1
    assert len(completed.resolution.links[0].members) == 3
    for before, after in zip(initial.events, completed.resolution.events, strict=True):
        assert [
            child_id(before.identity, RecordKind.LEG, leg.key) for leg in before.legs
        ] == [child_id(after.identity, RecordKind.LEG, leg.key) for leg in after.legs]
        assert after.legs[0].amount == before.legs[0].amount
    assert total(
        leg.amount
        for event in completed.resolution.events
        for leg in event.legs
        if leg.position == SemanticKey("transit")
    ) == ExactDecimal("0", 0, None)


@pytest.mark.parametrize(
    "failure", ["amount", "category", "split", "transit", "pending", "ownership"]
)
def test_transfer_rejects_invalid_economics_without_partial_rebooking(
    failure: str,
) -> None:
    outgoing, incoming = _event("checking", "-15"), _event("card", "15")
    inputs = _inputs()
    if failure == "amount":
        incoming = _event("card", "14")
    elif failure == "category":
        outgoing = replace(
            outgoing,
            legs=(
                outgoing.legs[0],
                replace(
                    outgoing.legs[1],
                    corrections=(("category_id", inputs.positions[0].declaration),),
                ),
            ),
        )
    elif failure == "split":
        outgoing = replace(
            outgoing,
            legs=(
                outgoing.legs[0],
                *(
                    replace(
                        outgoing.legs[1],
                        key=SemanticKey(key),
                        amount=ExactDecimal.parse(amount),
                    )
                    for key, amount in (("child-a", "10"), ("child-b", "5"))
                ),
            ),
        )
    elif failure == "transit":
        inputs = replace(inputs, positions=())
    elif failure == "pending":
        outgoing = replace(outgoing, state=EventState.PENDING)
    elif failure == "ownership":
        outgoing = replace(
            outgoing,
            legs=(
                replace(outgoing.legs[0], transfer=inputs.positions[0].declaration),
                outgoing.legs[1],
            ),
        )
    initial = _resolution(outgoing, incoming)
    result = fold(
        inputs, initial, (_decision(_transfer((outgoing,), (incoming,)), initial),), ()
    )
    assert any(finding.blocking for finding in result.resolution.findings)
    assert result.resolution.events == initial.events
    assert not result.resolution.links
    assert all(
        d.status == ResolutionStatus.BLOCKED for d in result.resolution.determinations
    )


def test_guard_tracks_both_transfer_sides_and_pair_structure_not_categories() -> None:
    outgoing, incoming = _event("checking", "-15"), _event("card", "15")
    initial = _resolution(outgoing, incoming)
    decision = _decision(_transfer((outgoing,), (incoming,)), initial)
    changed = _resolution(outgoing, _event("card", "16"))
    result = fold(_inputs(), changed, (decision,), ())
    assert result.receipts[0].evaluation.outcome.value == "changed"
    assert any(
        finding.code == FindingCode.GUARD for finding in result.resolution.findings
    )
    classified = replace(
        outgoing,
        legs=(
            outgoing.legs[0],
            replace(outgoing.legs[1], category=SemanticKey("expense")),
        ),
    )
    result = fold(_inputs(), _resolution(classified, incoming), (decision,), ())
    assert result.receipts[0].evaluation.outcome.value == "applied"
    assert not any(finding.blocking for finding in result.resolution.findings)


def test_transfer_shadowed_rule_findings_are_unique_and_canonically_ordered() -> None:
    revisions = tuple(
        RecordId(ENTITY, RecordKind.DECLARATION_REVISION, key * 64)
        for key in ("a", "b", "c", "d")
    )
    outgoing, incoming = _event("checking", "-15"), _event("card", "15")
    outgoing = replace(
        outgoing,
        legs=(
            outgoing.legs[0],
            replace(outgoing.legs[1], rules=(revisions[3], revisions[1])),
        ),
    )
    incoming = replace(
        incoming,
        legs=(
            incoming.legs[0],
            replace(incoming.legs[1], rules=(revisions[2], revisions[1], revisions[0])),
        ),
    )
    initial = _resolution(outgoing, incoming)
    result = fold(
        _inputs(),
        initial,
        (_decision(_transfer((outgoing,), (incoming,)), initial),),
        (),
    )
    assert result.resolution.findings == tuple(
        Finding(FindingCode.RULE_SHADOWED, revision, blocking=False)
        for revision in revisions
    )


def test_disjoint_source_legs_of_one_event_have_independent_transfer_ownership() -> (
    None
):
    first = _event("checking", "-10")
    other = _event("checking", "-5")
    source = replace(other.legs[0], key=SemanticKey("second-cash"))
    boundary = replace(
        other.legs[1], key=SemanticKey("boundary:second-cash"), source_leg=source.key
    )
    departure = replace(first, legs=(*first.legs, source, boundary))
    arrival_a, arrival_b = _event("card-a", "10"), _event("card-b", "5")
    initial = _resolution(departure, arrival_a, arrival_b)
    decision_a = _decision(_transfer((departure,), (arrival_a,)), initial)
    first_fold = fold(_inputs(), initial, (decision_a,), ())
    body_b = TransferBody(
        ENTITY,
        REVIEW,
        (child_id(departure.identity, RecordKind.LEG, source.key),),
        (_leg(arrival_b),),
        SemanticKey("USD"),
    )
    decision_b = _decision(body_b, first_fold.resolution, sequence=2)
    final = fold(_inputs(), initial, (decision_a, decision_b), ())
    assert not final.resolution.findings
    assert len(final.resolution.links) == 2
    assert {
        link.key: {(member.role, member.identity) for member in link.members}
        for link in final.resolution.links
    } == {
        decision_a.entry.key: {
            (MemberRole.OUTGOING, _leg(departure)),
            (MemberRole.INCOMING, _leg(arrival_a)),
        },
        decision_b.entry.key: {
            (
                MemberRole.OUTGOING,
                child_id(departure.identity, RecordKind.LEG, source.key),
            ),
            (MemberRole.INCOMING, _leg(arrival_b)),
        },
    }
    assert all(
        leg.kind == LegKind.POSITION
        for event in final.resolution.events
        for leg in event.legs
    )


def test_document_guard_requires_its_selected_archive_not_just_the_subject() -> None:
    purchase = _event("purchase", "-8")
    initial = _resolution(purchase)
    body = DocumentLinkBody(
        ENTITY, REVIEW, EventReviewRef(purchase.identity), ("e" * 64,)
    )
    decision = _decision(body, initial, documents=("e" * 64,))
    result = fold(_inputs(), initial, (decision,), ())
    assert result.receipts[0].evaluation.outcome.value == "missing"
    assert result.resolution.determinations[0].status == ResolutionStatus.BLOCKED
    assert result.resolution.links == ()


@pytest.mark.parametrize(
    ("state", "booked"),
    [
        (EventState.CANCELLED, "-2"),
        (EventState.PENDING, "-2"),
        (EventState.RECOGNIZED, "-102"),
    ],
)
def test_evidence_links_preserve_economics_without_inventing_failure_state(
    state: EventState, booked: str
) -> None:
    attempt = replace(_event("failed-attempt", "-100"), state=state)
    fee = _event("fee", "-2")
    initial = _resolution(attempt, fee)
    document = DocumentLinkBody(
        ENTITY, REVIEW, EventReviewRef(fee.identity), ("e" * 64,)
    )
    lifecycle = LifecycleLinkBody(
        ENTITY, REVIEW, EventReviewRef(attempt.identity), EventReviewRef(fee.identity)
    )
    result = fold(
        _inputs(),
        initial,
        (
            _decision(document, initial, documents=("e" * 64,)),
            _decision(lifecycle, initial, sequence=2),
        ),
        ("e" * 64,),
    )
    assert result.resolution.events == initial.events
    assert [link.kind.value for link in result.resolution.links] == [
        "document",
        "lifecycle",
    ]
    assert all(
        receipt.evaluation.outcome.value == "applied" for receipt in result.receipts
    )
    assert total(
        leg.amount
        for event in result.resolution.events
        if event.state == EventState.RECOGNIZED
        for leg in event.legs
        if leg.kind == LegKind.POSITION
    ) == ExactDecimal(booked, 0, None)
