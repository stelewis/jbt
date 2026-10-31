import ast
import hashlib
import json
from pathlib import Path

import pytest
from tests.integration.conformance.journal_reader import (
    KINDS,
    JournalObservation,
    read_entry,
    read_head,
    read_journal,
)
from tests.integration.conformance.reader import ReaderError

from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalHead,
    JournalOperation,
    encode_entry,
    entry_digest,
    select_journal,
)

pytestmark = pytest.mark.integration

VECTOR = (
    b'{"declaration_key":"split-A","entity_id":"entity-A","kind":"split",'
    b'"operation":"record","payload_digest":'
    b'"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"previous_entry_digest":null,"prior_entry_digest":null,'
    b'"reason":"reviewed split","reviewed_attempt_digest":'
    b'"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"schema_version":1,"sequence":1}\n'
)
VECTOR_DIGEST = "126cce39d5832ccd26bfffdd30db15cf918f32b135f4c1f7feada7ce980063af"
BODY = b'{"opaque":"synthetic split"}\n'
CHANGED_BODY = b'{"opaque":"changed synthetic split"}\n'
ASSIGN_BODY = b'{"opaque":"synthetic assignment"}\n'


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _wire(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _entry(  # noqa: PLR0913 - explicit independent wire fields
    sequence: int,
    *,
    previous: bytes | None = None,
    prior: bytes | None = None,
    key: str = "split-A",
    entity: str = "entity-A",
    kind: str = "split",
    operation: str = "record",
    body: bytes | None = BODY,
    reason: str = "reviewed synthetic decision",
) -> bytes:
    return _wire(
        {
            "schema_version": 1,
            "sequence": sequence,
            "previous_entry_digest": None if previous is None else _sha(previous),
            "entity_id": entity,
            "declaration_key": key,
            "kind": kind,
            "operation": operation,
            "prior_entry_digest": None if prior is None else _sha(prior),
            "reviewed_attempt_digest": "b" * 64,
            "reason": reason,
            "payload_digest": None if body is None else _sha(body),
        }
    )


def _head(entries: list[bytes]) -> bytes:
    return _wire(
        {
            "schema_version": 1,
            "sequence": len(entries),
            "entry_digest": _sha(entries[-1]) if entries else None,
        }
    )


def _bodies() -> dict[str, bytes]:
    return {_sha(body): body for body in (BODY, CHANGED_BODY, ASSIGN_BODY)}


def _observe(
    entries: list[bytes],
    *,
    selected: int | None = None,
    retained_heads: tuple[bytes, ...] = (),
) -> JournalObservation:
    return read_journal(
        entries,
        _head(entries if selected is None else entries[:selected]),
        bodies=_bodies(),
        retained_heads=retained_heads,
    )


def _positions(observation: JournalObservation) -> list[tuple[int, int]]:
    return [
        (decision.logical_position, decision.entry.sequence)
        for decision in observation.active
    ]


def _history() -> list[bytes]:
    split = _entry(1)
    child = _entry(2, previous=split, key="child-A", kind="assign", body=ASSIGN_BODY)
    equal = _entry(3, previous=child, prior=split, operation="supersede")
    changed = _entry(
        4, previous=equal, prior=equal, operation="supersede", body=CHANGED_BODY
    )
    retract = _entry(
        5,
        previous=changed,
        prior=child,
        key="child-A",
        kind="assign",
        operation="retract",
        body=None,
    )
    new = _entry(6, previous=retract, key="child-B", kind="assign", body=ASSIGN_BODY)
    return [split, child, equal, changed, retract, new]


def test_independent_canonical_header_vector() -> None:
    observed = read_entry(VECTOR)
    assert observed.digest == VECTOR_DIGEST
    assert observed.sequence == 1
    assert observed.declaration_key == "split-A"
    assert observed.payload_digest == "a" * 64
    assert _wire(json.loads(VECTOR)) == VECTOR
    assert _sha(VECTOR) == VECTOR_DIGEST


def test_supersession_keeps_original_logical_position_without_financial_claim() -> None:
    history = _history()
    equal = _observe(history[:3])
    assert _positions(equal) == [(1, 3), (2, 2)]
    assert equal.active[0].entry.payload_digest == _sha(BODY)
    changed = _observe(history[:4])
    assert _positions(changed) == [(1, 4), (2, 2)]
    assert changed.active[0].entry.payload_digest == _sha(CHANGED_BODY)
    assert _positions(_observe(history[:5])) == [(1, 4)]
    assert _positions(_observe(history)) == [(1, 4), (6, 6)]


def test_repeated_supersession_uses_immediately_active_digest() -> None:
    entries = _history()
    entries.append(
        _entry(7, previous=entries[-1], prior=entries[3], operation="supersede")
    )
    entries.append(
        _entry(
            8,
            previous=entries[-1],
            prior=entries[-1],
            operation="supersede",
            body=CHANGED_BODY,
        )
    )
    assert _positions(_observe(entries)) == [(1, 8), (6, 6)]


def test_entity_qualified_keys_have_independent_lifetimes() -> None:
    first = _entry(1)
    second = _entry(2, previous=first, entity="entity-B")
    third = _entry(
        3, previous=second, prior=first, operation="supersede", body=CHANGED_BODY
    )
    observation = _observe([first, second, third])
    assert _positions(observation) == [(1, 3), (2, 2)]
    assert [item.entry.entity_id for item in observation.active] == [
        "entity-A",
        "entity-B",
    ]


def test_historical_head_selects_prefix_and_exposes_unapplied_suffix() -> None:
    entries = _history()
    result = _observe(
        entries,
        selected=2,
        retained_heads=(_head([]), _head(entries[:1]), _head(entries[:2])),
    )
    assert _positions(result) == [(1, 1), (2, 2)]
    assert [entry.sequence for entry in result.selected] == [1, 2]
    assert [entry.sequence for entry in result.unselected] == [3, 4, 5, 6]
    assert result.head.sequence == 2


def test_unselected_retraction_does_not_disable_selected_decision() -> None:
    entries = _history()
    assert _positions(_observe(entries, selected=4)) == [(1, 4), (2, 2)]
    assert _positions(_observe(entries, selected=0)) == []
    assert _observe(entries, selected=0).selected == ()


def test_matching_retained_heads_and_empty_journal() -> None:
    entries = _history()
    assert _positions(
        _observe(entries, retained_heads=tuple(_head(entries[:n]) for n in range(7)))
    ) == [(1, 4), (6, 6)]
    empty = _observe([])
    assert empty.selected == empty.active == empty.unselected == ()
    assert empty.head.entry_digest is None


def test_valid_shorter_chain_and_head_cannot_rollback_retained_commit() -> None:
    entries = _history()
    assert _positions(_observe(entries[:2])) == [(1, 1), (2, 2)]
    with pytest.raises(ReaderError, match="rollback"):
        _observe(entries[:2], retained_heads=(_head(entries),))
    with pytest.raises(ReaderError, match="rollback"):
        _observe(entries, selected=2, retained_heads=(_head(entries[:3]),))


@pytest.mark.parametrize("head_kind", ["missing", "wrong", "retained-wrong"])
def test_head_must_name_exact_retained_entry(head_kind: str) -> None:
    entries = _history()
    value = json.loads(_head(entries))
    if head_kind == "missing":
        value["sequence"] = 7
    else:
        value["entry_digest"] = "c" * 64
    bad = _wire(value)
    with pytest.raises(ReaderError, match="head"):
        read_journal(
            entries,
            _head(entries) if head_kind == "retained-wrong" else bad,
            bodies=_bodies(),
            retained_heads=(bad,) if head_kind == "retained-wrong" else (),
        )


@pytest.mark.parametrize("mutation", ["altered", "missing", "suffix-altered"])
def test_body_seals_are_verified_without_body_interpretation(mutation: str) -> None:
    entries = _history()
    bodies = _bodies()
    digest = _sha(CHANGED_BODY) if mutation == "suffix-altered" else _sha(BODY)
    if mutation == "missing":
        del bodies[digest]
    else:
        bodies[digest] = b"unsealed replacement"
    with pytest.raises(ReaderError, match="body"):
        read_journal(
            entries,
            _head(entries[:2] if mutation == "suffix-altered" else entries),
            bodies=bodies,
        )


def test_bodies_are_opaque_exact_bytes_not_json_documents() -> None:
    opaque = b"\xff\x00not JSON\r\n"
    first = _entry(1, body=opaque)
    result = read_journal([first], _head([first]), bodies={_sha(opaque): opaque})
    assert _positions(result) == [(1, 1)]


@pytest.mark.parametrize("mutation", ["gap", "reorder", "duplicate", "link", "bytes"])
def test_physical_custody_chain_is_contiguous_and_exact(mutation: str) -> None:
    entries = _history()
    if mutation == "gap":
        entries.pop(1)
    elif mutation == "reorder":
        entries[1], entries[2] = entries[2], entries[1]
    elif mutation == "duplicate":
        entries.insert(1, entries[0])
    else:
        value = json.loads(entries[1])
        value["previous_entry_digest" if mutation == "link" else "reason"] = (
            "c" * 64 if mutation == "link" else "altered header"
        )
        entries[1] = _wire(value)
    with pytest.raises(ReaderError, match=r"sequence|link"):
        _observe(entries)


@pytest.mark.parametrize(
    "mutation",
    ["stale", "unknown", "inactive", "kind", "key", "entity", "duplicate-record"],
)
def test_rejects_nonactive_or_wrong_scope_prior(mutation: str) -> None:
    entries = _history()[:3]
    prior = entries[2]
    key, entity, kind = "split-A", "entity-A", "split"
    operation = "supersede"
    if mutation == "stale":
        prior = entries[0]
    elif mutation == "unknown":
        prior = b"unknown canonical entry"
    elif mutation == "inactive":
        entries = _history()[:5]
        prior, key, kind = entries[1], "child-A", "assign"
    elif mutation == "kind":
        kind = "assign"
    elif mutation == "key":
        key = "different-key"
    elif mutation == "entity":
        entity = "different-entity"
    else:
        operation = "record"
    entries.append(
        _entry(
            len(entries) + 1,
            previous=entries[-1],
            prior=None if operation == "record" else prior,
            key=key,
            entity=entity,
            kind=kind,
            operation=operation,
        )
    )
    with pytest.raises(ReaderError, match=r"prior|already established"):
        _observe(entries)


@pytest.mark.parametrize("operation", ["record", "supersede", "retract"])
def test_retracted_key_cannot_be_reactivated_or_retracted_again(operation: str) -> None:
    entries = _history()[:5]
    entries.append(
        _entry(
            6,
            previous=entries[-1],
            prior=None if operation == "record" else entries[4],
            key="child-A",
            kind="assign",
            operation=operation,
            body=None if operation == "retract" else ASSIGN_BODY,
        )
    )
    with pytest.raises(ReaderError, match=r"inactive|already established"):
        _observe(entries)


@pytest.mark.parametrize("reason", ["e\u0301", " \t\r\n ", "\x00\x1f\x7f", "理由🙂"])
def test_descriptive_reason_preserves_exact_utf8(reason: str) -> None:
    first = _entry(1, reason=reason)
    result = _observe([first])
    assert result.active[0].entry.reason.encode("utf-8") == reason.encode("utf-8")
    assert result.active[0].entry.digest == _sha(first)


def test_distinct_descriptive_normalizations_have_distinct_entry_digests() -> None:
    nfd = read_entry(_entry(1, reason="e\u0301"))
    nfc = read_entry(_entry(1, reason="é"))
    assert nfd.digest != nfc.digest
    assert nfd.reason != nfc.reason


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_all_supported_kinds_are_readable(kind: str) -> None:
    assert _observe([_entry(1, kind=kind)]).active[0].entry.kind == kind


@pytest.mark.parametrize("field", ["entity_id", "declaration_key"])
@pytest.mark.parametrize("value", ["", "e\u0301", "a\x00b", "a\nb", "\x7f", 4, None])
def test_identifiers_require_nonempty_nfc_control_free_text(
    field: str, value: object
) -> None:
    entry = json.loads(_entry(1))
    entry[field] = value
    with pytest.raises(ReaderError):
        read_entry(_wire(entry))


def test_identifiers_accept_nfc_unicode_without_overrestricting_whitespace() -> None:
    first = _entry(1, entity=" entité ", key="分割🙂")
    assert _observe([first]).active[0].entry.entity_id == " entité "


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("schema_version", 0),
        ("schema_version", 2),
        ("sequence", True),
        ("sequence", "1"),
        ("sequence", 0),
        ("sequence", -1),
        ("sequence", 2**63),
        ("sequence", 1.0),
        ("sequence", float("nan")),
        ("sequence", float("inf")),
        ("kind", "unknown"),
        ("kind", ["split"]),
        ("operation", "unknown"),
        ("reason", ""),
        ("reason", False),
        ("reviewed_attempt_digest", "A" * 64),
        ("reviewed_attempt_digest", "0" * 63),
        ("reviewed_attempt_digest", "g" * 64),
        ("reviewed_attempt_digest", None),
        ("payload_digest", None),
        ("payload_digest", 4),
        ("previous_entry_digest", "a" * 64),
        ("prior_entry_digest", "a" * 64),
    ],
)
def test_invalid_entry_fields_fail_safe(field: str, value: object) -> None:
    entry = json.loads(_entry(1))
    entry[field] = value
    with pytest.raises(ReaderError):
        read_entry(_wire(entry))


