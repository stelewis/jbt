"""Typed decisions pinned to retained native review evidence."""

# ruff: noqa: D105

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

from jbt.domain.errors import DomainError, IdentityError
from jbt.domain.guards import GuardReference
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey, validate_digest
from jbt.domain.journal import DecisionKind
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.time import LegDate

if TYPE_CHECKING:
    from collections.abc import Callable

_MIN_SPLIT_CHILDREN = 2


class DecisionError(DomainError):
    """A decision violates a located, payload-safe constraint."""


def _require(valid: bool, location: str, constraint: str) -> None:  # noqa: FBT001
    if not valid:
        raise DecisionError(constraint=constraint, location=location)


def _typed(value: object, expected: tuple[type, ...], location: str) -> None:
    _require(type(value) in expected, location, "exact_type_required")


def _digest(value: str, location: str) -> None:
    _typed(value, (str,), location)
    try:
        validate_digest(value, location)
    except IdentityError as error:
        raise DecisionError(error.constraint, error.location) from error


def _text(value: str, location: str) -> bytes:
    _typed(value, (str,), location)
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DecisionError(constraint="utf8_required", location=location) from error


def _items[T](values: tuple[T, ...], expected: tuple[type, ...], location: str) -> None:
    _typed(values, (tuple,), location)
    for value in values:
        _typed(value, expected, location)


def _ordered[T](
    values: tuple[T, ...], key: Callable[[T], bytes], location: str
) -> None:
    keys = tuple(map(key, values))
    _require(
        all(left < right for left, right in pairwise(keys)),
        location,
        "sorted_unique_required",
    )


@dataclass(frozen=True, slots=True)
class ReviewBasisRef:
    """Seals for the reviewed attempt and immutable expected basis."""

    attempt_digest: str
    basis_digest: str

    def __post_init__(self) -> None:
        _digest(self.attempt_digest, "review_basis_ref.attempt_digest")
        _digest(self.basis_digest, "review_basis_ref.basis_digest")


@dataclass(frozen=True, slots=True)
class EventReviewRef:
    """An entity-qualified EVENT guard address."""

    event: RecordId

    def __post_init__(self) -> None:
        _typed(self.event, (RecordId,), "event_review_ref")
        self.event.require(entity=self.event.entity, kind=RecordKind.EVENT)

    @property
    def guard_reference(self) -> GuardReference:
        """Use the native EVENT guard table, not a financial projection."""
        return GuardReference(
            self.event.entity, SemanticKey("review_events"), (self.event.value,)
        )


@dataclass(frozen=True, slots=True)
class LegReviewRef:
    """An entity-qualified LEG guard address."""

    leg: RecordId

    def __post_init__(self) -> None:
        _typed(self.leg, (RecordId,), "leg_review_ref")
        self.leg.require(entity=self.leg.entity, kind=RecordKind.LEG)

    @property
    def guard_reference(self) -> GuardReference:
        """Use the native LEG guard table, not a financial projection."""
        return GuardReference(
            self.leg.entity, SemanticKey("review_legs"), (self.leg.value,)
        )


def _common(entity: EntityId, review: ReviewBasisRef, location: str) -> None:
    _typed(entity, (EntityId,), f"{location}.entity")
    _typed(review, (ReviewBasisRef,), f"{location}.review")


class EventTextField(StrEnum):
    """Closed descriptive fields assignable to an event."""

    PAYEE = "payee"
    NARRATION = "narration"


class AssignmentDateRole(StrEnum):
    """Civil-date roles, without inheritance between roles."""

    AUTHORIZED = "authorized"
    TRADED = "traded"
    POSTED = "posted"
    SETTLED = "settled"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class EventTextAssignment:
    """Exact UTF-8 text or explicit absence; no key normalization."""

    field: EventTextField
    value: str | None

    def __post_init__(self) -> None:
        _typed(self.field, (EventTextField,), "event_text_assignment.field")
        if self.value is not None:
            _text(self.value, "event_text_assignment.value")


