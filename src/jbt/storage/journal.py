"""Durable journal custody, without decision-body interpretation.

The head replacement commits an append. An ``uncertain`` error located
at ``journal.head`` means acknowledgement is uncertain, not rollback.
Entry/body/head-preparation failures precede commit. Never infer rollback
from uncertain acknowledgement: retry the exact entry with its original
expected head. Conflicting or additional orphans are never removed or
promoted. Readers must supply all applicable retained heads.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager, suppress
from secrets import token_hex
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import ArtifactIntegrityError, byte_digest
from jbt.artifacts.journal import decode_entry, decode_head
from jbt.contracts.primitives import ContractError
from jbt.domain.journal import (
    JournalEntry,
    JournalError,
    JournalHead,
    JournalSelection,
    encode_entry,
    encode_head,
    entry_digest,
    select_journal,
)
from jbt.storage.local import (
    StorageCode,
    StorageError,
    WriterLock,
    _directory,
    _immutable,
    _open_directory,
    _read,
    _require_lock,
    _sync_installed,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def _journal(
    root: Path, *, lock: WriterLock | None = None, create: bool = False
) -> Iterator[tuple[int, int, int]]:
    owned = lock is None
    try:
        root_fd = _open_directory(root) if lock is None else _require_lock(root, lock)
    except StorageError as error:
        raise StorageError(error.code, "journal.root") from error
    except OSError as error:
        raise StorageError(StorageCode.UNCERTAIN, "journal.root") from error
    try:
        journal_fd = _directory(root_fd, "journal", create=create)
        try:
            entries_fd = _directory(journal_fd, "entries", create=create)
            try:
                yield root_fd, journal_fd, entries_fd
            finally:
                os.close(entries_fd)
        finally:
            os.close(journal_fd)
    except FileNotFoundError as error:
        raise StorageError(StorageCode.MISSING, "journal") from error
    except StorageError as error:
        if error.location is not None:
            raise
        raise StorageError(error.code, "journal") from error
    except OSError as error:
        raise StorageError(StorageCode.UNCERTAIN, "journal.io") from error
    finally:
        if owned:
            os.close(root_fd)


def _name(sequence: int) -> str:
    return f"{sequence:019d}.json"


def _entries(entries_fd: int) -> tuple[JournalEntry, ...]:
    names = sorted(os.listdir(entries_fd))
    entries: list[JournalEntry] = []
    for sequence, name in enumerate(names, start=1):
        if re.fullmatch(r"[0-9]{19}\.json", name) is None:
            raise StorageError(StorageCode.INVENTORY, "journal.entries")
        if name != _name(sequence):
            raise StorageError(StorageCode.CORRUPT, "journal.entries.sequence")
        try:
            entry = decode_entry(_read(entries_fd, name))
        except (ArtifactIntegrityError, ContractError) as error:
            raise StorageError(StorageCode.CORRUPT, "journal.entry") from error
        if entry.sequence != sequence:
            raise StorageError(StorageCode.CORRUPT, "journal.entry.sequence")
        entries.append(entry)
    return tuple(entries)


def _head(journal_fd: int) -> JournalHead:
    try:
        return decode_head(_read(journal_fd, "head.json"))
    except (ArtifactIntegrityError, ContractError) as error:
        raise StorageError(StorageCode.CORRUPT, "journal.head") from error


def _bodies(
    root_fd: int, entries: tuple[JournalEntry, ...], *, sync: bool = False
) -> None:
    digests = {
        entry.payload_digest for entry in entries if entry.payload_digest is not None
    }
    if not digests:
        return
    inputs_fd = _directory(root_fd, "inputs")
    try:
        for digest in sorted(digests):
            if byte_digest(_read(inputs_fd, digest)) != digest:
                raise StorageError(StorageCode.CORRUPT, "journal.payload")
            if sync:
                _sync_installed(inputs_fd, digest)
    finally:
        os.close(inputs_fd)


def _selection(
    root_fd: int,
    journal_fd: int,
    entries_fd: int,
    *,
    head: JournalHead | None,
    retained_heads: tuple[JournalHead, ...],
) -> JournalSelection:
    selected_head = _head(journal_fd) if head is None else head
    selection = select_journal(
        _entries(entries_fd),
        head=selected_head,
        retained_heads=retained_heads,
    )
    _bodies(root_fd, selection.committed_entries)
    return selection


def initialize_journal(root: Path, *, lock: WriterLock) -> JournalHead:
    """Durably install an empty head; never reset an existing journal.

    Repeating initialization of an empty journal re-establishes durability.
    Existing nonempty or lost heads fail; known empty-head suffixes remain.
    """
    if not isinstance(lock, WriterLock):
        raise StorageError(StorageCode.LOCK_REQUIRED, "journal.lock")
    empty = JournalHead(0, None)
    with _journal(root, lock=lock, create=True) as (_, journal_fd, entries_fd):
        entries = _entries(entries_fd)
        if entries and _head(journal_fd) != empty:
            raise StorageError(StorageCode.CORRUPT, "journal.head.initialize")
        select_journal(entries, head=empty, retained_heads=())
        try:
            _immutable(journal_fd, "head.json", encode_head(empty))
        except StorageError as error:
            raise StorageError(error.code, "journal.head.initialize") from error
        except OSError as error:
            raise StorageError(
                StorageCode.UNCERTAIN, "journal.head.initialize"
            ) from error
    return empty


def read_journal(
    root: Path,
    *,
    head: JournalHead | None,
    retained_heads: tuple[JournalHead, ...],
) -> JournalSelection:
    """Select live (``None``) or an explicit pin without taking a lock.

    Pinned reads never open the live head. All physical headers, including
    the explicit unselected suffix, must form a canonical contiguous chain.
    Every committed body's retained bytes must match its header's seal.
    Runtime owns body/base authentication and retained-head completeness.
    """
    with _journal(root) as (root_fd, journal_fd, entries_fd):
        return _selection(
            root_fd,
            journal_fd,
            entries_fd,
            head=head,
            retained_heads=retained_heads,
        )


def _replace_head(journal_fd: int, head: JournalHead) -> None:
    temporary = f".head-{token_hex(16)}"
    try:
        try:
            _immutable(journal_fd, temporary, encode_head(head))
        except (OSError, StorageError) as error:
            raise StorageError(StorageCode.UNCERTAIN, "journal.head.prepare") from error
        try:
            os.replace(
                temporary, "head.json", src_dir_fd=journal_fd, dst_dir_fd=journal_fd
            )
            os.fsync(journal_fd)
        except OSError as error:
            raise StorageError(StorageCode.UNCERTAIN, "journal.head") from error
    finally:
        try:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=journal_fd)
        except OSError as error:
            raise StorageError(StorageCode.UNCERTAIN, "journal.head") from error


def _validate_append(
    selection: JournalSelection, entry: JournalEntry, expected_head: JournalHead
) -> JournalHead:
    if entry.sequence != expected_head.sequence + 1:
        raise JournalError(
            constraint="next_sequence_required", location="journal.append.sequence"
        )
    if entry.previous_entry_digest != expected_head.entry_digest:
        raise JournalError(
            constraint="expected_previous_required",
            location="journal.append.previous_entry_digest",
        )
    next_head = JournalHead(entry.sequence, entry_digest(entry))
    if selection.head == next_head:
        if selection.unselected_entries:
            raise StorageError(StorageCode.CONFLICT, "journal.suffix")
        # The original head must still be the exact prefix of this retry.
        select_journal(
            selection.committed_entries,
            head=expected_head,
            retained_heads=(),
        )
        return next_head
    if selection.head != expected_head:
        raise StorageError(StorageCode.CONFLICT, "journal.expected_head")
    if selection.unselected_entries and selection.unselected_entries != (entry,):
        raise StorageError(StorageCode.CONFLICT, "journal.suffix")
    select_journal(
        (*selection.committed_entries, entry),
        head=next_head,
        retained_heads=(expected_head,),
    )
    return next_head


def append_journal(
    root: Path,
    entry: JournalEntry,
    *,
    expected_head: JournalHead,
    retained_heads: tuple[JournalHead, ...],
    lock: WriterLock,
) -> JournalHead:
    """Validate, sync the immutable entry/body, then commit the new head.

    The caller holds the root writer lock and supplies a prior head.
    An identical orphan can be explicitly retried. If the exact append is
    already live after uncertain acknowledgement, retry only syncs it.
    All other stale heads or conflicting suffixes fail without alteration.
    """
    if not isinstance(lock, WriterLock):
        raise StorageError(StorageCode.LOCK_REQUIRED, "journal.lock")
    if not isinstance(entry, JournalEntry):
        raise JournalError(
            constraint="typed_entry_required", location="journal.append.entry"
        )
    if not isinstance(expected_head, JournalHead):
        raise JournalError(
            constraint="typed_head_required", location="journal.append.expected_head"
        )
    with _journal(root, lock=lock) as (root_fd, journal_fd, entries_fd):
        selection = _selection(
            root_fd,
            journal_fd,
            entries_fd,
            head=None,
            retained_heads=retained_heads,
        )
        next_head = _validate_append(selection, entry, expected_head)
        _bodies(root_fd, (entry,), sync=True)
        try:
            _immutable(entries_fd, _name(entry.sequence), encode_entry(entry))
        except StorageError as error:
            raise StorageError(error.code, "journal.entry") from error
        except OSError as error:
            raise StorageError(StorageCode.UNCERTAIN, "journal.entry") from error
        if selection.head == next_head:
            try:
                _sync_installed(journal_fd, "head.json")
            except (OSError, StorageError) as error:
                raise StorageError(StorageCode.UNCERTAIN, "journal.head") from error
        else:
            _replace_head(journal_fd, next_head)
    return next_head
