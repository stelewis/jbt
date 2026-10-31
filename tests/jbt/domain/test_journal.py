from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from typing import TYPE_CHECKING, cast

import pytest

from jbt.domain.errors import DomainError
from jbt.domain.identity import declaration_id, revision_id
from jbt.domain.ids import EntityId, RecordKind, SemanticKey
from jbt.domain.journal import (
    ActiveDecision,
    DecisionKind,
    DecisionStage,
    JournalEntry,
    JournalError,
    JournalHead,
    JournalOperation,
    JournalSelection,
    encode_entry,
    encode_head,
    entry_digest,
    select_journal,
)

if TYPE_CHECKING:
    from jbt.domain.canonical import JsonValue

VECTOR_BYTES = (
    b'{"declaration_key":"split-a","entity_id":"synthetic","kind":"split",'
    b'"operation":"record","payload_digest":'
    b'"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"previous_entry_digest":null,"prior_entry_digest":null,'
    b'"reason":" e\xcc\x81\\n\\u0000 ","reviewed_attempt_digest":'
    b'"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"schema_version":1,"sequence":1}\n'
)
VECTOR_DIGEST = "8e302490231dd414d215fbd06ce0991b0d2858b7a49105d9cf17f08631c3a94e"
VECTOR_REVISION = "8a3648f7e9d828d8db50ccdf789ee94479da42e7cae2948c8132b897ba04bbd3"


@pytest.fixture
def entry() -> JournalEntry:
    return JournalEntry(
        sequence=1,
        previous_entry_digest=None,
        entity=EntityId("synthetic"),
        key=SemanticKey("split-a"),
        kind=DecisionKind.SPLIT,
        operation=JournalOperation.RECORD,
        prior_entry_digest=None,
        reviewed_attempt_digest="a" * 64,
        reason=" e\u0301\n\x00 ",
        payload_digest="b" * 64,
    )


def _next(previous: JournalEntry, **changes: object) -> JournalEntry:
    return replace(
        previous,
        sequence=previous.sequence + 1,
        previous_entry_digest=entry_digest(previous),
        **changes,
    )


def _head(entry: JournalEntry) -> JournalHead:
    return JournalHead(entry.sequence, entry_digest(entry))


def test_closed_decision_kinds_and_operations() -> None:
    assert tuple(DecisionKind) == (
        DecisionKind.ASSIGN,
        DecisionKind.SPLIT,
        DecisionKind.IDENTITY,
        DecisionKind.STATEMENT_REVISION,
        DecisionKind.TRANSFER,
        DecisionKind.LINK,
    )
    assert tuple(kind.value for kind in DecisionKind) == (
        "assign",
        "split",
        "identity",
        "statement_revision",
        "transfer",
        "link",
    )
    assert tuple(operation.value for operation in JournalOperation) == (
        "record",
        "supersede",
        "retract",
    )
    assert [kind.stage for kind in DecisionKind] == [
        DecisionStage.ENRICH,
        DecisionStage.ENRICH,
        DecisionStage.MERGE,
        DecisionStage.MERGE,
        DecisionStage.LINK,
        DecisionStage.LINK,
    ]
    assert DecisionStage.MERGE < DecisionStage.ENRICH < DecisionStage.LINK


def test_independent_exact_canonical_bytes_digest_and_identities(
    entry: JournalEntry,
) -> None:
    assert encode_entry(entry) == VECTOR_BYTES
    assert sha256(VECTOR_BYTES).hexdigest() == VECTOR_DIGEST
    assert entry_digest(entry) == VECTOR_DIGEST
    header: dict[str, JsonValue] = {
        "schema_version": 1,
        "sequence": 1,
        "previous_entry_digest": None,
        "entity_id": "synthetic",
        "declaration_key": "split-a",
        "kind": "split",
        "operation": "record",
        "prior_entry_digest": None,
        "reviewed_attempt_digest": "a" * 64,
        "reason": " e\u0301\n\x00 ",
        "payload_digest": "b" * 64,
    }
    assert entry.declaration_id == declaration_id(
        entry.entity, RecordKind.DECLARATION, entry.key
    )
    assert entry.revision_id == revision_id(
        entry.entity,
        entry.key,
        effective_from=None,
        effective_to=None,
        payload=header,
    )
    assert entry.revision_id.value == VECTOR_REVISION
    assert entry.declaration_id.kind is RecordKind.DECLARATION
    assert entry.revision_id.kind is RecordKind.DECLARATION_REVISION
    replacement = _next(
        entry,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=VECTOR_DIGEST,
    )
    assert replacement.declaration_id == entry.declaration_id
    assert replacement.revision_id != entry.revision_id


