"""Explicitly locked, durable local evidence and publication transactions."""

from __future__ import annotations

import errno
import fcntl
import os
import platform
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from secrets import token_hex
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    byte_digest,
    decode_document,
    require_digest,
)
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import descriptor_schema
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class StorageCode(StrEnum):
    """Named failures at the local storage boundary."""

    PLATFORM = "platform"
    PATH = "path"
    LOCKED = "locked"
    LOCK_REQUIRED = "lock_required"
    CONFLICT = "conflict"
    CHANGED = "changed"
    MISSING = "missing"
    CORRUPT = "corrupt"
    INVENTORY = "inventory"
    UNCERTAIN = "uncertain"


@dataclass
class StorageError(ValueError):
    """Named failure with optional safe, bounded diagnostic context."""

    code: StorageCode
    location: str | None = None

    def __str__(self) -> str:
        """Return a safe diagnostic."""
        if self.location is None:
            return f"local storage: {self.code.value}"
        return f"local storage: {self.code.value}: {self.location}"


@dataclass
class WriterLock:
    """Token valid only during its writer_lock context."""

    root: Path
    directory_fd: int
    lock_fd: int
    pid: int
    active: bool = True


@dataclass(frozen=True)
class Acquisition:
    """Committed accession identity and its independently verified object."""

    retry_key: str
    source_scope_id: str | None
    importer_id: str | None
    acquired_at: str
    original_filename: str
    object_digest: str
    size: int
    accession_id: str


@dataclass(frozen=True)
class Current:
    """A pinned immutable generation, independent of later pointer changes."""

    path: Path
    descriptor_digest: str


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise StorageError(StorageCode.PATH)
    return tuple(part for part in path.parts if part != "/")


def _open_directory(path: Path) -> int:
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in _parts(path):
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise StorageError(StorageCode.PATH) from error
                if error.errno == errno.ENOENT:
                    raise StorageError(StorageCode.MISSING) from error
                raise
            os.close(fd)
            fd = next_fd
        return os.dup(fd)
    finally:
        os.close(fd)


def _directory(parent: int, name: str, *, create: bool = False) -> int:
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StorageError(StorageCode.PATH) from error
        raise


def _chunks(fd: int, size: int) -> Iterator[bytes]:
    remaining = size + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


def _read(parent: int, name: str, *, expected_size: int | None = None) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StorageError(StorageCode.PATH) from error
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StorageError(StorageCode.PATH)
        if expected_size is not None and before.st_size != expected_size:
            raise StorageError(StorageCode.CORRUPT)
        chunks = list(_chunks(fd, before.st_size))
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or sum(map(len, chunks)) != after.st_size:
            raise StorageError(StorageCode.CHANGED)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fingerprint(
    parent: int, name: str, *, expected_size: int | None = None
) -> tuple[str, int]:
    fd = _opened_file(parent, name)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StorageError(StorageCode.PATH)
        if expected_size is not None and before.st_size != expected_size:
            raise StorageError(StorageCode.CORRUPT)
        digest = sha256()
        size = 0
        for chunk in _chunks(fd, before.st_size):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != after.st_size:
            raise StorageError(StorageCode.CHANGED)
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _opened_file(parent: int, name: str) -> int:
    try:
        return os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError as error:
        raise StorageError(StorageCode.MISSING) from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StorageError(StorageCode.PATH) from error
        raise


def _json(parent: int, name: str) -> dict[str, object]:
    try:
        return decode_document(_read(parent, name), name)
    except ArtifactIntegrityError as error:
        raise StorageError(StorageCode.CORRUPT) from error


def _write_new(parent: int, name: str, payload: bytes) -> None:
    temporary = f".tmp-{token_hex(16)}"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    linked = False
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
        os.fsync(fd)
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        linked = True
        os.fsync(parent)
    except OSError as error:
        if linked:
            raise StorageError(StorageCode.UNCERTAIN) from error
        raise
    finally:
        os.close(fd)
        os.unlink(temporary, dir_fd=parent)


def _immutable(parent: int, name: str, payload: bytes) -> None:
    try:
        existing = _read(parent, name)
    except FileNotFoundError:
        try:
            _write_new(parent, name, payload)
        except FileExistsError:
            existing = _read(parent, name)
        else:
            return
    if existing != payload:
        raise StorageError(StorageCode.CORRUPT)
    _sync_installed(parent, name)


