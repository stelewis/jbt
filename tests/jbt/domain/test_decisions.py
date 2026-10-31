from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest

from jbt.domain.decisions import (
    AssignBody,
    AssignmentDateRole,
    DecisionError,
    DocumentLinkBody,
    EventDateAssignment,
    EventReviewAssignment,
    EventReviewRef,
    EventStateAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    EventTextField,
    LegCategoryAssignment,
    LegDateAssignment,
    LegReviewRef,
    LifecycleLinkBody,
    ReviewBasisRef,
    SplitBody,
    SplitChild,
    TransferBody,
    assignment_field,
    decision_kind,
    structural_references,
)
from jbt.domain.errors import IdentityError
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.journal import DecisionKind
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, ReviewState
from jbt.domain.time import DateMode, LegDate

ENTITY = EntityId("synthetic")
REVIEW = ReviewBasisRef("a" * 64, "b" * 64)
EVENT = EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "c" * 64))
LEG = LegReviewRef(RecordId(ENTITY, RecordKind.LEG, "d" * 64))
TEXT = EventTextAssignment(EventTextField.PAYEE, " e\u0301\n\x00 ")
CHILDREN = (
    SplitChild(SemanticKey("a"), ExactDecimal("1", 0, 4)),
    SplitChild(SemanticKey("b"), ExactDecimal("-25", 2, None)),
)


def test_live_bodies_are_immutable_and_derive_their_journal_kind() -> None:
    assign = AssignBody(ENTITY, REVIEW, EVENT, (TEXT,))
    split = SplitBody(ENTITY, REVIEW, LEG, CHILDREN)
    assert decision_kind(assign) is DecisionKind.ASSIGN
    assert decision_kind(split) is DecisionKind.SPLIT
    assert split.children[0].amount == ExactDecimal("1", 0, 4)
    with pytest.raises(FrozenInstanceError):
        assign.entity = ENTITY  # ty: ignore[invalid-assignment]
    with pytest.raises(DecisionError, match="closed_body_required"):
        decision_kind(object())  # ty: ignore[invalid-argument-type]


def test_participation_assignment_only_cancels_without_manufacturing_source_state() -> (
    None
):
    assignment = EventStateAssignment(EventState.CANCELLED)
    assert assignment_field(assignment) == "event_state"
    for state in (EventState.RECOGNIZED, EventState.PENDING, EventState.RETRACTED):
        with pytest.raises(DecisionError, match="cancellation_only"):
            EventStateAssignment(state)


def test_target_refs_preserve_qualified_identity_and_guard_address() -> None:
    assert EVENT.guard_reference.entity == ENTITY
    assert EVENT.guard_reference.table == SemanticKey("review_events")
    assert EVENT.guard_reference.key == (EVENT.event.value,)
    assert LEG.guard_reference.table == SemanticKey("review_legs")
    assert LEG.guard_reference.key == (LEG.leg.value,)
    with pytest.raises(IdentityError):
        EventReviewRef(LEG.leg)
    with pytest.raises(IdentityError):
        LegReviewRef(EVENT.event)
    with pytest.raises(DecisionError, match="exact_type_required"):
        EventReviewRef("c" * 64)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "body",
    [
        AssignBody(ENTITY, REVIEW, EVENT, (TEXT,)),
        SplitBody(ENTITY, REVIEW, LEG, CHILDREN),
    ],
)
def test_body_operands_must_belong_to_the_owning_entity(body: object) -> None:
    with pytest.raises(DecisionError, match="same_entity_required"):
        replace(body, entity=EntityId("other"))  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "a" * 64 + "\n"])
def test_review_seals_are_exact_lowercase_sha256(digest: str) -> None:
    with pytest.raises(DecisionError):
        ReviewBasisRef(digest, REVIEW.basis_digest)
    with pytest.raises(DecisionError):
        ReviewBasisRef(REVIEW.attempt_digest, digest)


def test_assignments_have_closed_fields_and_target_specific_vocabulary() -> None:
    assignments = (
        EventTextAssignment(EventTextField.NARRATION, None),
        TEXT,
        EventDateAssignment(AssignmentDateRole.POSTED, date(2026, 1, 2)),
        EventReviewAssignment(ReviewState.IGNORED),
        EventTagsAssignment(("", "é")),
    )
    body = AssignBody(ENTITY, REVIEW, EVENT, assignments)
    assert tuple(map(assignment_field, body.assignments)) == (
        "narration",
        "payee",
        "posted_date",
        "review_state",
        "tags",
    )
    assert TEXT.value == " e\u0301\n\x00 "
    with pytest.raises(DecisionError, match="exact_type_required"):
        AssignBody(ENTITY, REVIEW, LEG, (TEXT,))
    with pytest.raises(DecisionError, match="exact_type_required"):
        AssignBody(ENTITY, REVIEW, EVENT, (LegCategoryAssignment(None),))
    with pytest.raises(DecisionError):
        EventReviewAssignment("ignored")  # ty: ignore[invalid-argument-type]
    with pytest.raises(DecisionError):
        EventTextAssignment(EventTextField.PAYEE, "\ud800")


