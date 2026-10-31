"""Canonical retained native-model review evidence and guard receipts."""

# Exception arguments are stable codes, not formatted messages.
# ruff: noqa: EM101
from __future__ import annotations

from copy import deepcopy
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING

from jsonschema import ValidationError

from jbt.artifacts.integrity import decode_document
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.schemas import review_report_schema, validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.guards import (
    FieldKind,
    GuardField,
    GuardReference,
    GuardTarget,
)
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.review import compound_candidates

if TYPE_CHECKING:
    from collections.abc import Mapping


def _document(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError("review_object", "review")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ContractError("review_object", "review")
        result[key] = item
    return result


def _text(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ContractError("review_text", location)
    return value


def write_target(target: GuardTarget) -> dict[str, object]:
    """Retain explicit schema types, identity, activity and physical values."""
    return {
        "entity_id": target.reference.entity.value,
        "table": target.reference.table.value,
        "key": list(target.reference.key),
        "active": target.active,
        "fields": [
            {
                "name": field.name,
                "kind": field.kind,
                "nullable": field.nullable,
                "value": field.json_value(),
            }
            for field in target.fields
        ],
    }


def _field(value: dict[str, object]) -> GuardField:
    require(
        set(value) == {"name", "kind", "nullable", "value"},
        "basis_field_schema",
        "basis",
    )
    kind = _text(value["kind"], "kind")
    kinds: dict[str, FieldKind] = {
        "string": "string",
        "integer": "integer",
        "boolean": "boolean",
        "date": "date",
        "list": "list",
    }
    require(kind in kinds, "basis_field_schema", "basis")
    nullable = value["nullable"]
    if not isinstance(nullable, bool):
        raise ContractError("basis_field_schema", "basis")
    item = value["value"]
    if kind == "list":
        if not isinstance(item, list):
            raise ContractError("basis_field_value", "basis")
        item = tuple(_text(part, "basis.field") for part in item)
    elif kind == "date" and item is not None:
        try:
            item = date.fromisoformat(_text(item, "date"))
        except ValueError as error:
            raise ContractError("basis_field_value", "basis") from error
    if item is not None and not isinstance(item, str | int | bool | date | tuple):
        raise ContractError("basis_field_value", "basis")
    try:
        return GuardField(_text(value["name"], "name"), kinds[kind], nullable, item)
    except ValueError as error:
        raise ContractError("basis_field_value", "basis") from error


def read_target(value: object) -> GuardTarget:
    """Decode a closed guard wire; the model owns profile completeness."""
    record = _document(value)
    require(
        set(record) == {"entity_id", "table", "key", "active", "fields"},
        "basis_target_schema",
        "basis",
    )
    key = record["key"]
    active = record["active"]
    if not isinstance(key, list) or len(key) != 1 or not isinstance(active, bool):
        raise ContractError("basis_target_schema", "basis")
    fields = record["fields"]
    if not isinstance(fields, list):
        raise ContractError("basis_fields", "basis")
    return GuardTarget(
        GuardReference(
            EntityId(_text(record["entity_id"], "entity")),
            SemanticKey(_text(record["table"], "table")),
            tuple(_text(part, "key") for part in key),
        ),
        tuple(_field(_document(item)) for item in fields),
        active,
    )


def review_boundary(value: object, key: str | None) -> tuple[GuardTarget, ...]:
    """Reconstruct reviewed pre-entry state from one tail and reverse changes."""
    review = _document(value)
    tail = review["tail"]
    receipts = review["receipts"]
    if not isinstance(tail, list) or not isinstance(receipts, list):
        raise ContractError("review_boundary_schema", "review")
    inventory: dict[GuardReference, tuple[GuardTarget, ...]] = {}
    for item in tail:
        target = read_target(item)
        inventory[target.reference] = (*inventory.get(target.reference, ()), target)
    result = tuple(target for group in inventory.values() for target in group)
    found = key is None
    keys: set[str] = set()
    for item in reversed(receipts):
        receipt = _document(item)
        decision_key = _text(receipt["decision_key"], "decision_key")
        require(decision_key not in keys, "review_boundary_duplicate", "review")
        keys.add(decision_key)
        changes = receipt["restore"]
        if not isinstance(changes, list):
            raise ContractError("review_boundary_schema", "review")
        references: set[GuardReference] = set()
        for change_value in changes:
            change = _document(change_value)
            address = change["key"]
            candidates = change["candidates"]
            if (
                set(change) != {"entity_id", "table", "key", "candidates"}
                or not isinstance(address, list)
                or len(address) != 1
                or not isinstance(candidates, list)
            ):
                raise ContractError("review_boundary_schema", "review")
            reference = GuardReference(
                EntityId(_text(change["entity_id"], "entity_id")),
                SemanticKey(_text(change["table"], "table")),
                tuple(_text(part, "key") for part in address),
            )
            restored = tuple(read_target(target) for target in candidates)
            require(
                reference not in references
                and all(target.reference == reference for target in restored)
                and restored != inventory.get(reference, ()),
                "review_boundary_change",
                "review",
            )
            references.add(reference)
            if restored:
                inventory[reference] = restored
            else:
                inventory.pop(reference, None)
        expected = read_target(receipt["expected"])
        candidates = receipt["candidates"]
        observed = (
            compound_candidates(
                expected,
                tuple(target for group in inventory.values() for target in group),
            )
            if expected.reference.table.value == "review_merge"
            else inventory.get(expected.reference, ())
        )
        require(
            isinstance(candidates, list)
            and tuple(read_target(target) for target in candidates) == observed,
            "review_boundary_receipt",
            "review",
        )
        if decision_key == key:
            found = True
            result = tuple(target for group in inventory.values() for target in group)
    require(found, "review_boundary_missing", "review")
    return result


def encode_review_report(value: Mapping[str, object]) -> bytes:
    """Validate the complete report shape before its immutable installation."""
    try:
        validate_document(dict(value), review_report_schema())
    except ValidationError as error:
        raise ContractError("review_report_schema", "review.report") from error
    return encode_json(dict(value), integer_strings=False)


def decode_review_report(payload: bytes) -> dict[str, object]:
    """Refuse alternate encodings and schema-invalid retained reports."""
    return deepcopy(_validated_report(payload))


@lru_cache(maxsize=16)
def _validated_report(payload: bytes) -> dict[str, object]:
    value = decode_document(payload, "review.report")
    require(
        encode_review_report(value) == payload,
        "review_report_canonical",
        "review.report",
    )
    return value