@dataclass(frozen=True, slots=True)
class EventTagsAssignment:
    """An immutable UTF-8-sorted unique set, including empty text."""

    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        _items(self.tags, (str,), "event_tags_assignment.tags")
        for tag in self.tags:
            _text(tag, "event_tags_assignment.tags")
        _ordered(
            self.tags, lambda tag: tag.encode("utf-8"), "event_tags_assignment.tags"
        )


@dataclass(frozen=True, slots=True)
class EventReviewAssignment:
    """Review disposition independent of economic participation."""

    state: ReviewState

    def __post_init__(self) -> None:
        _typed(self.state, (ReviewState,), "event_review_assignment.state")


@dataclass(frozen=True, slots=True)
class EventStateAssignment:
    """Reviewed cancellation cannot create source recognition or revision."""

    state: EventState

    def __post_init__(self) -> None:
        _typed(self.state, (EventState,), "event_state_assignment.state")
        _require(
            self.state == EventState.CANCELLED,
            "event_state_assignment.state",
            "cancellation_only",
        )


@dataclass(frozen=True, slots=True)
class EventDateAssignment:
    """An exact civil date or explicit absence, never a datetime."""

    role: AssignmentDateRole
    value: date | None

    def __post_init__(self) -> None:
        _typed(self.role, (AssignmentDateRole,), "event_date_assignment.role")
        if self.value is not None:
            _typed(self.value, (date,), "event_date_assignment.value")


@dataclass(frozen=True, slots=True)
class LegCategoryAssignment:
    """An entity-local category key or explicit absence."""

    category: SemanticKey | None

    def __post_init__(self) -> None:
        if self.category is not None:
            _typed(self.category, (SemanticKey,), "leg_category_assignment.category")


@dataclass(frozen=True, slots=True)
class LegDateAssignment:
    """An explicit posting date mode, distinct from nullable event dates."""

    role: AssignmentDateRole
    date: LegDate

    def __post_init__(self) -> None:
        _typed(self.role, (AssignmentDateRole,), "leg_date_assignment.role")
        _typed(self.date, (LegDate,), "leg_date_assignment.date")


type Assignment = (
    EventTextAssignment
    | EventTagsAssignment
    | EventReviewAssignment
    | EventStateAssignment
    | EventDateAssignment
    | LegCategoryAssignment
    | LegDateAssignment
)

_EVENT_ASSIGNMENTS = (
    EventTextAssignment,
    EventTagsAssignment,
    EventReviewAssignment,
    EventStateAssignment,
    EventDateAssignment,
)
_LEG_ASSIGNMENTS = (LegCategoryAssignment, LegDateAssignment)


def assignment_field(assignment: Assignment) -> str:
    """Derive the semantic field from the closed assignment operation."""
    _typed(assignment, (*_EVENT_ASSIGNMENTS, *_LEG_ASSIGNMENTS), "assignment")
    match assignment:
        case EventTextAssignment():
            return assignment.field.value
        case EventTagsAssignment():
            return "tags"
        case EventReviewAssignment():
            return "review_state"
        case EventStateAssignment():
            return "event_state"
        case EventDateAssignment() | LegDateAssignment():
            return f"{assignment.role.value}_date"
        case LegCategoryAssignment():
            return "category_id"


@dataclass(frozen=True, slots=True)
class AssignBody:
    """Target-specific ordered field changes, never arbitrary paths."""

    entity: EntityId
    review: ReviewBasisRef
    target: EventReviewRef | LegReviewRef
    assignments: tuple[Assignment, ...]

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "assign_body")
        _typed(self.target, (EventReviewRef, LegReviewRef), "assign_body.target")
        _require(
            self.target.guard_reference.entity == self.entity,
            "assign_body.target",
            "same_entity_required",
        )
        expected = (
            _EVENT_ASSIGNMENTS
            if type(self.target) is EventReviewRef
            else _LEG_ASSIGNMENTS
        )
        _items(self.assignments, expected, "assign_body.assignments")
        _require(bool(self.assignments), "assign_body.assignments", "nonempty_required")
        _ordered(
            self.assignments,
            lambda item: assignment_field(item).encode("utf-8"),
            "assign_body.assignments",
        )


