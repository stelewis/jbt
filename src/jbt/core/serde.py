"""Canonical serialization and content digest primitives."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

type CanonicalScalar = None | bool | int | Decimal | str | date | datetime
type CanonicalValue = (
    CanonicalScalar | Mapping[str, "CanonicalValue"] | Sequence["CanonicalValue"]
)


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented canonically."""


def canonical_bytes(value: CanonicalValue) -> bytes:
    """Serialize a supported value to canonical UTF-8 JSON with one trailing LF."""

    return (_encode(value) + "\n").encode("utf-8")


def sha256_digest(value: bytes) -> str:
    """Return a qualified SHA-256 content digest."""

    return f"sha256:{sha256(value).hexdigest()}"


def _encode(value: CanonicalValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise CanonicalizationError("binary floating-point values are forbidden")
    if isinstance(value, Decimal):
        return _encode_decimal(value)
    if isinstance(value, datetime):
        return _encode_string(_encode_datetime(value))
    if isinstance(value, date):
        return _encode_string(value.isoformat())
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, Mapping):
        return _encode_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    raise CanonicalizationError(f"unsupported value type: {type(value).__name__}")


def _encode_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _encode_mapping(value: Mapping[str, CanonicalValue]) -> str:
    normalized: dict[str, CanonicalValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CanonicalizationError("mapping keys must be strings")
        canonical_key = unicodedata.normalize("NFC", key)
        if canonical_key in normalized:
            raise CanonicalizationError(
                f"mapping keys collide after Unicode normalization: {canonical_key!r}"
            )
        normalized[canonical_key] = item

    members = (
        f"{_encode_string(key)}:{_encode(normalized[key])}"
        for key in sorted(normalized)
    )
    return "{" + ",".join(members) + "}"


def _encode_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalizationError("non-finite decimal values are forbidden")
    if value.is_zero():
        return "0"

    sign, digit_tuple, exponent = value.as_tuple()
    digits = list(digit_tuple)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        encoded = coefficient + ("0" * exponent)
    else:
        decimal_index = len(coefficient) + exponent
        if decimal_index > 0:
            encoded = coefficient[:decimal_index] + "." + coefficient[decimal_index:]
        else:
            encoded = "0." + ("0" * -decimal_index) + coefficient
    return "-" + encoded if sign else encoded


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("datetimes must include a UTC offset")
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat().replace("+00:00", "Z")