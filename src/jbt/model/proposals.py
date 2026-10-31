"""Deterministic suggestions; correspondence always requires a decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from jbt.domain.canonical import encode_json
from jbt.domain.decisions import IdentityMode
from jbt.domain.identity import AcquiredAnchor, SourceKeyAnchor, child_id
from jbt.domain.ids import RecordKind
from jbt.domain.quantity import EventState, LegKind, total
from jbt.model.quantity import EventKind, ResolutionStatus, SupportKind

if TYPE_CHECKING:
    from datetime import date

    from jbt.domain.ids import RecordId, SemanticKey
    from jbt.domain.numbers import ExactDecimal
    from jbt.model.quantity import Event, Resolution

_POSTING_DISTANCE = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class IdentityProposal:
    """A stable proposal over exact source occurrences, never an application."""

    canonical: RecordId
    members: tuple[RecordId, ...]
    mode: IdentityMode
    reason: str

    def document(self) -> dict[str, object]:
        """Use command operands without fabricating a reviewed basis."""
        value = {
            "kind": "identity",
            "mode": self.mode.value,
            "canonical": self.canonical.value,
            "members": [member.value for member in self.members],
            "reason": self.reason,
        }
        return {
            "proposal_id": sha256(
                encode_json(value, integer_strings=False)
            ).hexdigest(),
            **value,
        }


def _identity_proposals(resolution: Resolution) -> tuple[IdentityProposal, ...]:
    """Suggest pending-to-posted matches without guessing through ambiguity."""
    groups: dict[str, list[Event]] = {}
    for determination in resolution.determinations:
        event = determination.event
        if (
            determination.status != ResolutionStatus.RESOLVED
            or event is None
            or event.state
            not in {
                EventState.PENDING,
                EventState.RECOGNIZED,
            }
        ):
            continue
        if not event.support:
            continue
        position_legs = tuple(leg for leg in event.legs if leg.kind == LegKind.POSITION)
        shape = [
            [
                leg.account.value if leg.account else None,
                leg.position.value if leg.position else None,
                leg.commodity.value,
                [leg.amount.coefficient, leg.amount.scale],
            ]
            for leg in position_legs
        ]
        key = encode_json(shape, integer_strings=False).decode()
        groups.setdefault(key, []).append(event)
    result = []
    for events in groups.values():
        pending = tuple(
            event
            for event in events
            if event.state == EventState.PENDING
            and isinstance(event.occurrence.anchor, AcquiredAnchor)
        )
        posted = tuple(
            event
            for event in events
            if event.state == EventState.RECOGNIZED
            and isinstance(event.occurrence.anchor, SourceKeyAnchor)
        )
        for authorization in pending:
            candidates = tuple(
                event
                for event in posted
                if timedelta(0) <= event.when - authorization.when <= _POSTING_DISTANCE
            )
            if len(candidates) != 1:
                continue
            event = candidates[0]
            competing = tuple(
                item
                for item in pending
                if timedelta(0) <= event.when - item.when <= _POSTING_DISTANCE
            )
            if len(competing) != 1:
                continue
            canonical = min(
                (
                    member
                    for member in event.support
                    if member.kind == SupportKind.SELECTED
                ),
                key=lambda member: member.source_record.value,
            ).source_record
            member = min(
                (
                    item
                    for item in authorization.support
                    if item.kind == SupportKind.SELECTED
                ),
                key=lambda item: item.source_record.value,
            ).source_record
            result.append(
                IdentityProposal(
                    canonical,
                    tuple(sorted((canonical, member), key=lambda value: value.value)),
                    IdentityMode.KEY_FAMILY,
                    "Unique same-position, same-quantity posting within "
                    "seven days of authorization.",
                )
            )
    return tuple(
        sorted(
            result,
            key=lambda proposal: (
                proposal.canonical.value,
                tuple(member.value for member in proposal.members),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class TransferProposal:
    """Opposite source aggregates that still require explicit human review."""

    outgoing: tuple[RecordId, ...]
    incoming: tuple[RecordId, ...]
    commodity: SemanticKey

    def document(self) -> dict[str, object]:
        """Publish command operands without applying financial treatment."""
        value = {
            "kind": "transfer",
            "outgoing": [member.value for member in self.outgoing],
            "incoming": [member.value for member in self.incoming],
            "commodity": self.commodity.value,
            "reason": "Opposite same-commodity source aggregates in different "
            "accounts within seven days.",
        }
        return {
            "proposal_id": sha256(
                encode_json(value, integer_strings=False)
            ).hexdigest(),
            **value,
        }


@dataclass(frozen=True, slots=True)
class _TransferSide:
    members: tuple[RecordId, ...]
    account: SemanticKey
    commodity: SemanticKey
    when: date
    amount: ExactDecimal


def _transfer_candidates(resolution: Resolution) -> tuple[_TransferSide, ...]:
    groups = {}
    for determination in resolution.determinations:
        event = determination.event
        if (
            determination.status != ResolutionStatus.RESOLVED
            or event is None
            or event.kind != EventKind.TRANSACTION
            or event.state != EventState.RECOGNIZED
        ):
            continue
        for leg in event.legs:
            if (
                leg.kind != LegKind.POSITION
                or leg.account is None
                or leg.source_leg is not None
                or leg.transfer is not None
                or not int(leg.amount.coefficient)
            ):
                continue
            key = (
                leg.account,
                leg.commodity,
                event.when,
                int(leg.amount.coefficient) > 0,
            )
            groups.setdefault(key, []).append(
                (child_id(event.identity, RecordKind.LEG, leg.key), leg.amount)
            )
    candidates = {}
    for key, members in groups.items():
        for selection in (*((member,) for member in members), tuple(members)):
            ids = tuple(
                sorted(
                    (member for member, _ in selection), key=lambda member: member.value
                )
            )
            candidates[ids] = _TransferSide(
                ids, key[0], key[1], key[2], total(amount for _, amount in selection)
            )
    return tuple(candidates.values())


def _transfer_proposals(resolution: Resolution) -> tuple[TransferProposal, ...]:
    candidates = _transfer_candidates(resolution)
    incoming_by_value_day = {}
    for incoming in candidates:
        if int(incoming.amount.coefficient) > 0:
            index = (
                incoming.commodity,
                incoming.amount.coefficient,
                incoming.amount.scale,
                incoming.when.toordinal(),
            )
            incoming_by_value_day.setdefault(index, []).append(incoming)
    result = []
    for outgoing in candidates:
        if int(outgoing.amount.coefficient) > 0:
            continue
        ordinal = outgoing.when.toordinal()
        for day in range(
            ordinal - _POSTING_DISTANCE.days,
            ordinal + _POSTING_DISTANCE.days + 1,
        ):
            index = (
                outgoing.commodity,
                str(-int(outgoing.amount.coefficient)),
                outgoing.amount.scale,
                day,
            )
            result.extend(
                TransferProposal(outgoing.members, incoming.members, outgoing.commodity)
                for incoming in incoming_by_value_day.get(index, ())
                if outgoing.account != incoming.account
            )
    return tuple(
        sorted(
            result,
            key=lambda proposal: (
                tuple(member.value for member in proposal.outgoing),
                tuple(member.value for member in proposal.incoming),
            ),
        )
    )


def proposals(
    resolution: Resolution,
) -> tuple[IdentityProposal | TransferProposal, ...]:
    """Suggest bounded correspondences; never mutate the resolved record."""
    return (*_identity_proposals(resolution), *_transfer_proposals(resolution))
