"""Pure journal custody, pinned selection, and first-record logical order.

Payload digests bind separately retained financial decision bodies. This
module neither interprets those bodies nor evaluates their guards.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from hashlib import sha256

from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.errors import DomainError, IdentityError
from jbt.domain.identity import declaration_id, revision_id
from jbt.domain.ids import (
    EntityId,
    RecordId,
    RecordKind,
    SemanticKey,
    validate_digest,
)


class JournalError(DomainError):
    """Journal custody or structural history is invalid."""


class DecisionStage(IntEnum):
    """Ordered owning boundaries for reviewed decisions."""

    MERGE = 0
    ENRICH = 1
    LINK = 2


class DecisionKind(StrEnum):
    """Closed financial decision variants, without payload interpretation."""

    ASSIGN = "assign"
    SPLIT = "split"
    IDENTITY = "identity"
    STATEMENT_REVISION = "statement_revision"
    TRANSFER = "transfer"
    LINK = "link"

    @property
    def stage(self) -> DecisionStage:
        """Select the boundary at which the decision's operands are consumed."""
        if self in {DecisionKind.IDENTITY, DecisionKind.STATEMENT_REVISION}:
            return DecisionStage.MERGE
        if self in {DecisionKind.ASSIGN, DecisionKind.SPLIT}:
            return DecisionStage.ENRICH
        return DecisionStage.LINK


class JournalOperation(StrEnum):
    """Append-only changes to a declaration's active revision."""

    RECORD = "record"
    SUPERSEDE = "supersede"
    RETRACT = "retract"


def _sequence(value: int, *, minimum: int, location: str) -> None:
    if type(value) is not int or not minimum <= value < 2**63:
        raise JournalError(constraint="int64_sequence_required", location=location)


def _digest(value: str, location: str) -> None:
    try:
        validate_digest(value, location)
    except IdentityError as error:
        raise JournalError(error.constraint, error.location) from error


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """An immutable custody header binding a separately retained body."""

    sequence: int
    previous_entry_digest: str | None
    entity: EntityId
    key: SemanticKey
    kind: DecisionKind
    operation: JournalOperation
    prior_entry_digest: str | None
    reviewed_attempt_digest: str
    reason: str
    payload_digest: str | None

    def __post_init__(self) -> None:
        """Reject coercions and contradictory operation/header shapes."""
        _sequence(self.sequence, minimum=1, location="journal_entry.sequence")
        if (
            not isinstance(self.entity, EntityId)
            or not isinstance(self.key, SemanticKey)
            or not isinstance(self.kind, DecisionKind)
            or not isinstance(self.operation, JournalOperation)
        ):
            raise JournalError(
                constraint="typed_entry_required", location="journal_entry"
            )
        if (self.sequence == 1) != (self.previous_entry_digest is None):
            raise JournalError(
                constraint="previous_entry_digest_required",
                location="journal_entry.previous_entry_digest",
            )
        if self.previous_entry_digest is not None:
            _digest(self.previous_entry_digest, "journal_entry.previous_entry_digest")
        if (self.operation is JournalOperation.RECORD) != (
            self.prior_entry_digest is None
        ):
            raise JournalError(
                constraint="operation_prior_required",
                location="journal_entry.prior_entry_digest",
            )
        if self.prior_entry_digest is not None:
            _digest(self.prior_entry_digest, "journal_entry.prior_entry_digest")
        _digest(self.reviewed_attempt_digest, "journal_entry.reviewed_attempt_digest")
        if type(self.reason) is not str or not self.reason:
            raise JournalError(
                constraint="nonempty_reason_required", location="journal_entry.reason"
            )
        try:
            self.reason.encode("utf-8")
        except UnicodeEncodeError as error:
            raise JournalError(
                constraint="utf8_required", location="journal_entry.reason"
            ) from error
        if (self.operation is JournalOperation.RETRACT) != (
            self.payload_digest is None
        ):
            raise JournalError(
                constraint="operation_payload_required",
                location="journal_entry.payload_digest",
            )
        if self.payload_digest is not None:
            _digest(self.payload_digest, "journal_entry.payload_digest")

    @property
    def declaration_id(self) -> RecordId:
        """Use the existing stable declaration identity namespace."""
        return declaration_id(self.entity, RecordKind.DECLARATION, self.key)

    @property
    def revision_id(self) -> RecordId:
        """Bind the entire canonical header in the owning revision contract."""
        return revision_id(
            self.entity,
            self.key,
            effective_from=None,
            effective_to=None,
            payload=_entry_document(self),
        )


