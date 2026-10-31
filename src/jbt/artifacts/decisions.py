"""Strict canonical wire boundary for guarded decision bodies."""

from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from jsonschema import ValidationError

from jbt.artifacts.integrity import ArtifactIntegrityError, decode_document
from jbt.contracts.primitives import ContractError as SurfaceContractError
from jbt.contracts.schemas import decision_body_schema, validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    AssignBody,
    Assignment,
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
    decision_kind,
)
from jbt.domain.errors import DomainError
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.time import DateMode, LegDate

if TYPE_CHECKING:
    from collections.abc import Callable


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SurfaceContractError(code="decision_body_object", location=location)
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise SurfaceContractError(code="decision_body_object", location=location)
        result[key] = item
    return result


def _text(value: object, location: str) -> str:
    if type(value) is not str:
        raise SurfaceContractError(code="decision_body_text", location=location)
    return value


def _integer(value: object, location: str) -> int:
    if type(value) is not int:
        raise SurfaceContractError(code="decision_body_integer", location=location)
    return value


def _items[T](
    value: object, location: str, read: Callable[[object, str], T]
) -> tuple[T, ...]:
    if type(value) is not list:
        raise SurfaceContractError(code="decision_body_array", location=location)
    return tuple(read(item, f"{location}[{index}]") for index, item in enumerate(value))


def _enum[T: StrEnum](value: object, location: str, kind: type[T]) -> T:
    try:
        return kind(_text(value, location))
    except ValueError as error:
        raise SurfaceContractError(
            code="decision_body_enum", location=location
        ) from error