def _sync_installed(parent: int, name: str) -> None:
    """Re-establish durability even after an earlier uncertain installation."""
    fd = _opened_file(parent, name)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StorageError(StorageCode.PATH)
        try:
            os.fsync(fd)
            os.fsync(parent)
        except OSError as error:
            raise StorageError(StorageCode.UNCERTAIN) from error
    finally:
        os.close(fd)


@contextmanager
def writer_lock(root: Path) -> Iterator[WriterLock]:
    """Refuse competing project writers; caller owns transaction scope."""
    if platform.system() not in {"Darwin", "Linux"}:
        raise StorageError(StorageCode.PLATFORM)
    directory = _open_directory(root)
    try:
        try:
            lock_fd = os.open(
                ".writer.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise StorageError(StorageCode.PATH) from error
            raise
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise StorageError(StorageCode.PATH)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StorageError(StorageCode.LOCKED) from error
            os.fsync(directory)
            token = WriterLock(root, directory, lock_fd, os.getpid())
            try:
                yield token
            finally:
                token.active = False
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory)


def _require_lock(root: Path, lock: WriterLock) -> int:
    if root != lock.root or not lock.active or lock.pid != os.getpid():
        raise StorageError(StorageCode.LOCK_REQUIRED)
    try:
        opened = os.fstat(lock.directory_fd)
        named = root.stat(follow_symlinks=False)
        held = os.fstat(lock.lock_fd)
        lock_name = os.stat(
            ".writer.lock", dir_fd=lock.directory_fd, follow_symlinks=False
        )
        if (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ) or not stat.S_ISREG(held.st_mode):
            raise StorageError(StorageCode.LOCK_REQUIRED)
        if (held.st_dev, held.st_ino) != (lock_name.st_dev, lock_name.st_ino):
            raise StorageError(StorageCode.LOCK_REQUIRED)
    except OSError as error:
        raise StorageError(StorageCode.LOCK_REQUIRED) from error
    return lock.directory_fd