def test_descriptive_bytes_are_never_trimmed_normalized_or_filtered(
    entry: JournalEntry,
) -> None:
    assert entry.reason == " e\u0301\n\x00 "
    for reason in (" é\n\x00 ", "e\u0301\n\x00", " ", "\x00"):
        changed = replace(entry, reason=reason)
        assert changed.reason == reason
        assert encode_entry(changed) != VECTOR_BYTES
        assert entry_digest(changed) != VECTOR_DIGEST
        assert changed.revision_id != entry.revision_id
    changed_attempt = replace(entry, reviewed_attempt_digest="c" * 64)
    assert changed_attempt.declaration_id == entry.declaration_id
    assert changed_attempt.revision_id != entry.revision_id
    assert entry_digest(changed_attempt) != entry_digest(entry)


def test_exact_canonical_head_bytes(entry: JournalEntry) -> None:
    assert encode_head(JournalHead(0, None)) == (
        b'{"entry_digest":null,"schema_version":1,"sequence":0}\n'
    )
    assert encode_head(_head(entry)) == (
        b'{"entry_digest":"'
        + VECTOR_DIGEST.encode("ascii")
        + b'","schema_version":1,"sequence":1}\n'
    )


@pytest.mark.parametrize("payload", ["b" * 64, "c" * 64])
def test_split_supersession_keeps_position_before_child_assignment(
    entry: JournalEntry, payload: str
) -> None:
    child_assign = _next(
        entry, key=SemanticKey("child-assign"), kind=DecisionKind.ASSIGN
    )
    supersede_split = _next(
        child_assign,
        key=entry.key,
        kind=DecisionKind.SPLIT,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(entry),
        payload_digest=payload,
    )
    history = (entry, child_assign, supersede_split)
    result = select_journal(history, head=_head(supersede_split), retained_heads=())
    assert result.head == _head(supersede_split)
    assert result.committed_entries == history
    assert result.active_decisions == (
        ActiveDecision(supersede_split, 1),
        ActiveDecision(child_assign, 2),
    )
    assert tuple(item.entry.sequence for item in result.active_decisions) == (3, 2)
    assert result.unselected_entries == ()


def test_repeated_supersession_preserves_first_record_position(
    entry: JournalEntry,
) -> None:
    second = _next(
        entry,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(entry),
    )
    third = _next(
        second, prior_entry_digest=entry_digest(second), payload_digest="c" * 64
    )
    fourth = _next(third, prior_entry_digest=entry_digest(third), reason="re-reviewed")
    result = select_journal(
        (entry, second, third, fourth), head=_head(fourth), retained_heads=()
    )
    assert result.active_decisions == (ActiveDecision(fourth, 1),)
    assert len(result.committed_entries) == 4


def test_retraction_then_new_key_explicitly_reorders(entry: JournalEntry) -> None:
    child_assign = _next(
        entry, key=SemanticKey("child-assign"), kind=DecisionKind.ASSIGN
    )
    retraction = _next(
        child_assign,
        key=entry.key,
        kind=entry.kind,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None,
    )
    replacement = _next(
        retraction,
        key=SemanticKey("split-b"),
        operation=JournalOperation.RECORD,
        prior_entry_digest=None,
        payload_digest=entry.payload_digest,
    )
    result = select_journal(
        (entry, child_assign, retraction, replacement),
        head=_head(replacement),
        retained_heads=(),
    )
    assert result.active_decisions == (
        ActiveDecision(child_assign, 2),
        ActiveDecision(replacement, 4),
    )
    assert retraction not in tuple(item.entry for item in result.active_decisions)
    assert result.committed_entries[2] is retraction
    assert replacement.declaration_id != entry.declaration_id