def _nullable_date(value: object, location: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(_text(value, location))
    except ValueError as error:
        raise SurfaceContractError(
            code="decision_body_date", location=location
        ) from error


def _read_target(value: object, location: str) -> EventReviewRef | LegReviewRef:
    reference = _object(value, location)
    entity = EntityId(_text(reference["entity_id"], f"{location}.entity_id"))
    if "event_id" in reference:
        return EventReviewRef(
            RecordId(
                entity,
                RecordKind.EVENT,
                _text(reference["event_id"], f"{location}.event_id"),
            )
        )
    return LegReviewRef(
        RecordId(
            entity, RecordKind.LEG, _text(reference["leg_id"], f"{location}.leg_id")
        )
    )


def _read_assignment(value: object, location: str) -> Assignment:
    assignment = _object(value, location)
    match assignment["kind"]:
        case "event_text":
            text = assignment["value"]
            return EventTextAssignment(
                _enum(assignment["field"], f"{location}.field", EventTextField),
                None if text is None else _text(text, f"{location}.value"),
            )
        case "event_tags":
            return EventTagsAssignment(
                _items(assignment["tags"], f"{location}.tags", _text)
            )
        case "event_review":
            return EventReviewAssignment(
                _enum(assignment["state"], f"{location}.state", ReviewState)
            )
        case "event_state":
            return EventStateAssignment(
                _enum(assignment["state"], f"{location}.state", EventState)
            )
        case "event_date":
            return EventDateAssignment(
                _enum(assignment["role"], f"{location}.role", AssignmentDateRole),
                _nullable_date(assignment["value"], f"{location}.value"),
            )
        case "leg_category":
            category = assignment["category_id"]
            return LegCategoryAssignment(
                None
                if category is None
                else SemanticKey(_text(category, f"{location}.category_id"))
            )
        case "leg_date":
            leg_date = _object(assignment["date"], f"{location}.date")
            return LegDateAssignment(
                _enum(assignment["role"], f"{location}.role", AssignmentDateRole),
                LegDate(
                    _enum(leg_date["mode"], f"{location}.date.mode", DateMode),
                    _nullable_date(leg_date["value"], f"{location}.date.value"),
                ),
            )
        case _:
            raise SurfaceContractError(
                code="decision_body_assignment", location=location
            )


def _read_child(value: object, location: str) -> SplitChild:
    child = _object(value, location)
    amount = _object(child["amount"], f"{location}.amount")
    source_scale = amount["source_scale"]
    return SplitChild(
        SemanticKey(_text(child["child_key"], f"{location}.child_key")),
        ExactDecimal(
            _text(amount["coefficient"], f"{location}.amount.coefficient"),
            _integer(amount["scale"], f"{location}.amount.scale"),
            None
            if source_scale is None
            else _integer(source_scale, f"{location}.amount.source_scale"),
        ),
    )


def _read_body(document: dict[str, object]) -> Body:
    entity = EntityId(_text(document["entity_id"], "entity_id"))
    basis = _object(document["review"], "review")
    review = ReviewBasisRef(
        _text(basis["attempt_digest"], "review.attempt_digest"),
        _text(basis["basis_digest"], "review.basis_digest"),
    )
    if document["kind"] == "assign":
        return AssignBody(
            entity,
            review,
            _read_target(document["target"], "target"),
            _items(document["assignments"], "assignments", _read_assignment),
        )
    if document["kind"] == "identity":
        return IdentityBody(
            entity,
            review,
            _enum(document["mode"], "mode", IdentityMode),
            RecordId(
                entity,
                RecordKind.SOURCE_RECORD,
                _text(document["canonical"], "canonical"),
            ),
            _items(
                document["members"],
                "members",
                lambda value, location: RecordId(
                    entity, RecordKind.SOURCE_RECORD, _text(value, location)
                ),
            ),
        )
    if document["kind"] == "statement_revision":
        start = _nullable_date(document["start"], "start")
        end = _nullable_date(document["end"], "end")
        if start is None or end is None:
            raise SurfaceContractError(code="decision_body_period", location="period")
        return StatementRevisionBody(
            entity,
            review,
            _items(document["replaced"], "replaced", _text),
            _items(document["replacement"], "replacement", _text),
            SemanticKey(_text(document["account"], "account")),
            _enum(document["fact_kind"], "fact_kind", FactKind),
            SemanticKey(_text(document["basis"], "basis")),
            start,
            end,
        )
    if document["kind"] == "transfer":
        return TransferBody(
            entity,
            review,
            _items(
                document["outgoing"],
                "outgoing",
                lambda value, location: RecordId(
                    entity, RecordKind.LEG, _text(value, location)
                ),
            ),
            _items(
                document["incoming"],
                "incoming",
                lambda value, location: RecordId(
                    entity, RecordKind.LEG, _text(value, location)
                ),
            ),
            SemanticKey(_text(document["commodity"], "commodity")),
        )
    if document["kind"] == "link":
        if document["link_kind"] == "document":
            return DocumentLinkBody(
                entity,
                review,
                _read_target(document["target"], "target"),
                _items(document["documents"], "documents", _text),
            )
        attempt = _read_target(document["attempt"], "attempt")
        fee = _read_target(document["fee"], "fee")
        if type(attempt) is not EventReviewRef or type(fee) is not EventReviewRef:
            raise SurfaceContractError(code="decision_body_event", location="link")
        return LifecycleLinkBody(entity, review, attempt, fee)
    parent = _read_target(document["parent"], "parent")
    if type(parent) is not LegReviewRef:
        raise SurfaceContractError(code="decision_body_parent", location="parent")
    return SplitBody(
        entity,
        review,
        parent,
        _items(document["children"], "children", _read_child),
    )


def _write_target(reference: EventReviewRef | LegReviewRef) -> dict[str, object]:
    reference.__post_init__()
    match reference:
        case EventReviewRef():
            return {
                "entity_id": reference.event.entity.value,
                "event_id": reference.event.value,
            }
        case LegReviewRef():
            return {
                "entity_id": reference.leg.entity.value,
                "leg_id": reference.leg.value,
            }


def _write_assignment(assignment: Assignment) -> dict[str, object]:
    assignment.__post_init__()
    match assignment:
        case EventTextAssignment():
            return {
                "kind": "event_text",
                "field": assignment.field.value,
                "value": assignment.value,
            }
        case EventTagsAssignment():
            return {"kind": "event_tags", "tags": list(assignment.tags)}
        case EventReviewAssignment():
            return {"kind": "event_review", "state": assignment.state.value}
        case EventStateAssignment():
            return {"kind": "event_state", "state": assignment.state.value}
        case EventDateAssignment():
            return {
                "kind": "event_date",
                "role": assignment.role.value,
                "value": None
                if assignment.value is None
                else assignment.value.isoformat(),
            }
        case LegCategoryAssignment():
            return {
                "kind": "leg_category",
                "category_id": None
                if assignment.category is None
                else assignment.category.value,
            }
        case LegDateAssignment():
            return {
                "kind": "leg_date",
                "role": assignment.role.value,
                "date": {
                    "mode": assignment.date.mode.value,
                    "value": None
                    if assignment.date.value is None
                    else assignment.date.value.isoformat(),
                },
            }


def _write_body(body: Body) -> dict[str, object]:
    kind = decision_kind(body)
    body.__post_init__()
    body.review.__post_init__()
    common: dict[str, object] = {
        "schema_version": 1,
        "kind": kind.value,
        "entity_id": body.entity.value,
        "review": {
            "attempt_digest": body.review.attempt_digest,
            "basis_digest": body.review.basis_digest,
        },
    }
    match body:
        case AssignBody():
            return {
                **common,
                "target": _write_target(body.target),
                "assignments": [_write_assignment(item) for item in body.assignments],
            }
        case SplitBody():
            return {
                **common,
                "parent": _write_target(body.parent),
                "children": [
                    {
                        "child_key": child.child_key.value,
                        "amount": {
                            "coefficient": child.amount.coefficient,
                            "scale": child.amount.scale,
                            "source_scale": child.amount.source_scale,
                        },
                    }
                    for child in body.children
                ],
            }
        case IdentityBody():
            return {
                **common,
                "mode": body.mode.value,
                "canonical": body.canonical.value,
                "members": [record.value for record in body.members],
            }
        case StatementRevisionBody():
            return {
                **common,
                "replaced": list(body.replaced),
                "replacement": list(body.replacement),
                "account": body.account.value,
                "fact_kind": body.fact_kind.value,
                "basis": body.basis.value,
                "start": body.start.isoformat(),
                "end": body.end.isoformat(),
            }
        case TransferBody():
            return {
                **common,
                "outgoing": [record.value for record in body.outgoing],
                "incoming": [record.value for record in body.incoming],
                "commodity": body.commodity.value,
            }
        case DocumentLinkBody():
            return {
                **common,
                "link_kind": "document",
                "target": _write_target(body.target),
                "documents": list(body.documents),
            }
        case LifecycleLinkBody():
            return {
                **common,
                "link_kind": "lifecycle",
                "attempt": _write_target(body.attempt),
                "fee": _write_target(body.fee),
            }


def _validate(document: dict[str, object]) -> None:
    try:
        validate_document(document, decision_body_schema())
    except ValidationError as error:
        raise SurfaceContractError(
            code="decision_body_schema", location="decision_body"
        ) from error


def encode_decision_body(body: Body) -> bytes:
    """Validate constructors and schema before sealing canonical bytes."""
    try:
        document = _write_body(body)
        _validate(document)
        if _read_body(document) != body:
            raise SurfaceContractError(
                code="decision_body_value", location="decision_body"
            )
        return encode_json(document, integer_strings=False)
    except DomainError as error:
        raise SurfaceContractError(
            code="decision_body_domain", location=error.location
        ) from error


def decode_decision_body(payload: bytes) -> Body:
    """Reject noncanonical bytes and invalid or unsupported operations."""
    if type(payload) is not bytes:
        raise SurfaceContractError(code="decision_body_bytes", location="decision_body")
    try:
        document = decode_document(payload, "decision_body")
        _validate(document)
        if encode_json(document, integer_strings=False) != payload:
            raise SurfaceContractError(
                code="decision_body_canonical", location="decision_body"
            )
        return _read_body(document)
    except ArtifactIntegrityError as error:
        raise SurfaceContractError(
            code="decision_body_json", location="decision_body"
        ) from error
    except DomainError as error:
        raise SurfaceContractError(
            code="decision_body_domain", location=error.location
        ) from error