@pytest.mark.parametrize(
    ("operation", "prior", "body"),
    [
        ("supersede", None, BODY),
        ("retract", None, None),
        ("retract", BODY, BODY),
        ("supersede", BODY, None),
    ],
)
def test_operation_specific_nullable_fields(
    operation: str, prior: bytes | None, body: bytes | None
) -> None:
    with pytest.raises(ReaderError):
        read_entry(
            _entry(2, previous=BODY, operation=operation, prior=prior, body=body)
        )


def test_noninitial_entry_requires_previous_digest() -> None:
    with pytest.raises(ReaderError):
        read_entry(_entry(2))
    maximum = read_entry(_entry(2**63 - 1, previous=BODY))
    assert maximum.sequence == 2**63 - 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("schema_version", 2),
        ("sequence", True),
        ("sequence", "0"),
        ("sequence", -1),
        ("sequence", 2**63),
        ("sequence", 0.0),
        ("entry_digest", "a" * 64),
    ],
)
def test_invalid_head_fields_fail_safe(field: str, value: object) -> None:
    head = json.loads(_head([]))
    head[field] = value
    with pytest.raises(ReaderError):
        read_head(_wire(head))


@pytest.mark.parametrize("digest", [None, "A" * 64, "0" * 63, 4])
def test_nonempty_head_requires_digest(digest: object) -> None:
    with pytest.raises(ReaderError):
        read_head(_wire({"schema_version": 1, "sequence": 1, "entry_digest": digest}))