@dataclass(frozen=True, slots=True)
class SplitChild:
    """Authored exact amount retaining stated precision."""

    child_key: SemanticKey
    amount: ExactDecimal

    def __post_init__(self) -> None:
        _typed(self.child_key, (SemanticKey,), "split_child.child_key")
        _typed(self.amount, (ExactDecimal,), "split_child.amount")


@dataclass(frozen=True, slots=True)
class SplitBody:
    """A structural split whose conservation needs retained parent facts."""

    entity: EntityId
    review: ReviewBasisRef
    parent: LegReviewRef
    children: tuple[SplitChild, ...]

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "split_body")
        _typed(self.parent, (LegReviewRef,), "split_body.parent")
        _require(
            self.parent.guard_reference.entity == self.entity,
            "split_body.parent",
            "same_entity_required",
        )
        _items(self.children, (SplitChild,), "split_body.children")
        _require(
            len(self.children) >= _MIN_SPLIT_CHILDREN,
            "split_body.children",
            "two_children_required",
        )
        _ordered(
            self.children,
            lambda child: child.child_key.value.encode("utf-8"),
            "split_body.children",
        )


class IdentityMode(StrEnum):
    """Persistent key correspondence or exact occurrence reassignment."""

    KEY_FAMILY = "key_family"
    OCCURRENCE = "occurrence"


def _records(entity: EntityId, records: tuple[RecordId, ...], location: str) -> None:
    _items(records, (RecordId,), location)
    _require(bool(records), location, "nonempty_required")
    for record in records:
        record.require(entity=entity, kind=RecordKind.SOURCE_RECORD)
    _ordered(records, lambda record: record.value.encode("ascii"), location)


@dataclass(frozen=True, slots=True)
class IdentityBody:
    """Explicit correspondence anchored at one reviewed source occurrence."""

    entity: EntityId
    review: ReviewBasisRef
    mode: IdentityMode
    canonical: RecordId
    members: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "identity_body")
        _typed(self.mode, (IdentityMode,), "identity_body.mode")
        _typed(self.canonical, (RecordId,), "identity_body.canonical")
        _records(self.entity, self.members, "identity_body.members")
        _require(
            self.canonical in self.members,
            "identity_body.canonical",
            "canonical_member_required",
        )


@dataclass(frozen=True, slots=True)
class StatementRevisionBody:
    """Reviewed replacement partitions one declared financial scope."""

    entity: EntityId
    review: ReviewBasisRef
    replaced: tuple[str, ...]
    replacement: tuple[str, ...]
    account: SemanticKey
    fact_kind: FactKind
    basis: SemanticKey
    start: date
    end: date

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "statement_revision_body")
        for name, values in (
            ("replaced", self.replaced),
            ("replacement", self.replacement),
        ):
            _items(values, (str,), f"statement_revision_body.{name}")
            _require(bool(values), name, "nonempty_required")
            for value in values:
                _digest(value, name)
            _ordered(values, lambda value: value.encode("ascii"), name)
        _require(
            not set(self.replaced) & set(self.replacement),
            "statement_revision_body",
            "disjoint_statements_required",
        )
        _typed(self.account, (SemanticKey,), "statement_revision_body.account")
        _typed(self.basis, (SemanticKey,), "statement_revision_body.basis")
        _typed(self.fact_kind, (FactKind,), "statement_revision_body.fact_kind")
        _require(
            self.fact_kind in {FactKind.MOVEMENT, FactKind.BALANCE},
            "statement_revision_body.fact_kind",
            "supported_fact_kind_required",
        )
        _typed(self.start, (date,), "statement_revision_body.start")
        _typed(self.end, (date,), "statement_revision_body.end")
        _require(self.start < self.end, "statement_revision_body", "nonempty_period")


@dataclass(frozen=True, slots=True)
class TransferBody:
    """Explicit source position legs; conservation belongs to booking."""

    entity: EntityId
    review: ReviewBasisRef
    outgoing: tuple[RecordId, ...]
    incoming: tuple[RecordId, ...]
    commodity: SemanticKey

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "transfer_body")
        _typed(self.commodity, (SemanticKey,), "transfer_body.commodity")
        for name, records in (("outgoing", self.outgoing), ("incoming", self.incoming)):
            location = f"transfer_body.{name}"
            _items(records, (RecordId,), location)
            for record in records:
                record.require(entity=self.entity, kind=RecordKind.LEG)
            _ordered(records, lambda record: record.value.encode("ascii"), location)
        _require(
            bool(self.outgoing or self.incoming), "transfer_body", "nonempty_required"
        )
        _require(
            not set(self.outgoing) & set(self.incoming),
            "transfer_body",
            "disjoint_legs_required",
        )