def _entry_document(entry: JournalEntry) -> dict[str, JsonValue]:
    if not isinstance(entry, JournalEntry):
        raise JournalError(constraint="typed_entry_required", location="journal_entry")
    return {
        "schema_version": 1,
        "sequence": entry.sequence,
        "previous_entry_digest": entry.previous_entry_digest,
        "entity_id": entry.entity.value,
        "declaration_key": entry.key.value,
        "kind": entry.kind.value,
        "operation": entry.operation.value,
        "prior_entry_digest": entry.prior_entry_digest,
        "reviewed_attempt_digest": entry.reviewed_attempt_digest,
        "reason": entry.reason,
        "payload_digest": entry.payload_digest,
    }


def encode_entry(entry: JournalEntry) -> bytes:
    """Encode the closed custody header as exact LF-terminated JSON."""
    return encode_json(_entry_document(entry), integer_strings=False)


def entry_digest(entry: JournalEntry) -> str:
    """Hash canonical header bytes, excluding the derived digest itself."""
    return sha256(encode_entry(entry)).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalHead:
    """An explicit commit point, or the empty journal at sequence zero."""

    sequence: int
    entry_digest: str | None

    def __post_init__(self) -> None:
        """Require a digest exactly when the head names an entry."""
        _sequence(self.sequence, minimum=0, location="journal_head.sequence")
        if (self.sequence == 0) != (self.entry_digest is None):
            raise JournalError(
                constraint="head_digest_required", location="journal_head.entry_digest"
            )
        if self.entry_digest is not None:
            _digest(self.entry_digest, "journal_head.entry_digest")


def encode_head(head: JournalHead) -> bytes:
    """Encode the closed commit reference as exact LF-terminated JSON."""
    if not isinstance(head, JournalHead):
        raise JournalError(constraint="typed_head_required", location="journal_head")
    return encode_json(
        {
            "schema_version": 1,
            "sequence": head.sequence,
            "entry_digest": head.entry_digest,
        },
        integer_strings=False,
    )


@dataclass(frozen=True, slots=True)
class ActiveDecision:
    """An active custody entry at its original record's logical position."""

    entry: JournalEntry
    logical_position: int

    def __post_init__(self) -> None:
        """Require a non-retraction revision and an earlier record position."""
        if not isinstance(self.entry, JournalEntry):
            raise JournalError(
                constraint="typed_entry_required", location="active_decision.entry"
            )
        _sequence(
            self.logical_position,
            minimum=1,
            location="active_decision.logical_position",
        )
        if (
            self.entry.operation is JournalOperation.RETRACT
            or self.logical_position > self.entry.sequence
            or (
                self.entry.operation is JournalOperation.RECORD
                and self.logical_position != self.entry.sequence
            )
        ):
            raise JournalError(
                constraint="active_revision_position_required",
                location="active_decision",
            )


@dataclass(frozen=True, slots=True)
class JournalSelection:
    """Committed history, its active fold, and explicitly unselected appends."""

    head: JournalHead
    committed_entries: tuple[JournalEntry, ...]
    active_decisions: tuple[ActiveDecision, ...]
    unselected_entries: tuple[JournalEntry, ...]

    def __post_init__(self) -> None:
        """Keep the returned selection and every nested value immutable."""
        if not isinstance(self.head, JournalHead):
            raise JournalError(
                constraint="typed_head_required", location="journal_selection.head"
            )
        for name, values, item_type in (
            ("committed_entries", self.committed_entries, JournalEntry),
            ("active_decisions", self.active_decisions, ActiveDecision),
            ("unselected_entries", self.unselected_entries, JournalEntry),
        ):
            if type(values) is not tuple or any(
                not isinstance(value, item_type) for value in values
            ):
                raise JournalError(
                    constraint="immutable_typed_selection_required",
                    location=f"journal_selection.{name}",
                )


