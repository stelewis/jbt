"""Independent closed decision values, not guard or application authority."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from tests.integration.conformance.consumer_arithmetic import NumericError, read_number
from tests.integration.conformance.reader import ReaderError

KINDS = ("assign", "split", "identity", "statement_revision", "transfer", "link")
DATE_ROLES = ("authorized", "traded", "posted", "settled", "value")
type Address = tuple[str, str, tuple[str, ...]]


@dataclass(frozen=True)
class Body:
    kind: str
    entity_id: str
    wire: bytes
    digest: str


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise ReaderError(message)


def canonical_wire(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _object(value: object, names: set[str]) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == names, "closed object fields")
    return cast("dict[str, Any]", value)


def _text(value: object, *, identifier: bool = False) -> str:
    _require(type(value) is str, "text required")
    text = cast("str", value)
    try:
        text.encode("utf-8")
    except UnicodeError as error:
        message = "invalid UTF8"
        raise ReaderError(message) from error
    if identifier:
        _require(
            bool(text)
            and unicodedata.normalize("NFC", text) == text
            and not any(unicodedata.category(char) == "Cc" for char in text),
            "strict identifier required",
        )
    return text


def _enum(value: object, choices: tuple[str, ...]) -> str:
    result = _text(value)
    _require(result in choices, "closed enum")
    return result


def _digest(value: object) -> str:
    text = _text(value)
    _require(re.fullmatch(r"[0-9a-f]{64}", text) is not None, "lowercase digest")
    return text


def _date(value: object) -> str:
    text = _text(value)
    _require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is not None, "ISO date")
    try:
        date.fromisoformat(text)
    except ValueError as error:
        message = "invalid civil date"
        raise ReaderError(message) from error
    return text


def _entity(value: object, entity: str) -> str:
    result = _text(value, identifier=True)
    _require(result == entity, "cross-entity operand")
    return result


def _list(value: object, *, minimum: int = 0) -> list[Any]:
    _require(type(value) is list and len(value) >= minimum, "array cardinality")
    return cast("list[Any]", value)


def _ordered(keys: list[Any]) -> None:
    _require(keys == sorted(keys) and len(keys) == len(set(keys)), "sorted unique keys")


def reference_address(value: object, entity: str) -> Address:
    _require(type(value) is dict, "reference object")
    obj = cast("dict[str, Any]", value)
    _entity(obj.get("entity_id"), entity)
    fields = set(obj)
    if fields == {"entity_id", "event_id"}:
        table, key = "review_events", (_digest(obj["event_id"]),)
    elif fields == {"entity_id", "leg_id"}:
        table, key = "review_legs", (_digest(obj["leg_id"]),)
    else:
        message = "closed reference shape"
        raise ReaderError(message)
    return entity, table, key


def _ref(value: object, entity: str, tables: tuple[str, ...]) -> Address:
    address = reference_address(value, entity)
    _require(address[1] in tables, "operand reference kind")
    return address


def _nullable_date(value: object) -> None:
    if value is not None:
        _date(value)


def _assignment(value: object, table: str) -> str:
    _require(type(value) is dict, "assignment object")
    obj = cast("dict[str, Any]", value)
    kind = _enum(
        obj.get("kind"),
        ("event_text", "event_tags", "event_review", "event_state", "event_date")
        if table == "review_events"
        else ("leg_category", "leg_date"),
    )
    if kind == "event_text":
        _object(obj, {"kind", "field", "value"})
        field = _enum(obj["field"], ("payee", "narration"))
        if obj["value"] is not None:
            _text(obj["value"])
        return field
    if kind == "event_tags":
        _object(obj, {"kind", "tags"})
        tags = [_text(item) for item in _list(obj["tags"])]
        _ordered(tags)
        return "tags"
    if kind == "event_review":
        _object(obj, {"kind", "state"})
        _enum(obj["state"], ("reviewed", "unreviewed", "ignored"))
        return "review_state"
    if kind == "event_state":
        _object(obj, {"kind", "state"})
        _enum(obj["state"], ("cancelled",))
        return "event_state"
    if kind == "event_date":
        _object(obj, {"kind", "role", "value"})
        role = _enum(obj["role"], DATE_ROLES)
        _nullable_date(obj["value"])
        return role + "_date"
    if kind == "leg_category":
        _object(obj, {"kind", "category_id"})
        if obj["category_id"] is not None:
            _text(obj["category_id"], identifier=True)
        return "category_id"
    _object(obj, {"kind", "role", "date"})
    role = _enum(obj["role"], DATE_ROLES)
    choice = _object(obj["date"], {"mode", "value"})
    mode = _enum(choice["mode"], ("inherit", "value", "unknown"))
    _require((mode == "value") == (choice["value"] is not None), "date mode/value")
    _nullable_date(choice["value"])
    return role + "_date"


def _assign(obj: dict[str, Any], entity: str) -> None:
    target = _ref(obj["target"], entity, ("review_events", "review_legs"))
    fields = [
        _assignment(item, target[1]) for item in _list(obj["assignments"], minimum=1)
    ]
    _ordered(fields)


def _split(obj: dict[str, Any], entity: str) -> None:
    _ref(obj["parent"], entity, ("review_legs",))
    keys = []
    for value in _list(obj["children"], minimum=2):
        child = _object(value, {"child_key", "amount"})
        keys.append(_text(child["child_key"], identifier=True))
        amount = _object(child["amount"], {"coefficient", "scale", "source_scale"})
        _require(amount["coefficient"] is not None, "required amount")
        try:
            read_number(amount["coefficient"], amount["scale"], amount["source_scale"])
        except NumericError as error:
            message = "invalid exact split amount"
            raise ReaderError(message) from error
    _ordered(keys)


def _identity(obj: dict[str, Any], _entity_id: str) -> None:
    _enum(obj["mode"], ("key_family", "occurrence"))
    members = [_digest(value) for value in _list(obj["members"], minimum=1)]
    _ordered(members)
    _require(_digest(obj["canonical"]) in members, "canonical member")


def _revision(obj: dict[str, Any], _entity_id: str) -> None:
    replaced = [_digest(value) for value in _list(obj["replaced"], minimum=1)]
    replacement = [_digest(value) for value in _list(obj["replacement"], minimum=1)]
    _ordered(replaced)
    _ordered(replacement)
    _require(not set(replaced) & set(replacement), "disjoint revision evidence")
    _text(obj["account"], identifier=True)
    _text(obj["basis"], identifier=True)
    _enum(obj["fact_kind"], ("movement", "balance"))
    _require(_date(obj["start"]) < _date(obj["end"]), "nonempty revision period")


def _transfer(obj: dict[str, Any], _entity_id: str) -> None:
    outgoing = [_digest(value) for value in _list(obj["outgoing"])]
    incoming = [_digest(value) for value in _list(obj["incoming"])]
    _ordered(outgoing)
    _ordered(incoming)
    _require(bool(outgoing or incoming), "nonempty transfer")
    _require(not set(outgoing) & set(incoming), "disjoint transfer legs")
    _text(obj["commodity"], identifier=True)


def _link(obj: dict[str, Any], entity: str) -> None:
    if obj["link_kind"] == "document":
        _ref(obj["target"], entity, ("review_events", "review_legs"))
        documents = [_digest(value) for value in _list(obj["documents"], minimum=1)]
        _ordered(documents)
    else:
        attempt = _ref(obj["attempt"], entity, ("review_events",))
        fee = _ref(obj["fee"], entity, ("review_events",))
        _require(attempt != fee, "distinct lifecycle events")


def parse_body(value: object) -> Body:
    _require(type(value) is dict, "body object")
    obj = cast("dict[str, Any]", value)
    kind = _enum(obj.get("kind"), KINDS)
    fields = {
        "assign": {"target", "assignments"},
        "split": {"parent", "children"},
        "identity": {"mode", "canonical", "members"},
        "statement_revision": {
            "replaced",
            "replacement",
            "account",
            "fact_kind",
            "basis",
            "start",
            "end",
        },
        "transfer": {"outgoing", "incoming", "commodity"},
    }
    if kind == "link":
        link_kind = _enum(obj.get("link_kind"), ("document", "lifecycle"))
        fields["link"] = (
            {"link_kind", "target", "documents"}
            if link_kind == "document"
            else {"link_kind", "attempt", "fee"}
        )
    _object(obj, {"schema_version", "kind", "entity_id", "review"} | fields[kind])
    _require(
        type(obj["schema_version"]) is int and obj["schema_version"] == 1,
        "native version 1",
    )
    entity = _text(obj["entity_id"], identifier=True)
    review = _object(obj["review"], {"attempt_digest", "basis_digest"})
    _digest(review["attempt_digest"])
    _digest(review["basis_digest"])
    {
        "assign": _assign,
        "split": _split,
        "identity": _identity,
        "statement_revision": _revision,
        "transfer": _transfer,
        "link": _link,
    }[kind](obj, entity)
    wire = canonical_wire(obj)
    return Body(kind, entity, wire, hashlib.sha256(wire).hexdigest())


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _reject_number(_value: str) -> object:
    message = "non-native JSON integer"
    raise ReaderError(message)


def read_decision_body(payload: bytes) -> Body:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        body = parse_body(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        message = "invalid decision body wire"
        raise ReaderError(message) from error
    _require(payload == body.wire, "noncanonical body bytes")
    return body