def _text(value: str | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StorageError(StorageCode.PATH)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise StorageError(StorageCode.PATH) from error


def _acquisition_data(acquisition: Acquisition) -> dict[str, object]:
    return {
        "retry_key": acquisition.retry_key,
        "source_scope_id": acquisition.source_scope_id,
        "importer_id": acquisition.importer_id,
        "acquired_at": acquisition.acquired_at,
        "original_filename": acquisition.original_filename,
        "object_digest": acquisition.object_digest,
        "size": acquisition.size,
    }


def _record(acquisition: Acquisition) -> bytes:
    return encode_json(
        {**_acquisition_data(acquisition), "accession_id": acquisition.accession_id},
        integer_strings=False,
    )


def _validated_acquisition(document: dict[str, object]) -> Acquisition:
    names = {
        "retry_key",
        "source_scope_id",
        "importer_id",
        "acquired_at",
        "original_filename",
        "object_digest",
        "size",
        "accession_id",
    }
    if set(document) != names:
        raise StorageError(StorageCode.CORRUPT)
    retry_key = document["retry_key"]
    scope = document["source_scope_id"]
    importer = document["importer_id"]
    acquired_at = document["acquired_at"]
    filename = document["original_filename"]
    digest = document["object_digest"]
    size = document["size"]
    accession_id = document["accession_id"]
    if (
        not isinstance(retry_key, str)
        or not isinstance(acquired_at, str)
        or not isinstance(filename, str)
        or not isinstance(digest, str)
        or not isinstance(accession_id, str)
        or (scope is not None and not isinstance(scope, str))
        or (importer is not None and not isinstance(importer, str))
        or type(size) is not int
        or size < 0
    ):
        raise StorageError(StorageCode.CORRUPT)
    try:
        _text(retry_key)
        _text(acquired_at)
        _text(scope, optional=True)
        _text(importer, optional=True)
        _text(filename)
        require_digest(digest)
        require_digest(accession_id)
    except (StorageError, ArtifactIntegrityError) as error:
        raise StorageError(StorageCode.CORRUPT) from error
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise StorageError(StorageCode.CORRUPT)
    item = Acquisition(
        retry_key,
        scope,
        importer,
        acquired_at,
        filename,
        digest,
        size,
        accession_id,
    )
    calculated = byte_digest(
        encode_json(_acquisition_data(item), integer_strings=False)
    )
    if accession_id != calculated:
        raise StorageError(StorageCode.CORRUPT)
    return item


def _verify_object(root_fd: int, digest: str, size: int) -> None:
    try:
        objects = _directory(root_fd, "objects")
        try:
            actual_digest, actual_size = _fingerprint(
                objects, digest, expected_size=size
            )
        finally:
            os.close(objects)
    except FileNotFoundError as error:
        raise StorageError(StorageCode.MISSING) from error
    if actual_size != size or actual_digest != digest:
        raise StorageError(StorageCode.CORRUPT)


def acquire(  # noqa: PLR0913 - explicit caller-owned lock and acquisition facts
    root: Path,
    source: Path,
    *,
    retry_key: str,
    acquired_at: str,
    lock: WriterLock,
    source_scope_id: str | None,
    importer_id: str | None,
) -> Acquisition:
    """Retain source bytes and commit a distinct immutable accession."""
    root_fd = _require_lock(root, lock)
    for value in (retry_key, acquired_at):
        _text(value)
    for value in (source_scope_id, importer_id):
        _text(value, optional=True)
    if not _parts(source) or source == root or root in source.parents:
        raise StorageError(StorageCode.PATH)
    _text(source.name)
    parent = _open_directory(source.parent)
    try:
        source_fd = _opened_file(parent, source.name)
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise StorageError(StorageCode.PATH)
            return _acquire_open(
                root_fd,
                source,
                parent,
                source_fd,
                retry_key=retry_key,
                acquired_at=acquired_at,
                source_scope_id=source_scope_id,
                importer_id=importer_id,
            )
        finally:
            os.close(source_fd)
    finally:
        os.close(parent)


def _acquire_open(  # noqa: PLR0913 - source and lock descriptors stay explicit
    root_fd: int,
    source: Path,
    parent: int,
    source_fd: int,
    *,
    retry_key: str,
    acquired_at: str,
    source_scope_id: str | None,
    importer_id: str | None,
) -> Acquisition:
    identity = sha256(retry_key.encode("utf-8")).hexdigest()
    intent = encode_json(
        {
            "retry_key": retry_key,
            "source_scope_id": source_scope_id,
            "importer_id": importer_id,
            "acquired_at": acquired_at,
            "original_filename": source.name,
        },
        integer_strings=False,
    )
    intents = _directory(root_fd, "intents", create=True)
    try:
        try:
            _immutable(intents, f"{identity}.json", intent)
        except StorageError as error:
            if error.code is StorageCode.CORRUPT:
                raise StorageError(StorageCode.CONFLICT) from error
            raise
    finally:
        os.close(intents)
    staging = _directory(root_fd, "staging", create=True)
    try:
        os.fchmod(staging, 0o700)
        temporary = f".object-{token_hex(16)}"
        target = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=staging,
        )
        try:
            result = _copy_source(
                source,
                parent,
                source_fd,
                target,
                retry_key=retry_key,
                acquired_at=acquired_at,
                source_scope_id=source_scope_id,
                importer_id=importer_id,
            )
            _commit_acquisition(root_fd, staging, temporary, identity, result)
            return result
        finally:
            os.close(target)
            os.unlink(temporary, dir_fd=staging)
    finally:
        os.close(staging)


def _copy_source(  # noqa: PLR0913 - source and target descriptors stay explicit
    source: Path,
    parent: int,
    source_fd: int,
    target: int,
    *,
    retry_key: str,
    acquired_at: str,
    source_scope_id: str | None,
    importer_id: str | None,
) -> Acquisition:
    before = os.fstat(source_fd)
    digest = sha256()
    size = 0
    for chunk in _chunks(source_fd, before.st_size):
        digest.update(chunk)
        size += len(chunk)
        written = 0
        while written < len(chunk):
            written += os.write(target, chunk[written:])
    os.fsync(target)
    after = os.fstat(source_fd)
    try:
        path_after = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError as error:
        raise StorageError(StorageCode.CHANGED) from error
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    path_identity = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if (
        identity_before != identity_after
        or identity_after != path_identity
        or size != after.st_size
    ):
        raise StorageError(StorageCode.CHANGED)
    data = {
        "retry_key": retry_key,
        "source_scope_id": source_scope_id,
        "importer_id": importer_id,
        "acquired_at": acquired_at,
        "original_filename": source.name,
        "object_digest": digest.hexdigest(),
        "size": size,
    }
    return Acquisition(
        retry_key,
        source_scope_id,
        importer_id,
        acquired_at,
        source.name,
        digest.hexdigest(),
        size,
        byte_digest(encode_json(data, integer_strings=False)),
    )