def test_assignment_order_duplicates_empty_and_mutability_are_rejected() -> None:
    tags = EventTagsAssignment(("a", "b"))
    for assignments in ((tags, TEXT), (TEXT, TEXT), ()):
        with pytest.raises(DecisionError):
            AssignBody(ENTITY, REVIEW, EVENT, assignments)
    with pytest.raises(DecisionError):
        AssignBody(ENTITY, REVIEW, EVENT, [TEXT])  # ty: ignore[invalid-argument-type]
    for values in (("b", "a"), ("a", "a")):
        with pytest.raises(DecisionError, match="sorted_unique_required"):
            EventTagsAssignment(values)


def test_civil_date_assignments_do_not_conflate_leg_modes_or_coerce_datetime() -> None:
    role = AssignmentDateRole.POSTED
    modes = (
        LegDate(DateMode.INHERIT, None),
        LegDate(DateMode.UNKNOWN, None),
        LegDate(DateMode.VALUE, date(2026, 1, 2)),
    )
    values = tuple(LegDateAssignment(role, value) for value in modes)
    assert len(set(values)) == 3
    for value in values:
        assert AssignBody(ENTITY, REVIEW, LEG, (value,)).assignments == (value,)
    assert EventDateAssignment(role, None).value is None
    with pytest.raises(DecisionError):
        EventDateAssignment(role, datetime(2026, 1, 2, tzinfo=UTC))


def test_split_keys_require_two_sorted_unique_children_not_conservation() -> None:
    for children in (CHILDREN[:1], tuple(reversed(CHILDREN)), (CHILDREN[0],) * 2):
        with pytest.raises(DecisionError):
            SplitBody(ENTITY, REVIEW, LEG, children)
    assert SplitBody(ENTITY, REVIEW, LEG, CHILDREN).children == CHILDREN
    with pytest.raises(DecisionError):
        SplitBody(ENTITY, REVIEW, EVENT, CHILDREN)  # ty: ignore[invalid-argument-type]


def test_transfer_owns_sorted_unique_disjoint_source_legs() -> None:
    other = RecordId(ENTITY, RecordKind.LEG, "e" * 64)
    body = TransferBody(ENTITY, REVIEW, (LEG.leg,), (other,), SemanticKey("NZD"))
    assert decision_kind(body) == DecisionKind.TRANSFER
    assert [reference.table.value for reference in structural_references(body)] == [
        "review_legs",
        "review_legs",
    ]
    for outgoing, incoming in (
        ((), ()),
        ((LEG.leg,), (LEG.leg,)),
        ((other, LEG.leg), ()),
        ((LEG.leg, LEG.leg), ()),
    ):
        with pytest.raises(DecisionError):
            replace(body, outgoing=outgoing, incoming=incoming)
    with pytest.raises(IdentityError):
        replace(body, outgoing=(EVENT.event,))
    with pytest.raises(IdentityError):
        replace(body, entity=EntityId("other"))


def test_links_bind_native_refs_and_sorted_blob_digest_addresses() -> None:
    document = DocumentLinkBody(ENTITY, REVIEW, LEG, ("e" * 64, "f" * 64))
    fee = EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "e" * 64))
    lifecycle = LifecycleLinkBody(ENTITY, REVIEW, EVENT, fee)
    assert decision_kind(document) == decision_kind(lifecycle) == DecisionKind.LINK
    references = structural_references(document)
    assert [(reference.table.value, reference.key) for reference in references] == [
        ("review_documents", ("e" * 64,)),
        ("review_documents", ("f" * 64,)),
        ("review_legs", (LEG.leg.value,)),
    ]
    for documents in ((), ("e" * 64,) * 2, ("f" * 64, "e" * 64), ("A" * 64,)):
        with pytest.raises(DecisionError):
            replace(document, documents=documents)
    for body in (document, lifecycle):
        with pytest.raises(DecisionError, match="same_entity_required"):
            replace(body, entity=EntityId("other"))
    with pytest.raises(DecisionError, match="distinct_events_required"):
        replace(lifecycle, fee=EVENT)
    with pytest.raises(DecisionError, match="exact_type_required"):
        replace(lifecycle, fee=LEG)
