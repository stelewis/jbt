import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from tests.integration.conformance.reader import ReaderError, canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_SEQUENCE = 2**63 - 1
KINDS = frozenset(
    {
        "assign",
        "split",
        "identity",
        "statement_revision",
        "transfer",
        "link",
    }
)
OPERATIONS = frozenset({"record", "supersede", "retract"})


@dataclass(frozen=True)
class Entry:
    schema_version: int
    sequence: int
    previous_entry_digest: str | None
    entity_id: str
    declaration_key: str
    kind: str
    operation: str
    prior_entry_digest: str | None
    reviewed_attempt_digest: str
    reason: str
    payload_digest: str | None
    digest: str


@dataclass(frozen=True)
class Head:
    schema_version: int
    sequence: int
    entry_digest: str | None


@dataclass(frozen=True)
class ActiveDecision:
    logical_position: int
    entry: Entry


@dataclass(frozen=True)
class JournalObservation:
    head: Head
    selected: tuple[Entry, ...]
    active: tuple[ActiveDecision, ...]
    unselected: tuple[Entry, ...]


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise ReaderError(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _reject_number(_value: str) -> object:
    message = "non-integer JSON number"
    raise ReaderError(message)


def _object(payload: bytes, keys: set[str]) -> dict[str, object]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        message = "invalid journal JSON"
        raise ReaderError(message) from error
    _require(isinstance(value, dict), "expected journal object")
    _require(set(value) == keys, "journal keys differ from closed contract")
    try:
        encoded = canonical_bytes(value, integer_strings=False)
    except (UnicodeError, ValueError, RecursionError) as error:
        message = "invalid canonical journal value"
        raise ReaderError(message) from error
    _require(encoded == payload, "noncanonical journal bytes")
    return cast("dict[str, object]", value)


def _integer(value: object, *, minimum: int) -> int:
    _require(
        type(value) is int and minimum <= value <= MAX_SEQUENCE,
        "invalid native int64",
    )
    return cast("int", value)


def _digest(value: object) -> str:
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "invalid SHA256 digest",
    )
    return cast("str", value)


def _identifier(value: object) -> str:
    _require(
        isinstance(value, str)
        and bool(value)
        and unicodedata.normalize("NFC", value) == value
        and not any(unicodedata.category(char) == "Cc" for char in value),
        "invalid journal identifier",
    )
    return cast("str", value)


def _enum(value: object, allowed: frozenset[str]) -> str:
    _require(isinstance(value, str) and value in allowed, "invalid journal enum")
    return cast("str", value)


def read_entry(payload: bytes) -> Entry:
    value = _object(
        payload,
        {
            "schema_version",
            "sequence",
            "previous_entry_digest",
            "entity_id",
            "declaration_key",
            "kind",
            "operation",
            "prior_entry_digest",
            "reviewed_attempt_digest",
            "reason",
            "payload_digest",
        },
    )
    version = _integer(value["schema_version"], minimum=1)
    _require(version == 1, "unsupported journal schema")
    sequence = _integer(value["sequence"], minimum=1)
    previous = value["previous_entry_digest"]
    if sequence == 1:
        _require(previous is None, "first entry has previous digest")
    else:
        previous = _digest(previous)
    operation = _enum(value["operation"], OPERATIONS)
    prior = value["prior_entry_digest"]
    if operation == "record":
        _require(prior is None, "record has prior digest")
    else:
        prior = _digest(prior)
    body = value["payload_digest"]
    if operation == "retract":
        _require(body is None, "retract has payload digest")
    else:
        body = _digest(body)
    reason = value["reason"]
    _require(isinstance(reason, str) and bool(reason), "empty descriptive reason")
    return Entry(
        version,
        sequence,
        cast("str | None", previous),
        _identifier(value["entity_id"]),
        _identifier(value["declaration_key"]),
        _enum(value["kind"], KINDS),
        operation,
        cast("str | None", prior),
        _digest(value["reviewed_attempt_digest"]),
        cast("str", reason),
        cast("str | None", body),
        hashlib.sha256(payload).hexdigest(),
    )


def read_head(payload: bytes) -> Head:
    value = _object(payload, {"schema_version", "sequence", "entry_digest"})
    version = _integer(value["schema_version"], minimum=1)
    _require(version == 1, "unsupported head schema")
    sequence = _integer(value["sequence"], minimum=0)
    digest = value["entry_digest"]
    if sequence == 0:
        _require(digest is None, "empty head has entry digest")
    else:
        digest = _digest(digest)
    return Head(version, sequence, cast("str | None", digest))


def _fold(entries: tuple[Entry, ...]) -> tuple[ActiveDecision, ...]:
    # A slot survives retraction: local keys cannot be reused to move history.
    slots: dict[tuple[str, str], ActiveDecision | None] = {}
    for entry in entries:
        key = (entry.entity_id, entry.declaration_key)
        if entry.operation == "record":
            _require(key not in slots, "declaration key already established")
            slots[key] = ActiveDecision(entry.sequence, entry)
            continue
        current = slots.get(key)
        _require(current is not None, "prior decision unknown or inactive")
        assert current is not None
        _require(
            current.entry.digest == entry.prior_entry_digest
            and current.entry.kind == entry.kind,
            "prior decision is not the immediately active same-kind entry",
        )
        slots[key] = (
            None
            if entry.operation == "retract"
            else ActiveDecision(current.logical_position, entry)
        )
    return tuple(slot for slot in slots.values() if slot is not None)


def read_journal(
    entries: Sequence[bytes],
    head: bytes,
    *,
    bodies: Mapping[str, bytes],
    retained_heads: Sequence[bytes] = (),
) -> JournalObservation:
    """Prove custody and logical order, without interpreting sealed bodies."""
    parsed = tuple(read_entry(payload) for payload in entries)
    previous = None
    for sequence, entry in enumerate(parsed, 1):
        _require(entry.sequence == sequence, "physical sequence gap or reorder")
        _require(entry.previous_entry_digest == previous, "broken journal link")
        previous = entry.digest
        if entry.payload_digest is not None:
            body = bodies.get(entry.payload_digest)
            _require(body is not None, "missing retained body")
            assert body is not None
            _require(
                hashlib.sha256(body).hexdigest() == entry.payload_digest,
                "retained body seal mismatch",
            )
    selected_head = read_head(head)
    for candidate in (selected_head, *(read_head(raw) for raw in retained_heads)):
        _require(candidate.sequence <= selected_head.sequence, "retained head rollback")
        _require(candidate.sequence <= len(parsed), "head entry missing")
        expected = (
            None if candidate.sequence == 0 else parsed[candidate.sequence - 1].digest
        )
        _require(candidate.entry_digest == expected, "head digest mismatch")
    prefix = parsed[: selected_head.sequence]
    return JournalObservation(
        selected_head, prefix, _fold(prefix), parsed[selected_head.sequence :]
    )
