import json
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.decisions import decode_decision_body, encode_decision_body
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    AssignBody,
    AssignmentDateRole,
    Body,
    DocumentLinkBody,
    EventDateAssignment,
    EventReviewAssignment,
    EventReviewRef,
    EventStateAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    EventTextField,
    IdentityBody,
    IdentityMode,
    LegCategoryAssignment,
    LegDateAssignment,
    LegReviewRef,
    LifecycleLinkBody,
    ReviewBasisRef,
    SplitBody,
    SplitChild,
    StatementRevisionBody,
    TransferBody,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.time import DateMode, LegDate

if TYPE_CHECKING:
    from collections.abc import Callable

ENTITY = EntityId("synthetic")
REVIEW = ReviewBasisRef("a" * 64, "b" * 64)
EVENT = EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "c" * 64))
LEG = LegReviewRef(RecordId(ENTITY, RecordKind.LEG, "d" * 64))
ASSIGN = AssignBody(
    ENTITY,
    REVIEW,
    EVENT,
    (
        EventTextAssignment(EventTextField.NARRATION, None),
        EventTextAssignment(EventTextField.PAYEE, " e\u0301\n\x00 "),
        EventDateAssignment(AssignmentDateRole.POSTED, date(2026, 1, 2)),
        EventReviewAssignment(ReviewState.UNREVIEWED),
        EventTagsAssignment(("", "é")),
        EventDateAssignment(AssignmentDateRole.VALUE, None),
    ),
)
SPLIT = SplitBody(
    ENTITY,
    REVIEW,
    LEG,
    (
        SplitChild(SemanticKey("a"), ExactDecimal("1", 0, 4)),
        SplitChild(SemanticKey("b"), ExactDecimal("-25", 2, None)),
    ),
)


@pytest.mark.parametrize(
    "body",
    [
        ASSIGN,
        SPLIT,
        TransferBody(ENTITY, REVIEW, (LEG.leg,), (), SemanticKey("NZD")),
        TransferBody(
            ENTITY,
            REVIEW,
            (),
            (LEG.leg, RecordId(ENTITY, RecordKind.LEG, "e" * 64)),
            SemanticKey("NZD"),
        ),
        DocumentLinkBody(ENTITY, REVIEW, EVENT, ("e" * 64, "f" * 64)),
        DocumentLinkBody(ENTITY, REVIEW, LEG, ("e" * 64,)),
        LifecycleLinkBody(
            ENTITY,
            REVIEW,
            EVENT,
            EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "e" * 64)),
        ),
        AssignBody(
            ENTITY, REVIEW, EVENT, (EventStateAssignment(EventState.CANCELLED),)
        ),
        IdentityBody(
            ENTITY,
            REVIEW,
            IdentityMode.KEY_FAMILY,
            RecordId(ENTITY, RecordKind.SOURCE_RECORD, "e" * 64),
            (
                RecordId(ENTITY, RecordKind.SOURCE_RECORD, "e" * 64),
                RecordId(ENTITY, RecordKind.SOURCE_RECORD, "f" * 64),
            ),
        ),
        StatementRevisionBody(
            ENTITY,
            REVIEW,
            ("e" * 64,),
            ("f" * 64,),
            SemanticKey("card"),
            FactKind.MOVEMENT,
            SemanticKey("posted"),
            date(2026, 1, 1),
            date(2026, 2, 1),
        ),
        *[
            AssignBody(
                ENTITY,
                REVIEW,
                LEG,
                (
                    LegCategoryAssignment(category),
                    LegDateAssignment(AssignmentDateRole.POSTED, value),
                ),
            )
            for category, value in (
                (None, LegDate(DateMode.INHERIT, None)),
                (SemanticKey("food"), LegDate(DateMode.UNKNOWN, None)),
                (None, LegDate(DateMode.VALUE, date(2026, 1, 2))),
            )
        ],
    ],
)
def test_live_variants_roundtrip_canonically_without_losing_precision(
    body: Body,
) -> None:
    wire = encode_decision_body(body)
    assert wire == encode_json(json.loads(wire), integer_strings=False)
    assert wire.endswith(b"\n")
    assert decode_decision_body(wire) == body


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document.update(kind="identity"),
        lambda document: document.update(kind="unknown"),
        lambda document: document.update(guards=[]),
        lambda document: document.update(schema_version=True),
        lambda document: document["review"].update(basis_digest="A" * 64),
        lambda document: document["target"].update(entity_id="other"),
        lambda document: document["target"].update(leg_id="e" * 64),
        lambda document: document.update(assignments=[{"kind": "cancel_event"}]),
        lambda document: document.update(assignments=[{"kind": "amount", "value": 1}]),
        lambda document: document["assignments"].reverse(),
        lambda document: document["assignments"].append(document["assignments"][0]),
    ],
)
def test_decode_rejects_unsupported_shapes_and_semantic_invalidity(
    change: Callable[[dict], object],
) -> None:
    document = json.loads(encode_decision_body(ASSIGN))
    change(document)
    with pytest.raises(ContractError):
        decode_decision_body(encode_json(document, integer_strings=False))