@pytest.mark.parametrize("target", ["entry", "head"])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing-key",
        "extra-key",
        "duplicate-key",
        "no-lf",
        "extra-lf",
        "spaces",
        "reordered-keys",
        "bom",
        "invalid-utf8",
        "surrogate",
        "non-object",
        "trailing",
        "bad-json",
    ],
)
def test_only_exact_closed_canonical_utf8_json_is_accepted(
    target: str, mutation: str
) -> None:
    payload = _entry(1) if target == "entry" else _head([])
    value = json.loads(payload)
    if mutation == "missing-key":
        del value["schema_version"]
        payload = _wire(value)
    elif mutation == "extra-key":
        payload = _wire({**value, "extra": None})
    elif mutation == "duplicate-key":
        payload = b'{"schema_version":1,' + payload[1:]
    elif mutation == "reordered-keys":
        payload = (
            json.dumps(
                dict(reversed(list(value.items()))), separators=(",", ":")
            ).encode()
            + b"\n"
        )
    else:
        payload = {
            "no-lf": payload[:-1],
            "extra-lf": payload + b"\n",
            "spaces": payload.replace(b":", b": ", 1),
            "bom": b"\xef\xbb\xbf" + payload,
            "invalid-utf8": payload.replace(b"schema_version", b"schema_\xffversion"),
            "surrogate": payload.replace(b"schema_version", b"schema_\\ud800version"),
            "non-object": b"[]\n",
            "trailing": payload + b"{}\n",
            "bad-json": b"{\n",
        }[mutation]
    with pytest.raises(ReaderError):
        (read_entry if target == "entry" else read_head)(payload)


