import errno
import os
import stat
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import byte_digest
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalError,
    JournalHead,
    JournalOperation,
    encode_entry,
    encode_head,
    entry_digest,
)
from jbt.storage.journal import (
    append_journal,
    initialize_journal,
    read_journal,
)
from jbt.storage.local import (
    StorageCode,
    StorageError,
    put_retained,
    writer_lock,
)

if TYPE_CHECKING:
    from pathlib import Path

BODY = b"synthetic decision body"
BODY_DIGEST = byte_digest(BODY)
EMPTY = JournalHead(0, None)


def _start(root: Path) -> None:
    with writer_lock(root) as lock:
        assert initialize_journal(root, lock=lock) == EMPTY
        assert put_retained(root, BODY, lock=lock) == BODY_DIGEST


def _record(head: JournalHead, key: str = "decision") -> JournalEntry:
    return JournalEntry(
        sequence=head.sequence + 1,
        previous_entry_digest=head.entry_digest,
        entity=EntityId("entity"),
        key=SemanticKey(key),
        kind=DecisionKind.ASSIGN,
        operation=JournalOperation.RECORD,
        prior_entry_digest=None,
        reviewed_attempt_digest="a" * 64,
        reason="synthetic reason",
        payload_digest=BODY_DIGEST,
    )


def _append(root: Path, entry: JournalEntry, head: JournalHead) -> JournalHead:
    with writer_lock(root) as lock:
        return append_journal(
            root, entry, expected_head=head, retained_heads=(head,), lock=lock
        )


def _entry_path(root: Path, sequence: int) -> Path:
    return root / "journal" / "entries" / f"{sequence:019d}.json"