def _chain_digests(entries: tuple[JournalEntry, ...]) -> tuple[str, ...]:
    digests: list[str] = []
    previous: str | None = None
    for sequence, entry in enumerate(entries, start=1):
        if entry.sequence != sequence:
            raise JournalError(
                constraint="contiguous_sequence_required",
                location=f"journal.entries[{sequence - 1}].sequence",
            )
        if entry.previous_entry_digest != previous:
            raise JournalError(
                constraint="previous_entry_digest_mismatch",
                location=f"journal.entries[{sequence - 1}].previous_entry_digest",
            )
        previous = entry_digest(entry)
        digests.append(previous)
    return tuple(digests)


def _check_head(head: JournalHead, digests: tuple[str, ...], *, location: str) -> None:
    if head.sequence > len(digests):
        raise JournalError(constraint="head_entry_missing", location=location)
    if head.sequence and head.entry_digest != digests[head.sequence - 1]:
        raise JournalError(constraint="head_digest_mismatch", location=location)


def _fold(
    entries: tuple[JournalEntry, ...], digests: tuple[str, ...]
) -> tuple[ActiveDecision, ...]:
    recorded: set[tuple[EntityId, SemanticKey]] = set()
    active: dict[tuple[EntityId, SemanticKey], ActiveDecision] = {}
    for entry in entries:
        key = (entry.entity, entry.key)
        location = f"journal.entries[{entry.sequence - 1}]"
        if entry.operation is JournalOperation.RECORD:
            if key in recorded:
                raise JournalError(constraint="duplicate_record_key", location=location)
            recorded.add(key)
            active[key] = ActiveDecision(entry, entry.sequence)
            continue
        prior = active.get(key)
        if (
            prior is None
            or entry.prior_entry_digest != digests[prior.entry.sequence - 1]
        ):
            raise JournalError(
                constraint="immediately_active_prior_required",
                location=f"{location}.prior_entry_digest",
            )
        if entry.kind is not prior.entry.kind:
            raise JournalError(
                constraint="same_decision_kind_required", location=f"{location}.kind"
            )
        if entry.operation is JournalOperation.RETRACT:
            del active[key]
        else:
            active[key] = ActiveDecision(entry, prior.logical_position)
    return tuple(sorted(active.values(), key=lambda item: item.logical_position))


def select_journal(
    entries: tuple[JournalEntry, ...],
    *,
    head: JournalHead,
    retained_heads: tuple[JournalHead, ...],
) -> JournalSelection:
    """Validate custody and fold only the pinned committed prefix.

    Every supplied entry must form one physical chain. Retained heads must
    belong to this selection's history; historical replay supplies only its
    applicable retained heads, never reads or reconciles live state.
    """
    if type(entries) is not tuple or any(
        not isinstance(entry, JournalEntry) for entry in entries
    ):
        raise JournalError(
            constraint="immutable_typed_entries_required", location="journal.entries"
        )
    if not isinstance(head, JournalHead):
        raise JournalError(constraint="typed_head_required", location="journal.head")
    if type(retained_heads) is not tuple or any(
        not isinstance(retained, JournalHead) for retained in retained_heads
    ):
        raise JournalError(
            constraint="immutable_typed_heads_required",
            location="journal.retained_heads",
        )
    digests = _chain_digests(entries)
    _check_head(head, digests, location="journal.head")
    for index, retained in enumerate(retained_heads):
        location = f"journal.retained_heads[{index}]"
        if retained.sequence > head.sequence:
            raise JournalError(
                constraint="retained_head_extension_required", location=location
            )
        _check_head(retained, digests, location=location)
    committed = entries[: head.sequence]
    return JournalSelection(
        head=head,
        committed_entries=committed,
        active_decisions=_fold(committed, digests),
        unselected_entries=entries[head.sequence :],
    )