def test_reader_has_no_producer_imports() -> None:
    path = Path(__file__).with_name("journal_reader.py")
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module] if node.module is not None else []
        else:
            continue
        assert all(name != "jbt" and not name.startswith("jbt.") for name in imports)


def _producer_entry(payload: bytes) -> JournalEntry:
    observed = read_entry(payload)
    return JournalEntry(
        sequence=observed.sequence,
        previous_entry_digest=observed.previous_entry_digest,
        entity=EntityId(observed.entity_id),
        key=SemanticKey(observed.declaration_key),
        kind=DecisionKind(observed.kind),
        operation=JournalOperation(observed.operation),
        prior_entry_digest=observed.prior_entry_digest,
        reviewed_attempt_digest=observed.reviewed_attempt_digest,
        reason=observed.reason,
        payload_digest=observed.payload_digest,
    )


def test_public_producer_matches_independent_header_vector() -> None:
    assert read_entry(VECTOR).digest == VECTOR_DIGEST
    producer = _producer_entry(VECTOR)
    assert encode_entry(producer) == VECTOR
    assert entry_digest(producer) == VECTOR_DIGEST


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (0, []),
        (1, [(1, 1)]),
        (2, [(1, 1), (2, 2)]),
        (3, [(1, 3), (2, 2)]),
        (4, [(1, 4), (2, 2)]),
        (5, [(1, 4)]),
        (6, [(1, 4), (6, 6)]),
    ],
)
def test_public_producer_selected_order_matches_independent_oracle(
    selected: int, expected: list[tuple[int, int]]
) -> None:
    history = _history()
    independent = _observe(history, selected=selected)
    assert _positions(independent) == expected
    producer_entries = tuple(_producer_entry(raw) for raw in history)
    result = select_journal(
        producer_entries,
        head=JournalHead(independent.head.sequence, independent.head.entry_digest),
        retained_heads=(),
    )
    assert [
        (decision.logical_position, decision.entry.sequence)
        for decision in result.active_decisions
    ] == expected
    assert tuple(item.sequence for item in result.committed_entries) == tuple(
        range(1, selected + 1)
    )
    assert tuple(item.sequence for item in result.unselected_entries) == tuple(
        range(selected + 1, 7)
    )
    assert tuple(encode_entry(entry) for entry in producer_entries) == tuple(history)