def _commit_acquisition(  # noqa: PLR0912 - retain explicit durability boundaries
    root_fd: int, staging: int, temporary: str, identity: str, result: Acquisition
) -> None:
    receipts = _directory(root_fd, "receipts", create=True)
    try:
        name = f"{identity}.json"
        try:
            existing = _read(receipts, name)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing != _record(result):
            raise StorageError(StorageCode.CONFLICT)
        if existing is not None:
            _verify_object(root_fd, result.object_digest, result.size)
        objects = _directory(root_fd, "objects", create=True)
        try:
            if existing is None:
                with suppress(FileExistsError):
                    os.link(
                        temporary,
                        result.object_digest,
                        src_dir_fd=staging,
                        dst_dir_fd=objects,
                    )
            _verify_object(root_fd, result.object_digest, result.size)
            _sync_installed(objects, result.object_digest)
        finally:
            os.close(objects)
        accessions = _directory(root_fd, "accessions", create=True)
        try:
            accession_name = f"{result.accession_id}.json"
            if existing is None:
                _immutable(accessions, accession_name, _record(result))
            else:
                try:
                    indexed = _read(accessions, accession_name)
                except FileNotFoundError as error:
                    raise StorageError(StorageCode.MISSING) from error
                if indexed != _record(result):
                    raise StorageError(StorageCode.CORRUPT)
                _sync_installed(accessions, accession_name)
        finally:
            os.close(accessions)
        if existing is None:
            _immutable(receipts, name, _record(result))
        else:
            _sync_installed(receipts, name)
    finally:
        os.close(receipts)


def read_acquisition(root: Path, accession_id: str) -> Acquisition:
    """Read an exact indexed accession and its committed retry receipt."""
    try:
        require_digest(accession_id)
    except ArtifactIntegrityError as error:
        raise StorageError(StorageCode.PATH) from error
    root_fd = _open_directory(root)
    try:
        try:
            accessions = _directory(root_fd, "accessions")
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING) from error
        try:
            try:
                receipts = _directory(root_fd, "receipts")
            except FileNotFoundError as error:
                raise StorageError(StorageCode.MISSING) from error
            try:
                index_payload = _read(accessions, f"{accession_id}.json")
                try:
                    document = decode_document(index_payload, "accession.json")
                except ArtifactIntegrityError as error:
                    raise StorageError(StorageCode.CORRUPT) from error
                item = _validated_acquisition(document)
                if item.accession_id != accession_id:
                    raise StorageError(StorageCode.CORRUPT)
                if index_payload != _record(item):
                    raise StorageError(StorageCode.CORRUPT)
                retry_id = sha256(item.retry_key.encode("utf-8")).hexdigest()
                if _read(receipts, f"{retry_id}.json") != _record(item):
                    raise StorageError(StorageCode.CORRUPT)
            except FileNotFoundError as error:
                raise StorageError(StorageCode.MISSING) from error
            else:
                return item
            finally:
                os.close(receipts)
        finally:
            os.close(accessions)
    finally:
        os.close(root_fd)


def read_object(root: Path, acquisition: Acquisition) -> bytes:
    """Read only the expected committed object's opened, verified bytes."""
    if read_acquisition(root, acquisition.accession_id) != acquisition:
        raise StorageError(StorageCode.CORRUPT)
    root_fd = _open_directory(root)
    try:
        try:
            objects = _directory(root_fd, "objects")
            try:
                payload = _read(
                    objects, acquisition.object_digest, expected_size=acquisition.size
                )
            finally:
                os.close(objects)
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING) from error
        if (
            len(payload) != acquisition.size
            or byte_digest(payload) != acquisition.object_digest
        ):
            raise StorageError(StorageCode.CORRUPT)
        return payload
    finally:
        os.close(root_fd)


