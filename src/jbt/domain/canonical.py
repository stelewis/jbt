"""Canonical pure-value encodings for typed records and JSON documents.

Each value is a one-byte tag, ASCII byte length, colon, and payload.
NFC text uses UTF-8; integers use minimal ASCII decimal; null is distinct
from empty text. Lists retain order and multiplicity. Records retain
declared field order, reject duplicate names, and carry a domain/version.
There are no implicit maps, floating-point numbers, or line terminators.

JSON documents use a separate encoding: NFC text, sorted object keys,
compact separators, exact decimal integer literals, and one final LF.
Numeric coefficient strings retain their string type; no types are coerced.
"""

import json
from datetime import date
from hashlib import sha256
from unicodedata import normalize

from jbt.domain.errors import CanonicalError
from jbt.domain.numbers import ExactDecimal

type CanonicalValue = (
    bool | int | str | date | ExactDecimal | tuple[CanonicalValue, ...] | None
)
type JsonValue = bool | int | str | list[JsonValue] | dict[str, JsonValue] | None
_FIELD_ARITY = 2
_MAX_JSON_DEPTH = 64


def _frame(tag: bytes, payload: bytes) -> bytes:
    return tag + str(len(payload)).encode("ascii") + b":" + payload


def encode(value: CanonicalValue) -> bytes:
    """Encode a supported typed value, rejecting coercions and mutable lists."""
    if value is None:
        return b"n0:"
    if type(value) is bool:
        return b"b1:1" if value else b"b1:0"
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise CanonicalError(
                constraint="int64_required", location="canonical.integer"
            )
        return _frame(b"i", str(value).encode("ascii"))
    if isinstance(value, str):
        try:
            payload = normalize("NFC", value).encode("utf-8")
        except UnicodeEncodeError as error:
            raise CanonicalError(
                constraint="utf8_required", location="canonical.text"
            ) from error
        return _frame(b"s", payload)
    if type(value) is date:
        return _frame(b"d", value.isoformat().encode("ascii"))
    if isinstance(value, ExactDecimal):
        return _frame(
            b"x", encode((value.coefficient, value.scale, value.source_scale))
        )
    if isinstance(value, tuple):
        return _frame(b"l", b"".join(encode(item) for item in value))
    raise CanonicalError(constraint="unsupported_value_type", location="canonical")


def encode_record(domain: str, fields: tuple[tuple[str, CanonicalValue], ...]) -> bytes:
    """Encode a domain-separated record with explicit ordered typed fields."""
    if not isinstance(domain, str) or not domain:
        raise CanonicalError(constraint="domain_required", location="canonical.record")
    if not isinstance(fields, tuple) or any(
        not isinstance(field, tuple)
        or len(field) != _FIELD_ARITY
        or not isinstance(field[0], str)
        for field in fields
    ):
        raise CanonicalError(
            constraint="ordered_typed_fields_required", location="canonical.record"
        )
    names = tuple(normalize("NFC", name) for name, _ in fields)
    if any(not name for name in names) or len(set(names)) != len(names):
        raise CanonicalError(
            constraint="unique_field_names_required", location="canonical.record"
        )
    return b"JBT\x00" + encode((domain, 1, fields))


def digest_record(domain: str, fields: tuple[tuple[str, CanonicalValue], ...]) -> str:
    """Hash versioned canonical bytes with SHA-256."""
    return sha256(encode_record(domain, fields)).hexdigest()


def _json_text(value: str) -> str:
    normalized = normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CanonicalError(
            constraint="utf8_required", location="canonical.json"
        ) from error
    return normalized


def _json_object(
    value: dict[object, object], active: set[int], depth: int, *, integer_strings: bool
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CanonicalError(
                constraint="json_string_key_required", location="canonical.json"
            )
        normalized = _json_text(key)
        if normalized in result:
            raise CanonicalError(
                constraint="normalized_key_collision", location="canonical.json"
            )
        result[normalized] = _json_value(
            item, active, depth + 1, integer_strings=integer_strings
        )
    return result


def _json_value(
    value: object, active: set[int], depth: int, *, integer_strings: bool
) -> JsonValue:
    if depth > _MAX_JSON_DEPTH:
        raise CanonicalError(constraint="json_nesting_limit", location="canonical.json")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise CanonicalError(constraint="int64_required", location="canonical.json")
        return str(value) if integer_strings else value
    if isinstance(value, str):
        return _json_text(value)
    if not isinstance(value, dict | list):
        raise CanonicalError(
            constraint="json_value_required", location="canonical.json"
        )
    identity = id(value)
    if identity in active:
        raise CanonicalError(
            constraint="cyclic_json_container", location="canonical.json"
        )
    active.add(identity)
    try:
        if isinstance(value, list):
            return [
                _json_value(item, active, depth + 1, integer_strings=integer_strings)
                for item in value
            ]
        return _json_object(value, active, depth, integer_strings=integer_strings)
    finally:
        active.remove(identity)


def encode_json(value: object, *, integer_strings: bool) -> bytes:
    """Encode a validated JSON-compatible value as NFC UTF-8 plus one LF.

    Object keys sort by Unicode scalar value, equivalently UTF-8 bytes.
    Lists retain order and duplicates. Integers are signed INT64 decimal
    literals or decimal strings, as explicitly selected by the caller.
    Wider financial coefficients must already be strings. Row and manifest
    encodings select strings; typed declaration payloads select literals.
    Floats, nonstring keys, cycles, and nesting beyond 64 levels fail.
    No dates, domain types, tuples, or other objects are silently coerced.
    """
    if type(integer_strings) is not bool:
        raise CanonicalError(
            constraint="integer_encoding_required", location="canonical.json"
        )
    normalized = _json_value(value, set(), 0, integer_strings=integer_strings)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")
