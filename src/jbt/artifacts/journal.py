"""Strict canonical decoding of journal custody metadata."""

from jsonschema import ValidationError

from jbt.artifacts.integrity import decode_document
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import (
    journal_entry_schema,
    journal_head_schema,
    validate_document,
)
from jbt.domain.canonical import encode_json
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalHead,
    JournalOperation,
)


def _document(payload: bytes, schema: dict, location: str) -> dict[str, object]:
    document = decode_document(payload, location)
    try:
        validate_document(document, schema)
    except ValidationError as error:
        raise ContractError(code="journal_schema", location=location) from error
    if payload != encode_json(document, integer_strings=False):
        raise ContractError(code="journal_canonical", location=location)
    return document


def _text(document: dict[str, object], name: str) -> str:
    value = document[name]
    if not isinstance(value, str):
        raise ContractError(code="journal_text", location=name)
    return value


def _nullable_text(document: dict[str, object], name: str) -> str | None:
    value = document[name]
    if value is None:
        return None
    return _text(document, name)


def _sequence(document: dict[str, object]) -> int:
    value = document["sequence"]
    if type(value) is not int:
        raise ContractError(code="journal_sequence", location="sequence")
    return value


def decode_entry(payload: bytes) -> JournalEntry:
    """Decode one sealed header, not its separately retained decision body."""
    document = _document(payload, journal_entry_schema(), "journal_entry")
    return JournalEntry(
        sequence=_sequence(document),
        previous_entry_digest=_nullable_text(document, "previous_entry_digest"),
        entity=EntityId(_text(document, "entity_id")),
        key=SemanticKey(_text(document, "declaration_key")),
        kind=DecisionKind(_text(document, "kind")),
        operation=JournalOperation(_text(document, "operation")),
        prior_entry_digest=_nullable_text(document, "prior_entry_digest"),
        reviewed_attempt_digest=_text(document, "reviewed_attempt_digest"),
        reason=_text(document, "reason"),
        payload_digest=_nullable_text(document, "payload_digest"),
    )


def decode_head(payload: bytes) -> JournalHead:
    """Decode the exact committed prefix, including an explicitly empty head."""
    document = _document(payload, journal_head_schema(), "journal_head")
    return JournalHead(
        sequence=_sequence(document),
        entry_digest=_nullable_text(document, "entry_digest"),
    )
