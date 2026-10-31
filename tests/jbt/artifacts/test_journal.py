import json
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import ArtifactIntegrityError
from jbt.artifacts.journal import decode_entry, decode_head
from jbt.contracts.primitives import ContractError
from jbt.domain.ids import EntityId, RecordKind, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalHead,
    JournalOperation,
    encode_entry,
    encode_head,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def entry_document() -> dict:
    return {
        "schema_version": 1,
        "sequence": 1,
        "previous_entry_digest": None,
        "entity_id": "household",
        "declaration_key": "reviewed-split",
        "kind": "split",
        "operation": "record",
        "prior_entry_digest": None,
        "reviewed_attempt_digest": "a" * 64,
        "reason": " e\u0301\n\x00 ",
        "payload_digest": "b" * 64,
    }


def _bytes(document: dict) -> bytes:
    return (
        json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()


@pytest.mark.parametrize("kind", list(DecisionKind))
def test_entry_round_trips_independent_canonical_bytes(
    entry_document: dict,
    kind: DecisionKind,
) -> None:
    entry_document["kind"] = kind.value
    payload = _bytes(entry_document)
    entry = decode_entry(payload)
    assert entry == JournalEntry(
        sequence=1,
        previous_entry_digest=None,
        entity=EntityId("household"),
        key=SemanticKey("reviewed-split"),
        kind=kind,
        operation=JournalOperation.RECORD,
        prior_entry_digest=None,
        reviewed_attempt_digest="a" * 64,
        reason=" e\u0301\n\x00 ",
        payload_digest="b" * 64,
    )
    assert encode_entry(entry) == payload
    assert entry.declaration_id.kind is RecordKind.DECLARATION
    assert entry.revision_id.kind is RecordKind.DECLARATION_REVISION


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "sequence": 0, "entry_digest": None},
        {"schema_version": 1, "sequence": 3, "entry_digest": "a" * 64},
    ],
)
def test_head_round_trips_independent_canonical_bytes(document: dict) -> None:
    payload = _bytes(document)
    head = decode_head(payload)
    assert head == JournalHead(
        sequence=document["sequence"], entry_digest=document["entry_digest"]
    )
    assert encode_head(head) == payload


@pytest.mark.parametrize("operation", ["supersede", "retract"])
def test_entry_decoder_preserves_replacement_and_retraction(
    entry_document: dict, operation: str
) -> None:
    entry_document.update(
        sequence=3,
        previous_entry_digest="c" * 64,
        operation=operation,
        prior_entry_digest="d" * 64,
    )
    if operation == "retract":
        entry_document["payload_digest"] = None
    entry = decode_entry(_bytes(entry_document))
    assert entry.operation is JournalOperation(operation)
    assert entry.prior_entry_digest == "d" * 64
    assert entry.payload_digest == entry_document["payload_digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(sequence="1"),
        lambda value: value.update(sequence=True),
        lambda value: value.update(kind="correction"),
        lambda value: value.update(payload_digest="b" * 64 + "\n"),
        lambda value: value.update(reason=""),
        lambda value: value.update(entry_digest="a" * 64),
        lambda value: value.pop("previous_entry_digest"),
    ],
)
def test_entry_decoder_rejects_invalid_headers(
    entry_document: dict, mutation: Callable[[dict], object]
) -> None:
    mutation(entry_document)
    with pytest.raises(ContractError, match="journal_schema"):
        decode_entry(_bytes(entry_document))


@pytest.mark.parametrize("suffix", [b"", b"\n\n", b" "])
def test_entry_decoder_requires_exact_lf_framing(
    entry_document: dict, suffix: bytes
) -> None:
    with pytest.raises(ContractError, match="journal_canonical"):
        decode_entry(_bytes(entry_document).removesuffix(b"\n") + suffix)


def test_entry_decoder_rejects_whitespace_and_escaped_alternate_encoding(
    entry_document: dict,
) -> None:
    with pytest.raises(ContractError, match="journal_canonical"):
        decode_entry(json.dumps(entry_document).encode() + b"\n")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"sequence":1,"sequence":2}\n',
        b'{"sequence":1.0}\n',
        b'{"sequence":NaN}\n',
        b"\xff\n",
        b"[]\n",
    ],
)
def test_entry_decoder_rejects_malformed_json(payload: bytes) -> None:
    with pytest.raises(ArtifactIntegrityError):
        decode_entry(payload)


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": True, "sequence": 0, "entry_digest": None},
        {"schema_version": 1, "sequence": "0", "entry_digest": None},
        {"schema_version": 1, "sequence": True, "entry_digest": "a" * 64},
        {"schema_version": 1, "sequence": 0, "entry_digest": "a" * 64},
        {"schema_version": 1, "sequence": 1, "entry_digest": None},
        {"schema_version": 1, "sequence": 1, "entry_digest": "a" * 64 + "\n"},
        {"schema_version": 1, "sequence": 0},
        {"schema_version": 1, "sequence": 0, "entry_digest": None, "reason": ""},
    ],
)
def test_head_decoder_rejects_invalid_headers(document: dict) -> None:
    with pytest.raises(ContractError, match="journal_schema"):
        decode_head(_bytes(document))


def test_head_decoder_rejects_alternate_canonical_form() -> None:
    with pytest.raises(ContractError, match="journal_canonical"):
        decode_head(b'{"schema_version":1,"sequence":0,"entry_digest":null}\n')