@dataclass(frozen=True, slots=True)
class DocumentLinkBody:
    """Archive-only blob evidence attached to a native event or leg."""

    entity: EntityId
    review: ReviewBasisRef
    target: EventReviewRef | LegReviewRef
    documents: tuple[str, ...]

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "document_link_body")
        _typed(self.target, (EventReviewRef, LegReviewRef), "document_link_body.target")
        _require(
            self.target.guard_reference.entity == self.entity,
            "document_link_body.target",
            "same_entity_required",
        )
        _items(self.documents, (str,), "document_link_body.documents")
        _require(
            bool(self.documents), "document_link_body.documents", "nonempty_required"
        )
        for digest in self.documents:
            _digest(digest, "document_link_body.documents")
        _ordered(
            self.documents,
            lambda value: value.encode("ascii"),
            "document_link_body.documents",
        )


@dataclass(frozen=True, slots=True)
class LifecycleLinkBody:
    """A reviewed assertion relating a failed attempt to its fee."""

    entity: EntityId
    review: ReviewBasisRef
    attempt: EventReviewRef
    fee: EventReviewRef

    def __post_init__(self) -> None:
        _common(self.entity, self.review, "lifecycle_link_body")
        for name, reference in (("attempt", self.attempt), ("fee", self.fee)):
            _typed(reference, (EventReviewRef,), f"lifecycle_link_body.{name}")
            _require(
                reference.guard_reference.entity == self.entity,
                f"lifecycle_link_body.{name}",
                "same_entity_required",
            )
        _require(
            self.attempt != self.fee, "lifecycle_link_body", "distinct_events_required"
        )


type StructuralBody = (
    IdentityBody
    | StatementRevisionBody
    | TransferBody
    | DocumentLinkBody
    | LifecycleLinkBody
)
type Body = AssignBody | SplitBody | StructuralBody


def decision_kind(body: Body) -> DecisionKind:
    """Derive custody kind only from an exact live body type."""
    if type(body) is AssignBody:
        return DecisionKind.ASSIGN
    if type(body) is SplitBody:
        return DecisionKind.SPLIT
    if type(body) is IdentityBody:
        return DecisionKind.IDENTITY
    if type(body) is StatementRevisionBody:
        return DecisionKind.STATEMENT_REVISION
    if type(body) is TransferBody:
        return DecisionKind.TRANSFER
    if type(body) in {DocumentLinkBody, LifecycleLinkBody}:
        return DecisionKind.LINK
    raise DecisionError(constraint="closed_body_required", location="decision.body")


def structural_references(
    body: StructuralBody,
) -> tuple[GuardReference, ...]:
    """Bind compound review operands to the exact sealed decision body."""
    match body:
        case IdentityBody():
            references = tuple(
                GuardReference(
                    body.entity, SemanticKey("review_sources"), (record.value,)
                )
                for record in body.members
            )
        case StatementRevisionBody():
            references = tuple(
                GuardReference(body.entity, SemanticKey("review_statements"), (value,))
                for value in (*body.replaced, *body.replacement)
            )
        case TransferBody():
            references = tuple(
                LegReviewRef(record).guard_reference
                for record in (*body.outgoing, *body.incoming)
            )
        case DocumentLinkBody():
            references = (
                body.target.guard_reference,
                *(
                    GuardReference(
                        body.entity, SemanticKey("review_documents"), (value,)
                    )
                    for value in body.documents
                ),
            )
        case LifecycleLinkBody():
            references = (body.attempt.guard_reference, body.fee.guard_reference)
        case _:
            raise DecisionError(
                constraint="structural_body_required", location="decision.body"
            )
    return tuple(
        sorted(references, key=lambda reference: (reference.table.value, reference.key))
    )