def test_entity_qualified_local_keys_are_independent(entry: JournalEntry) -> None:
    other = _next(entry, entity=EntityId("other"))
    supersede = _next(
        other,
        entity=entry.entity,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(entry),
    )
    result = select_journal(
        (entry, other, supersede), head=_head(supersede), retained_heads=()
    )
    assert result.active_decisions == (
        ActiveDecision(supersede, 1),
        ActiveDecision(other, 2),
    )
    assert entry.declaration_id != other.declaration_id


@pytest.mark.parametrize(
    "operation", [JournalOperation.SUPERSEDE, JournalOperation.RETRACT]
)
@pytest.mark.parametrize(
    "reference", ["unknown", "wrong_entity", "wrong_key", "future"]
)
def test_prior_must_be_immediately_active_at_same_entity_and_key(
    entry: JournalEntry, operation: JournalOperation, reference: str
) -> None:
    prior_digest = entry_digest(entry)
    changes: dict[str, object] = {}
    if reference == "unknown":
        prior_digest = "d" * 64
    elif reference == "wrong_entity":
        changes["entity"] = EntityId("other")
    elif reference == "wrong_key":
        changes["key"] = SemanticKey("other")
    elif reference == "future":
        future = _next(entry, key=SemanticKey("future"))
        prior_digest = entry_digest(future)
    invalid = _next(
        entry,
        operation=operation,
        prior_entry_digest=prior_digest,
        payload_digest=None if operation is JournalOperation.RETRACT else "b" * 64,
        **changes,
    )
    with pytest.raises(
        JournalError, match="immediately_active_prior_required"
    ) as caught:
        select_journal((entry, invalid), head=_head(invalid), retained_heads=())
    assert caught.value.location == "journal.entries[1].prior_entry_digest"


@pytest.mark.parametrize(
    "operation", [JournalOperation.SUPERSEDE, JournalOperation.RETRACT]
)
def test_stale_prior_is_rejected(
    entry: JournalEntry, operation: JournalOperation
) -> None:
    supersede = _next(
        entry,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(entry),
    )
    stale = _next(
        supersede,
        operation=operation,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None if operation is JournalOperation.RETRACT else "b" * 64,
    )
    with pytest.raises(JournalError, match="immediately_active_prior_required"):
        select_journal((entry, supersede, stale), head=_head(stale), retained_heads=())


@pytest.mark.parametrize("reference_retraction", [False, True])
@pytest.mark.parametrize(
    "operation", [JournalOperation.SUPERSEDE, JournalOperation.RETRACT]
)
def test_dead_prior_cannot_be_revived(
    entry: JournalEntry, operation: JournalOperation, *, reference_retraction: bool
) -> None:
    retraction = _next(
        entry,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None,
    )
    invalid = _next(
        retraction,
        operation=operation,
        prior_entry_digest=entry_digest(retraction if reference_retraction else entry),
        payload_digest=None if operation is JournalOperation.RETRACT else "b" * 64,
    )
    with pytest.raises(JournalError, match="immediately_active_prior_required"):
        select_journal(
            (entry, retraction, invalid), head=_head(invalid), retained_heads=()
        )


@pytest.mark.parametrize(
    "operation", [JournalOperation.SUPERSEDE, JournalOperation.RETRACT]
)
def test_variant_change_requires_retract_and_new_record(
    entry: JournalEntry, operation: JournalOperation
) -> None:
    changed_kind = _next(
        entry,
        kind=DecisionKind.ASSIGN,
        operation=operation,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None if operation is JournalOperation.RETRACT else "b" * 64,
    )
    with pytest.raises(JournalError, match="same_decision_kind_required"):
        select_journal(
            (entry, changed_kind), head=_head(changed_kind), retained_heads=()
        )


