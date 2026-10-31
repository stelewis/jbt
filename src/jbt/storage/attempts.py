"""Explicit attempt witnesses and immutable report references.

The index covers every selection witness. Orphans, lost indexes and edited
references fail closed rather than being discovered as financial inputs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from secrets import token_hex
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    byte_digest,
    decode_document,
    require_digest,
)
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.storage.local import (
    StorageCode,
    StorageError,
    WriterLock,
    _directory,
    _immutable,
    _open_directory,
    _read,
    _require_lock,
    put_retained,
    read_retained,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def _attempts(root: Path, lock: WriterLock | None = None) -> Iterator[int]:
    owned = lock is None
    parent = _open_directory(root) if owned else _require_lock(root, lock)
    directory = None
    try:
        marker = encode_json({"schema_version": 1}, integer_strings=False)
        try:
            installed = _read(parent, "attempts.version")
        except FileNotFoundError:
            installed = None
        require(
            installed is None or installed == marker,
            "attempt_store_version",
            "attempts",
        )
        try:
            directory = _directory(parent, "attempts")
        except FileNotFoundError:
            require(lock is not None, "attempt_index_missing", "attempts")
            require(installed is None, "attempt_index_lost", "attempts")
            directory = _directory(parent, "attempts", create=True)
            _immutable(
                directory,
                "index.json",
                encode_json({"attempts": []}, integer_strings=False),
            )
            _immutable(parent, "attempts.version", marker)
        else:
            require(installed == marker, "attempt_store_marker_lost", "attempts")
        try:
            yield directory
        except (ArtifactIntegrityError, ContractError) as error:
            raise StorageError(StorageCode.CORRUPT, "attempts") from error
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING, "attempts") from error
    finally:
        if directory is not None:
            os.close(directory)
        if owned:
            os.close(parent)


def _index(directory: int) -> tuple[str, ...]:
    record = decode_document(_read(directory, "index.json"), "attempts.index")
    require(set(record) == {"attempts"}, "attempt_index_schema", "attempts")
    value = record["attempts"]
    if not isinstance(value, list):
        code = "attempt_index_schema"
        raise ContractError(code, "attempts")
    selected = tuple(_digest(item) for item in value)
    require(len(selected) == len(set(selected)), "attempt_index_duplicate", "attempts")
    for digest in selected:
        require_digest(digest)
    names = set(os.listdir(directory))
    allowed = {"index.json", *(f"{digest}.selection" for digest in selected)}
    allowed.update(f"{digest}.report" for digest in selected)
    require(
        {f"{digest}.selection" for digest in selected} <= names <= allowed,
        "attempt_index_inventory",
        "attempts",
    )
    return selected


def selections(root: Path, *, lock: WriterLock) -> tuple[bytes, ...]:
    """Authenticate every earlier applicable selection, including failures."""
    with _attempts(root, lock) as directory:
        results = []
        for ordinal, digest in enumerate(_index(directory), start=1):
            payload = _read(directory, f"{digest}.selection")
            require(
                byte_digest(payload) == digest, "attempt_witness_digest", "attempts"
            )
            require(
                read_retained(root, digest) == payload,
                "attempt_witness_changed",
                "attempts",
            )
            witness = decode_document(payload, "attempt.witness")
            require(
                set(witness) == {"schema_version", "input_digest", "ordinal"}
                and witness["schema_version"] == 1
                and type(witness["ordinal"]) is int
                and witness["ordinal"] == ordinal,
                "attempt_witness_schema",
                "attempts",
            )
            results.append(read_retained(root, _digest(witness["input_digest"])))
        return tuple(results)


def retain_attempt(root: Path, input_digest: str, *, lock: WriterLock) -> str:
    """Give every invocation an immutable witness, independent of input identity."""
    require_digest(input_digest)
    read_retained(root, input_digest)
    with _attempts(root, lock) as directory:
        index = _index(directory)
        payload = encode_json(
            {
                "schema_version": 1,
                "input_digest": input_digest,
                "ordinal": len(index) + 1,
            },
            integer_strings=False,
        )
        digest = put_retained(root, payload, lock=lock)
        _immutable(directory, f"{digest}.selection", payload)
        if digest not in index:
            name = f".index-{token_hex(16)}"
            try:
                _immutable(
                    directory,
                    name,
                    encode_json({"attempts": [*index, digest]}, integer_strings=False),
                )
                os.replace(
                    name, "index.json", src_dir_fd=directory, dst_dir_fd=directory
                )
                os.fsync(directory)
            finally:
                if name in os.listdir(directory):  # noqa: PTH208 - fd-relative custody
                    os.unlink(name, dir_fd=directory)
    return digest


def retain_report(root: Path, attempt: str, payload: bytes, *, lock: WriterLock) -> str:
    """Seal a report separately from its input lineage; never replace it."""
    require_digest(attempt)
    digest = put_retained(root, payload, lock=lock)
    with _attempts(root, lock) as directory:
        require(attempt in _index(directory), "attempt_not_selected", "attempts")
        _immutable(
            directory,
            f"{attempt}.report",
            encode_json({"report_digest": digest}, integer_strings=False),
        )
    return digest


def read_report(root: Path, attempt: str) -> bytes:
    """Read by explicit attempt ID without opening live project or journal."""
    require_digest(attempt)
    with _attempts(root) as directory:
        witness = _read(directory, f"{attempt}.selection")
        require(byte_digest(witness) == attempt, "attempt_witness_digest", "attempts")
        record = decode_document(
            _read(directory, f"{attempt}.report"), "attempt.report"
        )
        require(
            type(record) is dict and set(record) == {"report_digest"},
            "attempt_report_reference",
            "attempts",
        )
        digest = record["report_digest"]
        return read_retained(root, _digest(digest))


def _digest(value: object) -> str:
    if not isinstance(value, str):
        code = "attempt_digest_required"
        raise ContractError(code, "attempts")
    require_digest(value)
    return value
