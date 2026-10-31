from dataclasses import replace
from datetime import date

import pytest

from jbt.domain.identity import AcquiredAnchor, Occurrence, SourceKeyAnchor, event_id
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind, ReviewState
from jbt.domain.time import TimeRole
from jbt.model.proposals import IdentityProposal, TransferProposal, proposals
from jbt.model.quantity import (
    Determination,
    Event,
    EventKind,
    Leg,
    Resolution,
    ResolutionStatus,
    Support,
    SupportKind,
)


def _event(key: str, *, pending: bool, day: int) -> Event:
    entity = EntityId("synthetic")
    anchor = AcquiredAnchor(
        key * 64, SemanticKey("feed"), SemanticKey("card"), SemanticKey("synthetic:1")
    )
    occurrence = Occurrence(
        anchor
        if pending
        else SourceKeyAnchor(
            anchor.namespace, anchor.section, SemanticKey("fitid"), SemanticKey(key)
        ),
        SemanticKey("main"),
    )
    return Event(
        event_id(entity, occurrence),
        occurrence,
        date(2026, 1, day),
        EventKind.TRANSACTION,
        EventState.PENDING if pending else EventState.RECOGNIZED,
        ReviewState.UNREVIEWED,
        None,
        None,
        (
            Leg(
                SemanticKey("principal"),
                LegKind.POSITION,
                SemanticKey("USD"),
                ExactDecimal.parse("-5.00"),
                SemanticKey("card"),
                SemanticKey("claim"),
                None,
                None,
                None,
            ),
        ),
        (
            Support(
                RecordId(entity, RecordKind.SOURCE_RECORD, key * 64),
                SupportKind.SELECTED,
            ),
        ),
        None,
        effective_role=TimeRole.AUTHORIZED if pending else TimeRole.POSTED,
    )


def _resolution(*events: Event) -> Resolution:
    return Resolution(
        tuple(
            Determination(event.identity, ResolutionStatus.RESOLVED, event)
            for event in events
        ),
        (),
    )


def test_proposals_are_stable_bounded_unique_suggestions_not_correspondence() -> None:
    pending, posted = (
        _event("a", pending=True, day=10),
        _event("b", pending=False, day=17),
    )
    result = proposals(_resolution(pending, posted))
    assert len(result) == 1
    assert isinstance(result[0], IdentityProposal)
    assert result == proposals(_resolution(posted, pending))
    assert result[0].canonical == posted.support[0].source_record
    assert result[0].members == (
        pending.support[0].source_record,
        posted.support[0].source_record,
    )
    assert pending.identity != posted.identity
    revised = replace(
        posted,
        support=(
            Support(
                RecordId(posted.identity.entity, RecordKind.SOURCE_RECORD, "0" * 64),
                SupportKind.REPLACED,
            ),
            *posted.support,
        ),
    )
    assert proposals(_resolution(pending, revised)) == result
    blocked = _resolution(pending, posted)
    blocked = replace(
        blocked,
        determinations=(
            replace(blocked.determinations[0], status=ResolutionStatus.BLOCKED),
            blocked.determinations[1],
        ),
    )
    assert proposals(blocked) == ()
    assert (
        proposals(_resolution(pending, replace(posted, when=date(2026, 1, 18)))) == ()
    )
    assert (
        proposals(_resolution(pending, posted, _event("c", pending=False, day=11)))
        == ()
    )
    assert (
        proposals(_resolution(pending, posted, _event("d", pending=True, day=12))) == ()
    )


def test_transfer_candidates_include_exact_daily_aggregates_without_applying() -> None:
    outgoing = _event("a", pending=False, day=20)
    incoming = tuple(
        replace(
            event,
            legs=(
                replace(
                    event.legs[0],
                    account=SemanticKey("bank"),
                    amount=ExactDecimal.parse(amount),
                ),
            ),
        )
        for event, amount in (
            (_event("b", pending=False, day=27), "3"),
            (_event("c", pending=False, day=27), "2"),
        )
    )
    resolution = _resolution(outgoing, *incoming)
    candidates = [
        proposal
        for proposal in proposals(resolution)
        if isinstance(proposal, TransferProposal)
    ]
    assert len(candidates) == 1
    assert candidates[0].document()["kind"] == "transfer"
    assert len(candidates[0].outgoing) == 1
    assert len(candidates[0].incoming) == 2
    assert proposals(resolution) == proposals(_resolution(*reversed(resolution.events)))
    assert resolution.links == ()
    assert not proposals(
        _resolution(
            outgoing, *(replace(event, when=date(2026, 1, 28)) for event in incoming)
        )
    )


@pytest.mark.parametrize(
    ("outgoing_kind", "incoming_kind"),
    [
        (EventKind.OPENING, EventKind.OPENING),
        (EventKind.OPENING, EventKind.TRANSACTION),
        (EventKind.TRANSACTION, EventKind.OPENING),
    ],
)
def test_transfer_proposals_exclude_opening_balances(
    outgoing_kind: EventKind, incoming_kind: EventKind
) -> None:
    outgoing = replace(_event("a", pending=False, day=20), kind=outgoing_kind)
    incoming = _event("b", pending=False, day=20)
    incoming = replace(
        incoming,
        kind=incoming_kind,
        legs=(
            replace(
                incoming.legs[0],
                account=SemanticKey("bank"),
                amount=ExactDecimal.parse("5"),
            ),
        ),
    )
    assert proposals(_resolution(outgoing, incoming)) == ()