def test_duplicate_record_key_is_forbidden_even_after_retraction(
    entry: JournalEntry,
) -> None:
    duplicate = _next(entry, kind=DecisionKind.ASSIGN)
    with pytest.raises(JournalError, match="duplicate_record_key"):
        select_journal((entry, duplicate), head=_head(duplicate), retained_heads=())
    retraction = _next(
        entry,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None,
    )
    repeated = _next(
        retraction,
        operation=JournalOperation.RECORD,
        prior_entry_digest=None,
        payload_digest="b" * 64,
    )
    with pytest.raises(JournalError, match="duplicate_record_key"):
        select_journal(
            (entry, retraction, repeated), head=_head(repeated), retained_heads=()
        )
    retracted = select_journal(
        (entry, retraction), head=_head(retraction), retained_heads=()
    )
    assert retracted.active_decisions == ()


def test_empty_head_does_not_commit_supplied_entries(entry: JournalEntry) -> None:
    empty = JournalHead(0, None)
    assert select_journal((), head=empty, retained_heads=(empty,)) == JournalSelection(
        empty, (), (), ()
    )
    selection = select_journal((entry,), head=empty, retained_heads=())
    assert selection.committed_entries == ()
    assert selection.active_decisions == ()
    assert selection.unselected_entries == (entry,)
    with pytest.raises(JournalError, match="retained_head_extension_required"):
        select_journal((entry,), head=empty, retained_heads=(_head(entry),))


def test_retained_heads_require_extension_and_matching_history(
    entry: JournalEntry,
) -> None:
    second = _next(entry, key=SemanticKey("second"))
    third = _next(second, key=SemanticKey("third"))
    heads = (JournalHead(0, None), _head(entry), _head(second), _head(third))
    selected = select_journal(
        (entry, second, third), head=heads[-1], retained_heads=heads
    )
    assert len(selected.active_decisions) == 3
    with pytest.raises(JournalError, match="retained_head_extension_required"):
        select_journal((entry,), head=_head(entry), retained_heads=(_head(second),))
    with pytest.raises(JournalError, match="retained_head_extension_required"):
        select_journal(
            (entry, second, third),
            head=_head(entry),
            retained_heads=(_head(second),),
        )
    changed = replace(entry, reason="different history")
    with pytest.raises(JournalError, match="head_digest_mismatch") as caught:
        select_journal(
            (entry, second, third),
            head=_head(third),
            retained_heads=(_head(changed),),
        )
    assert caught.value.location == "journal.retained_heads[0]"


def test_selected_head_must_exist_and_match(entry: JournalEntry) -> None:
    second = _next(entry, key=SemanticKey("second"))
    with pytest.raises(JournalError, match="head_entry_missing"):
        select_journal((entry,), head=_head(second), retained_heads=())
    with pytest.raises(JournalError, match="head_entry_missing"):
        select_journal((), head=_head(entry), retained_heads=())
    with pytest.raises(JournalError, match="head_digest_mismatch"):
        select_journal((entry,), head=JournalHead(1, "d" * 64), retained_heads=())


def test_pinned_replay_never_folds_unselected_entries(entry: JournalEntry) -> None:
    supersede = _next(
        entry,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(entry),
        payload_digest="c" * 64,
    )
    uncommitted_invalid = _next(
        supersede,
        prior_entry_digest="d" * 64,
        kind=DecisionKind.ASSIGN,
    )
    pinned = select_journal(
        (entry, supersede, uncommitted_invalid),
        head=_head(entry),
        retained_heads=(JournalHead(0, None), _head(entry)),
    )
    assert pinned.committed_entries == (entry,)
    assert pinned.active_decisions == (ActiveDecision(entry, 1),)
    assert pinned.unselected_entries == (supersede, uncommitted_invalid)
    extended = select_journal(
        (entry, supersede, uncommitted_invalid),
        head=_head(supersede),
        retained_heads=(_head(entry),),
    )
    assert extended.active_decisions == (ActiveDecision(supersede, 1),)
    assert extended.unselected_entries == (uncommitted_invalid,)
    with pytest.raises(JournalError, match="immediately_active_prior_required"):
        select_journal(
            (entry, supersede, uncommitted_invalid),
            head=_head(uncommitted_invalid),
            retained_heads=(),
        )