def put_retained(root: Path, payload: bytes, *, lock: WriterLock) -> str:
    """Retain authored snapshot bytes without inventing an acquisition."""
    root_fd = _require_lock(root, lock)
    if not isinstance(payload, bytes):
        raise StorageError(StorageCode.PATH)
    digest = byte_digest(payload)
    inputs = _directory(root_fd, "inputs", create=True)
    try:
        _immutable(inputs, digest, payload)
    finally:
        os.close(inputs)
    return digest


def read_retained(root: Path, digest: str) -> bytes:
    """Read a pinned authored input, verifying actual opened bytes."""
    try:
        require_digest(digest)
    except ArtifactIntegrityError as error:
        raise StorageError(StorageCode.PATH) from error
    root_fd = _open_directory(root)
    try:
        try:
            inputs = _directory(root_fd, "inputs")
            try:
                payload = _read(inputs, digest)
            finally:
                os.close(inputs)
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING) from error
        if byte_digest(payload) != digest:
            raise StorageError(StorageCode.CORRUPT)
        return payload
    finally:
        os.close(root_fd)


def inventory(root: Path, expected: tuple[Acquisition, ...]) -> None:
    """Verify the caller's expected receipt and object set, never discover it."""
    for item in expected:
        if read_acquisition(root, item.accession_id) != item:
            raise StorageError(StorageCode.CORRUPT)
        root_fd = _open_directory(root)
        try:
            _verify_object(root_fd, item.object_digest, item.size)
        finally:
            os.close(root_fd)


def _relative(path: str) -> tuple[str, ...]:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or ":" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise StorageError(StorageCode.PATH)
    return tuple(path.split("/"))