def test_history_preserves_positions_seals_and_historical_selection(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    a = _record(EMPTY)
    first = _append(tmp_path, a, EMPTY)
    b = _record(first, "other")
    second = _append(tmp_path, b, first)
    replacement_body = b"synthetic replacement"
    with writer_lock(tmp_path) as lock:
        digest = put_retained(tmp_path, replacement_body, lock=lock)
    supersede = replace(
        a,
        sequence=3,
        previous_entry_digest=second.entry_digest,
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=first.entry_digest,
        payload_digest=digest,
    )
    third = _append(tmp_path, supersede, second)
    selection = read_journal(tmp_path, head=None, retained_heads=(first, second, third))
    assert selection.committed_entries == (a, b, supersede)
    assert [
        (item.entry.sequence, item.logical_position)
        for item in selection.active_decisions
    ] == [(3, 1), (2, 2)]
    retract = replace(
        supersede,
        sequence=4,
        previous_entry_digest=third.entry_digest,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=third.entry_digest,
        payload_digest=None,
    )
    fourth = _append(tmp_path, retract, third)
    live = read_journal(tmp_path, head=None, retained_heads=(first, third))
    assert live.head == fourth
    assert tuple(item.entry for item in live.active_decisions) == (b,)
    pinned = read_journal(tmp_path, head=first, retained_heads=(first,))
    assert pinned.committed_entries == (a,)
    assert pinned.unselected_entries == (b, supersede, retract)
    assert _entry_path(tmp_path, 1).read_bytes() == encode_entry(a)
    assert (tmp_path / "inputs" / BODY_DIGEST).read_bytes() == BODY
    assert (tmp_path / "inputs" / digest).read_bytes() == replacement_body
    # Pinned replay does not even open a damaged ambient live head.
    (tmp_path / "journal" / "head.json").write_bytes(b"damaged live head")
    assert read_journal(tmp_path, head=first, retained_heads=()).head == first
    with pytest.raises(StorageError, match=r"corrupt: journal\.head"):
        read_journal(tmp_path, head=None, retained_heads=())


def test_initialization_is_durable_idempotent_and_never_resets(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    with writer_lock(tmp_path) as lock:
        assert initialize_journal(tmp_path, lock=lock) == EMPTY
    assert read_journal(tmp_path, head=None, retained_heads=()).head == EMPTY
    first = _append(tmp_path, _record(EMPTY), EMPTY)
    with writer_lock(tmp_path) as lock, pytest.raises(StorageError, match="corrupt"):
        initialize_journal(tmp_path, lock=lock)
    assert read_journal(tmp_path, head=None, retained_heads=()).head == first


def test_uninitialized_store_is_not_an_empty_success(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="missing: journal"):
        read_journal(tmp_path, head=None, retained_heads=())


def test_initialization_cannot_turn_a_lost_commit_head_into_empty_history(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    first = _append(tmp_path, _record(EMPTY), EMPTY)
    head_path = tmp_path / "journal" / "head.json"
    head_path.unlink()
    with writer_lock(tmp_path) as lock, pytest.raises(StorageError, match="missing"):
        initialize_journal(tmp_path, lock=lock)
    assert not head_path.exists()
    assert read_journal(tmp_path, head=first, retained_heads=(first,)).head == first


def test_initialization_preserves_an_orphan_under_its_known_empty_head(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    orphan = _record(EMPTY)
    _entry_path(tmp_path, 1).write_bytes(encode_entry(orphan))
    with writer_lock(tmp_path) as lock:
        assert initialize_journal(tmp_path, lock=lock) == EMPTY
    selection = read_journal(tmp_path, head=None, retained_heads=())
    assert selection.committed_entries == ()
    assert selection.unselected_entries == (orphan,)


def test_uncertain_initial_head_installation_is_safely_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sync = os.fsync
    journal = tmp_path / "journal"

    def fail_sync(fd: int) -> None:
        if (journal / "head.json").exists() and os.fstat(
            fd
        ).st_ino == journal.stat().st_ino:
            raise OSError(errno.EIO, "synthetic initialization failure")
        real_sync(fd)

    with writer_lock(tmp_path) as lock:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_sync)
            with pytest.raises(
                StorageError, match=r"uncertain: journal\.head\.initialize"
            ):
                initialize_journal(tmp_path, lock=lock)
        installed_inode = (journal / "head.json").stat().st_ino
        assert initialize_journal(tmp_path, lock=lock) == EMPTY
        assert (journal / "head.json").stat().st_ino == installed_inode
    assert read_journal(tmp_path, head=None, retained_heads=()).head == EMPTY


def test_two_retained_heads_detect_valid_rollback_and_truncation(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    first = _append(tmp_path, _record(EMPTY, "a"), EMPTY)
    second = _append(tmp_path, _record(first, "b"), first)
    third = _append(tmp_path, _record(second, "c"), second)
    _entry_path(tmp_path, 3).unlink()
    (tmp_path / "journal" / "head.json").write_bytes(encode_head(second))
    with pytest.raises(JournalError, match="retained_head_extension_required"):
        read_journal(tmp_path, head=None, retained_heads=(first, third))
    assert read_journal(tmp_path, head=first, retained_heads=(first,)).head == first


def test_retained_seal_rejects_a_consistently_rewritten_chain(tmp_path: Path) -> None:
    _start(tmp_path)
    original = _record(EMPTY)
    retained = _append(tmp_path, original, EMPTY)
    edited = replace(original, reason="rewritten")
    edited_head = JournalHead(1, entry_digest(edited))
    _entry_path(tmp_path, 1).write_bytes(encode_entry(edited))
    (tmp_path / "journal" / "head.json").write_bytes(encode_head(edited_head))
    with pytest.raises(JournalError, match="head_digest_mismatch"):
        read_journal(tmp_path, head=None, retained_heads=(EMPTY, retained))
    with (
        writer_lock(tmp_path) as lock,
        pytest.raises(JournalError, match="head_digest_mismatch"),
    ):
        append_journal(
            tmp_path,
            _record(edited_head, "later"),
            expected_head=edited_head,
            retained_heads=(retained,),
            lock=lock,
        )
    assert not _entry_path(tmp_path, 2).exists()


def test_orphan_is_explicit_and_only_its_exact_retry_can_commit(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    orphan = _record(EMPTY)
    _entry_path(tmp_path, 1).write_bytes(encode_entry(orphan))
    selection = read_journal(tmp_path, head=None, retained_heads=())
    assert selection.committed_entries == selection.active_decisions == ()
    assert selection.unselected_entries == (orphan,)
    with pytest.raises(StorageError, match=r"conflict: journal\.suffix"):
        _append(tmp_path, _record(EMPTY, "different"), EMPTY)
    assert _entry_path(tmp_path, 1).read_bytes() == encode_entry(orphan)
    first = _append(tmp_path, orphan, EMPTY)
    assert first == JournalHead(1, entry_digest(orphan))
    assert _append(tmp_path, orphan, EMPTY) == first
    assert read_journal(tmp_path, head=None, retained_heads=()).unselected_entries == ()


def test_retry_does_not_promote_or_delete_additional_suffix(tmp_path: Path) -> None:
    _start(tmp_path)
    entry = _record(EMPTY, "a")
    first = JournalHead(1, entry_digest(entry))
    later = _record(first, "b")
    _entry_path(tmp_path, 1).write_bytes(encode_entry(entry))
    _entry_path(tmp_path, 2).write_bytes(encode_entry(later))
    with pytest.raises(StorageError, match=r"conflict: journal\.suffix"):
        _append(tmp_path, entry, EMPTY)
    assert read_journal(tmp_path, head=None, retained_heads=()).unselected_entries == (
        entry,
        later,
    )


@pytest.mark.parametrize(
    "damage",
    [
        "entry_edit",
        "entry_wire",
        "head_digest",
        "head_wire",
        "gap",
        "truncate",
        "chain",
        "sequence",
        "inventory",
    ],
)
def test_opened_canonical_headers_and_chain_are_verified(
    tmp_path: Path,
    damage: str,
) -> None:
    _start(tmp_path)
    a = _record(EMPTY, "a")
    first = _append(tmp_path, a, EMPTY)
    b = _record(first, "b")
    second = _append(tmp_path, b, first)
    match damage:
        case "entry_edit":
            _entry_path(tmp_path, 1).write_bytes(
                encode_entry(replace(a, reason="changed"))
            )
        case "entry_wire":
            _entry_path(tmp_path, 1).write_bytes(encode_entry(a) + b"\n")
        case "head_digest":
            (tmp_path / "journal" / "head.json").write_bytes(
                encode_head(JournalHead(2, "f" * 64))
            )
        case "head_wire":
            (tmp_path / "journal" / "head.json").write_bytes(encode_head(second)[:-1])
        case "gap":
            _entry_path(tmp_path, 1).unlink()
        case "truncate":
            _entry_path(tmp_path, 2).unlink()
        case "chain":
            _entry_path(tmp_path, 2).write_bytes(
                encode_entry(replace(b, previous_entry_digest="f" * 64))
            )
        case "sequence":
            _entry_path(tmp_path, 2).write_bytes(encode_entry(a))
        case "inventory":
            (tmp_path / "journal" / "entries" / "unexpected").write_bytes(b"")
        case _:
            pytest.fail("unknown damage")
    with pytest.raises((StorageError, JournalError)):
        read_journal(tmp_path, head=None, retained_heads=())


@pytest.mark.parametrize("damage", ["missing", "edited"])
def test_every_selected_body_is_retained_and_sealed(
    tmp_path: Path,
    damage: str,
) -> None:
    _start(tmp_path)
    entry = _record(EMPTY)
    first = _append(tmp_path, entry, EMPTY)
    retract = replace(
        entry,
        sequence=2,
        previous_entry_digest=first.entry_digest,
        operation=JournalOperation.RETRACT,
        prior_entry_digest=first.entry_digest,
        payload_digest=None,
    )
    _append(tmp_path, retract, first)
    body = tmp_path / "inputs" / BODY_DIGEST
    if damage == "missing":
        body.unlink()
    else:
        body.write_bytes(b"modified")
    with pytest.raises(StorageError, match=r"missing|corrupt"):
        read_journal(tmp_path, head=None, retained_heads=())


@pytest.mark.parametrize("damage", ["missing", "edited"])
def test_append_requires_body_before_installing_entry(
    tmp_path: Path,
    damage: str,
) -> None:
    _start(tmp_path)
    body = tmp_path / "inputs" / BODY_DIGEST
    if damage == "missing":
        body.unlink()
    else:
        body.write_bytes(b"modified")
    with pytest.raises(StorageError, match=r"missing|corrupt"):
        _append(tmp_path, _record(EMPTY), EMPTY)
    assert not _entry_path(tmp_path, 1).exists()
    assert read_journal(tmp_path, head=None, retained_heads=()).head == EMPTY


def test_stale_expected_head_and_structurally_invalid_append_fail(
    tmp_path: Path,
) -> None:
    _start(tmp_path)
    first = _append(tmp_path, _record(EMPTY), EMPTY)
    with pytest.raises(StorageError, match=r"conflict: journal\.expected_head"):
        _append(tmp_path, _record(EMPTY, "other"), EMPTY)
    with pytest.raises(JournalError, match="duplicate_record_key"):
        _append(tmp_path, _record(first), first)
    with pytest.raises(JournalError, match="next_sequence_required"):
        _append(tmp_path, replace(_record(first), sequence=3), first)
    with pytest.raises(JournalError, match="expected_previous_required"):
        _append(
            tmp_path,
            replace(_record(first), previous_entry_digest="f" * 64),
            first,
        )
    assert not _entry_path(tmp_path, 2).exists()


def test_writer_exclusion_and_expired_token_gate_append(tmp_path: Path) -> None:
    _start(tmp_path)
    with writer_lock(tmp_path) as lock:
        with pytest.raises(StorageError, match="locked"), writer_lock(tmp_path):
            pytest.fail("competing writer acquired lock")
        with pytest.raises(StorageError, match="locked"):
            _append(tmp_path, _record(EMPTY), EMPTY)
    with pytest.raises(StorageError, match="lock_required"):
        append_journal(
            tmp_path,
            _record(EMPTY),
            expected_head=EMPTY,
            retained_heads=(),
            lock=lock,
        )
    with pytest.raises(StorageError, match="lock_required"):
        initialize_journal(tmp_path, lock=lock)


@pytest.mark.parametrize("target", ["journal", "entries", "head", "entry", "body"])
@pytest.mark.parametrize("replacement", ["symlink", "nonregular"])
def test_paths_refuse_symlinks_and_nonregular_files(
    tmp_path: Path,
    target: str,
    replacement: str,
) -> None:
    _start(tmp_path)
    first = _append(tmp_path, _record(EMPTY), EMPTY)
    paths = {
        "journal": tmp_path / "journal",
        "entries": tmp_path / "journal" / "entries",
        "head": tmp_path / "journal" / "head.json",
        "entry": _entry_path(tmp_path, 1),
        "body": tmp_path / "inputs" / BODY_DIGEST,
    }
    path = paths[target]
    original = tmp_path / "original"
    path.rename(original)
    if replacement == "symlink":
        path.symlink_to(original, target_is_directory=original.is_dir())
    elif target in {"journal", "entries"}:
        path.write_bytes(b"not a directory")
    else:
        os.mkfifo(path)
    with pytest.raises(StorageError, match="path"):
        read_journal(tmp_path, head=None, retained_heads=(first,))
    with pytest.raises(StorageError, match="path"):
        _append(tmp_path, _record(first, "other"), first)


@pytest.mark.parametrize("phase", ["file", "directory"])
def test_entry_sync_failure_never_advances_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _start(tmp_path)
    entry = _record(EMPTY)
    directory_inode = (tmp_path / "journal" / "entries").stat().st_ino
    body_inode = (tmp_path / "inputs" / BODY_DIGEST).stat().st_ino
    real_sync = os.fsync

    def fail_sync(fd: int) -> None:
        info = os.fstat(fd)
        if (phase == "directory" and info.st_ino == directory_inode) or (
            phase == "file" and stat.S_ISREG(info.st_mode) and info.st_ino != body_inode
        ):
            raise OSError(errno.EIO, "synthetic sync failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(StorageError):
            _append(tmp_path, entry, EMPTY)
    selection = read_journal(tmp_path, head=None, retained_heads=())
    assert selection.head == EMPTY
    assert selection.committed_entries == ()
    assert selection.unselected_entries == (() if phase == "file" else (entry,))
    assert _append(tmp_path, entry, EMPTY) == JournalHead(1, entry_digest(entry))


@pytest.mark.parametrize("phase", ["payload", "head_prepare"])
def test_prerequisite_sync_failure_never_advances_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _start(tmp_path)
    entry = _record(EMPTY)
    body_inode = (tmp_path / "inputs" / BODY_DIGEST).stat().st_ino
    real_sync = os.fsync

    def fail_sync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and (
            (phase == "payload" and info.st_ino == body_inode)
            or (
                phase == "head_prepare"
                and _entry_path(tmp_path, 1).exists()
                and info.st_ino
                not in {body_inode, _entry_path(tmp_path, 1).stat().st_ino}
            )
        ):
            raise OSError(errno.EIO, "synthetic prerequisite failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(StorageError):
            _append(tmp_path, entry, EMPTY)
    selection = read_journal(tmp_path, head=None, retained_heads=())
    assert selection.head == EMPTY
    assert selection.unselected_entries == (() if phase == "payload" else (entry,))
    assert _append(tmp_path, entry, EMPTY) == JournalHead(1, entry_digest(entry))


@pytest.mark.parametrize("phase", ["replace_before", "replace_after", "head_sync"])
def test_head_failure_is_explicitly_uncertain_and_exact_retry_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _start(tmp_path)
    entry = _record(EMPTY)
    first = JournalHead(1, entry_digest(entry))
    journal_inode = (tmp_path / "journal").stat().st_ino
    real_replace = os.replace
    real_sync = os.fsync
    replaced = False

    def fail_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal replaced
        if phase == "replace_before":
            raise OSError(errno.EIO, "synthetic replace failure")
        real_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        replaced = True
        if phase == "replace_after":
            raise OSError(errno.EIO, "synthetic acknowledgement failure")

    def fail_sync(fd: int) -> None:
        if phase == "head_sync" and replaced and os.fstat(fd).st_ino == journal_inode:
            raise OSError(errno.EIO, "synthetic head sync failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(StorageError) as failure:
            _append(tmp_path, entry, EMPTY)
        assert failure.value.code is StorageCode.UNCERTAIN
        assert failure.value.location == "journal.head"
    selection = read_journal(tmp_path, head=None, retained_heads=())
    assert selection.head == (EMPTY if phase == "replace_before" else first)
    assert selection.unselected_entries == (
        (entry,) if phase == "replace_before" else ()
    )
    installed_inode = _entry_path(tmp_path, 1).stat().st_ino
    assert _append(tmp_path, entry, EMPTY) == first
    assert _entry_path(tmp_path, 1).stat().st_ino == installed_inode
    assert read_journal(tmp_path, head=None, retained_heads=()).head == first


def test_already_committed_retry_requires_fresh_durability_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(tmp_path)
    entry = _record(EMPTY)
    first = _append(tmp_path, entry, EMPTY)
    head_inode = (tmp_path / "journal" / "head.json").stat().st_ino
    real_sync = os.fsync

    def fail_sync(fd: int) -> None:
        if os.fstat(fd).st_ino == head_inode:
            raise OSError(errno.EIO, "synthetic retry failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(StorageError, match=r"uncertain: journal\.head"):
            _append(tmp_path, entry, EMPTY)
    assert read_journal(tmp_path, head=None, retained_heads=()).head == first
    assert _append(tmp_path, entry, EMPTY) == first


def test_live_reader_samples_head_before_a_concurrent_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(tmp_path)
    a = _record(EMPTY, "a")
    first = _append(tmp_path, a, EMPTY)
    b = _record(first, "b")
    entries_inode = (tmp_path / "journal" / "entries").stat().st_ino
    real_list = os.listdir
    appended = False

    def append_during_list(fd: int) -> list[str]:
        nonlocal appended
        if os.fstat(fd).st_ino == entries_inode and not appended:
            appended = True
            _append(tmp_path, b, first)
        return real_list(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", append_during_list)
        selection = read_journal(tmp_path, head=None, retained_heads=(first,))
    assert selection.head == first
    assert selection.committed_entries == (a,)
    assert selection.unselected_entries == (b,)