@pytest.mark.parametrize("shape", ["gap", "duplicate", "reordered", "missing_first"])
def test_entire_physical_chain_must_be_contiguous(
    entry: JournalEntry, shape: str
) -> None:
    second = _next(entry, key=SemanticKey("second"))
    third = _next(second, key=SemanticKey("third"))
    histories = {
        "gap": (entry, third),
        "duplicate": (entry, second, second),
        "reordered": (entry, third, second),
        "missing_first": (second, third),
    }
    with pytest.raises(JournalError, match="contiguous_sequence_required"):
        select_journal(histories[shape], head=_head(entry), retained_heads=())


def test_broken_links_are_rejected_even_beyond_selected_head(
    entry: JournalEntry,
) -> None:
    broken = replace(
        _next(entry, key=SemanticKey("second")), previous_entry_digest="d" * 64
    )
    with pytest.raises(JournalError, match="previous_entry_digest_mismatch") as caught:
        select_journal((entry, broken), head=_head(entry), retained_heads=())
    assert caught.value.location == "journal.entries[1].previous_entry_digest"


@pytest.mark.parametrize("value", [True, False, 0, -1, 2**63, 1.0, "1", None])
def test_entry_sequence_requires_positive_int64(
    entry: JournalEntry, value: object
) -> None:
    with pytest.raises(JournalError, match="int64_sequence_required") as caught:
        replace(entry, sequence=value)
    assert caught.value.location == "journal_entry.sequence"


@pytest.mark.parametrize("value", [True, False, -1, 2**63, 1.0, "0", None])
def test_head_sequence_requires_nonnegative_int64(value: object) -> None:
    with pytest.raises(JournalError, match="int64_sequence_required"):
        JournalHead(cast("int", value), None)


def test_int64_maximum_is_admitted_for_entry_and_head(entry: JournalEntry) -> None:
    maximum = 2**63 - 1
    assert (
        replace(entry, sequence=maximum, previous_entry_digest="d" * 64).sequence
        == maximum
    )
    assert JournalHead(maximum, "d" * 64).sequence == maximum


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"entity": "synthetic"}, "typed_entry_required"),
        ({"key": "split-a"}, "typed_entry_required"),
        ({"kind": "split"}, "typed_entry_required"),
        ({"operation": "record"}, "typed_entry_required"),
        ({"reason": ""}, "nonempty_reason_required"),
        ({"reason": None}, "nonempty_reason_required"),
        ({"reason": b"reason"}, "nonempty_reason_required"),
        ({"reason": "\ud800"}, "utf8_required"),
        ({"previous_entry_digest": "a" * 64}, "previous_entry_digest_required"),
        ({"sequence": 2}, "previous_entry_digest_required"),
        ({"prior_entry_digest": "a" * 64}, "operation_prior_required"),
        ({"operation": JournalOperation.SUPERSEDE}, "operation_prior_required"),
        ({"operation": JournalOperation.RETRACT}, "operation_prior_required"),
        ({"payload_digest": None}, "operation_payload_required"),
        (
            {"operation": JournalOperation.RETRACT, "prior_entry_digest": "d" * 64},
            "operation_payload_required",
        ),
    ],
)
def test_entry_construction_is_strict_and_located(
    entry: JournalEntry, changes: dict[str, object], constraint: str
) -> None:
    with pytest.raises(JournalError, match=constraint) as caught:
        replace(entry, **changes)
    assert isinstance(caught.value, DomainError)
    assert caught.value.constraint == constraint
    assert caught.value.location.startswith("journal_entry")
    assert caught.value.references == ()