def test_wire_rejects_noncanonical_bytes_duplicate_keys_and_non_native_numbers() -> (
    None
):
    wire = encode_decision_body(ASSIGN)
    payloads = (
        wire[:-1],
        b" " + wire,
        wire.replace(b'"schema_version":1', b'"schema_version":1.0'),
        wire.replace(b'"kind":"assign"', b'"kind":"assign","kind":"assign"'),
        json.dumps(json.loads(wire), indent=2).encode(),
        b"\xff",
    )
    for payload in payloads:
        with pytest.raises(ContractError):
            decode_decision_body(payload)


def test_split_precision_is_sealed_and_child_order_is_not_normalized() -> None:
    wire = encode_decision_body(SPLIT)
    assert b'"coefficient":"1","scale":0,"source_scale":4' in wire
    child = replace(SPLIT.children[0], amount=ExactDecimal("1", 0, None))
    assert (
        encode_decision_body(replace(SPLIT, children=(child, SPLIT.children[1])))
        != wire
    )
    document = json.loads(wire)
    document["children"].reverse()
    with pytest.raises(ContractError):
        decode_decision_body(encode_json(document, integer_strings=False))


def test_encoder_revalidates_forged_frozen_values() -> None:
    body = replace(ASSIGN)
    object.__setattr__(body, "assignments", tuple(reversed(body.assignments)))
    with pytest.raises(ContractError):
        encode_decision_body(body)


@pytest.mark.parametrize(
    ("body", "change"),
    [
        (
            TransferBody(ENTITY, REVIEW, (LEG.leg,), (), SemanticKey("NZD")),
            lambda document: document.update(outgoing=[]),
        ),
        (
            TransferBody(ENTITY, REVIEW, (LEG.leg,), (), SemanticKey("NZD")),
            lambda document: document.update(incoming=document["outgoing"]),
        ),
        (
            TransferBody(ENTITY, REVIEW, (LEG.leg,), (), SemanticKey("NZD")),
            lambda document: document["outgoing"].append(document["outgoing"][0]),
        ),
        (
            DocumentLinkBody(ENTITY, REVIEW, LEG, ("e" * 64, "f" * 64)),
            lambda document: document["documents"].reverse(),
        ),
        (
            DocumentLinkBody(ENTITY, REVIEW, LEG, ("e" * 64,)),
            lambda document: document.update(documents=[]),
        ),
        (
            DocumentLinkBody(ENTITY, REVIEW, LEG, ("e" * 64,)),
            lambda document: document.update(link_kind="card_payment"),
        ),
        (
            LifecycleLinkBody(
                ENTITY,
                REVIEW,
                EVENT,
                EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "e" * 64)),
            ),
            lambda document: document.update(fee=document["attempt"]),
        ),
        (
            LifecycleLinkBody(
                ENTITY,
                REVIEW,
                EVENT,
                EventReviewRef(RecordId(ENTITY, RecordKind.EVENT, "e" * 64)),
            ),
            lambda document: document.update(
                fee={"entity_id": ENTITY.value, "leg_id": LEG.leg.value}
            ),
        ),
    ],
)
def test_transfer_and_link_wire_rejects_invalid_operands(
    body: Body,
    change: Callable[[dict], object],
) -> None:
    document = json.loads(encode_decision_body(body))
    change(document)
    with pytest.raises(ContractError):
        decode_decision_body(encode_json(document, integer_strings=False))