def _tree(fd: int, prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for name in os.listdir(fd):
        _relative(name)
        entry = f"{prefix}{name}"
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _directory(fd, name)
            try:
                result.update(_tree(child, f"{entry}/"))
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            result[entry] = _fingerprint(fd, name)[0]
        else:
            raise StorageError(StorageCode.PATH)
    return result


def _verify_generation(fd: int, digest: str) -> None:
    require_digest(digest)
    actual = _tree(fd)
    try:
        descriptor_digest = actual["descriptor.json"]
    except KeyError as error:
        raise StorageError(StorageCode.MISSING) from error
    if descriptor_digest != digest:
        raise StorageError(StorageCode.CORRUPT, "descriptor.json digest")
    descriptor_bytes = _read(fd, "descriptor.json")
    if byte_digest(descriptor_bytes) != digest:
        raise StorageError(StorageCode.CHANGED)
    from jbt.artifacts.reader import (  # noqa: PLC0415 - keep acquisition imports light
        decode_typed_document,
    )

    try:
        descriptor = decode_typed_document(
            descriptor_bytes, descriptor_schema(), integer_strings=True
        )
    except ArtifactIntegrityError as error:
        raise StorageError(StorageCode.CORRUPT, "descriptor.json JSON") from error
    except ContractError as error:
        raise StorageError(StorageCode.CORRUPT, "descriptor.json schema") from error
    except ValueError as error:
        raise StorageError(
            StorageCode.CORRUPT, "descriptor.json wire encoding"
        ) from error
    manifest_path = descriptor["manifest_path"]
    payloads = descriptor["payloads"]
    if not isinstance(manifest_path, str) or not isinstance(payloads, list):
        raise StorageError(StorageCode.CORRUPT)
    manifest_digest = descriptor["manifest_digest"]
    if not isinstance(manifest_digest, str):
        raise StorageError(StorageCode.CORRUPT)
    expected = {"descriptor.json": digest, manifest_path: manifest_digest}
    _check_payloads(actual, expected, payloads, manifest_path)


def _check_payloads(
    actual: dict[str, str],
    expected: dict[str, str],
    payloads: list[object],
    manifest_path: str,
) -> None:
    for row in payloads:
        if not isinstance(row, dict):
            raise StorageError(StorageCode.CORRUPT)
        name, checksum = row.get("path"), row.get("byte_digest")
        if not isinstance(name, str) or not isinstance(checksum, str):
            raise StorageError(StorageCode.CORRUPT)
        expected[name] = checksum
    if len(expected) != len(payloads) + 2 or set(expected) != set(actual):
        raise StorageError(StorageCode.INVENTORY, "generation file set")
    for name, expected_digest in expected.items():
        _relative(name)
        if actual[name] != expected_digest:
            location = "manifest digest" if name == manifest_path else "payload digest"
            raise StorageError(StorageCode.CORRUPT, location)


def _sync_tree(fd: int) -> None:
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _directory(fd, name)
            try:
                os.fchmod(child, 0o700)
                _sync_tree(child)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
            try:
                os.fchmod(file_fd, 0o600)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        else:
            raise StorageError(StorageCode.PATH)
    os.fsync(fd)


def _install_generation(root_fd: int, stage_name: str, digest: str) -> None:
    staging = _directory(root_fd, "staging")
    try:
        os.fchmod(staging, 0o700)
        generations = _directory(root_fd, "generations", create=True)
        try:
            try:
                stage = _directory(staging, stage_name)
            except FileNotFoundError:
                stage = None
            if stage is not None:
                try:
                    _verify_generation(stage, digest)
                    os.fchmod(stage, 0o700)
                    _sync_tree(stage)
                    _verify_generation(stage, digest)
                finally:
                    os.close(stage)
            try:
                existing = _directory(generations, digest)
            except FileNotFoundError as error:
                if stage is None:
                    raise StorageError(StorageCode.MISSING) from error
                os.rename(
                    stage_name,
                    digest,
                    src_dir_fd=staging,
                    dst_dir_fd=generations,
                )
                existing = _directory(generations, digest)
            try:
                _verify_generation(existing, digest)
                _sync_tree(existing)
            finally:
                os.close(existing)
            os.fsync(generations)
            os.fsync(staging)
        finally:
            os.close(generations)
    finally:
        os.close(staging)


def publish(
    root: Path, assembled: Path, descriptor_digest: str, *, lock: WriterLock
) -> Current:
    """Install a complete staged generation, then durably switch current.

    Retrying after a stage rename may reuse its already-installed generation.
    """
    root_fd = _require_lock(root, lock)
    try:
        require_digest(descriptor_digest)
    except ArtifactIntegrityError as error:
        raise StorageError(StorageCode.CORRUPT) from error
    if assembled.parent != root / "staging" or not assembled.name.startswith(
        "generation-"
    ):
        raise StorageError(StorageCode.PATH)
    _install_generation(root_fd, assembled.name, descriptor_digest)
    pointer = encode_json(
        {"generation": descriptor_digest, "descriptor_digest": descriptor_digest},
        integer_strings=False,
    )
    temporary = f".current-{token_hex(16)}"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(pointer)
                stream.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, "current.json", src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=root_fd)
        raise
    try:
        os.fsync(root_fd)
    except OSError as error:
        raise StorageError(StorageCode.UNCERTAIN) from error
    return Current(root / "generations" / descriptor_digest, descriptor_digest)


def read_current(root: Path) -> Current:
    """Resolve current once and verify the pinned generation inventory."""
    root_fd = _open_directory(root)
    try:
        try:
            pointer = _json(root_fd, "current.json")
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING) from error
        if (
            set(pointer) != {"generation", "descriptor_digest"}
            or pointer["generation"] != pointer["descriptor_digest"]
        ):
            raise StorageError(StorageCode.CORRUPT)
        digest = pointer["descriptor_digest"]
        if not isinstance(digest, str):
            raise StorageError(StorageCode.CORRUPT)
        try:
            require_digest(digest)
        except ArtifactIntegrityError as error:
            raise StorageError(StorageCode.CORRUPT) from error
        try:
            generations = _directory(root_fd, "generations")
        except FileNotFoundError as error:
            raise StorageError(StorageCode.MISSING) from error
        try:
            try:
                generation = _directory(generations, digest)
            except FileNotFoundError as error:
                raise StorageError(StorageCode.MISSING) from error
            try:
                _verify_generation(generation, digest)
            finally:
                os.close(generation)
        finally:
            os.close(generations)
        return Current(root / "generations" / digest, digest)
    finally:
        os.close(root_fd)