@pytest.mark.parametrize(
    "value", ["A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n", "", True, 1, None]
)
@pytest.mark.parametrize(
    "field",
    [
        "previous_entry_digest",
        "prior_entry_digest",
        "reviewed_attempt_digest",
        "payload_digest",
    ],
)
def test_all_entry_digest_fields_are_validated(
    entry: JournalEntry, field: str, value: object
) -> None:
    changes: dict[str, object] = {field: value}
    if field == "previous_entry_digest":
        changes["sequence"] = 2
    elif field == "prior_entry_digest":
        changes["operation"] = JournalOperation.SUPERSEDE
    with pytest.raises(JournalError) as caught:
        replace(entry, **changes)
    assert caught.value.location == f"journal_entry.{field}"


@pytest.mark.parametrize(
    ("sequence", "digest"),
    [(0, "a" * 64), (1, None), (1, "A" * 64), (1, ""), (1, True)],
)
def test_head_requires_digest_exactly_for_nonempty_sequence(
    sequence: int, digest: object
) -> None:
    with pytest.raises(JournalError) as caught:
        JournalHead(sequence, cast("str | None", digest))
    assert caught.value.location == "journal_head.entry_digest"


@pytest.mark.parametrize(
    "input_kind", ["entries", "entry", "head", "heads", "retained"]
)
def test_selection_requires_immutable_typed_inputs(
    entry: JournalEntry, input_kind: str
) -> None:
    entries: object = (entry,)
    head: object = _head(entry)
    retained: object = ()
    if input_kind == "entries":
        entries = [entry]
    elif input_kind == "entry":
        entries = ("entry",)
    elif input_kind == "head":
        head = 1
    elif input_kind == "heads":
        retained = [_head(entry)]
    elif input_kind == "retained":
        retained = (1,)
    with pytest.raises(JournalError):
        select_journal(
            cast("tuple[JournalEntry, ...]", entries),
            head=cast("JournalHead", head),
            retained_heads=cast("tuple[JournalHead, ...]", retained),
        )


def test_encoders_require_typed_inputs() -> None:
    with pytest.raises(JournalError, match="typed_entry_required"):
        encode_entry(cast("JournalEntry", {}))
    with pytest.raises(JournalError, match="typed_entry_required"):
        entry_digest(cast("JournalEntry", {}))
    with pytest.raises(JournalError, match="typed_head_required"):
        encode_head(cast("JournalHead", {}))


def test_results_are_frozen_slotted_and_typed(entry: JournalEntry) -> None:
    selected = select_journal((entry,), head=_head(entry), retained_heads=())
    active = selected.active_decisions[0]
    for value in (entry, selected.head, active, selected):
        assert not hasattr(value, "__dict__")
    for value, attribute, replacement in (
        (entry, "reason", "edited"),
        (selected.head, "sequence", 0),
        (active, "logical_position", 2),
        (selected, "committed_entries", ()),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, replacement)
    with pytest.raises(JournalError, match="immutable_typed_selection_required"):
        replace(selected, active_decisions=[active])
    with pytest.raises(JournalError, match="immutable_typed_selection_required"):
        replace(selected, committed_entries=("entry",))
    with pytest.raises(JournalError, match="immutable_typed_selection_required"):
        replace(selected, unselected_entries=[entry])
    with pytest.raises(JournalError, match="typed_head_required"):
        replace(selected, head="head")


@pytest.mark.parametrize("position", [True, 0, -1, 2**63])
def test_active_decision_position_requires_positive_int64(
    entry: JournalEntry, position: int
) -> None:
    with pytest.raises(JournalError, match="int64_sequence_required"):
        ActiveDecision(entry, position)


def test_active_decision_must_be_nonretracted_and_positioned(
    entry: JournalEntry,
) -> None:
    with pytest.raises(JournalError, match="typed_entry_required"):
        ActiveDecision(cast("JournalEntry", "entry"), 1)
    with pytest.raises(JournalError, match="active_revision_position_required"):
        ActiveDecision(entry, 2)
    second = _next(entry, key=SemanticKey("second"))
    with pytest.raises(JournalError, match="active_revision_position_required"):
        ActiveDecision(second, 1)
    retraction = _next(
        entry,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=entry_digest(entry),
        payload_digest=None,
    )
    with pytest.raises(JournalError, match="active_revision_position_required"):
        ActiveDecision(retraction, 1)
