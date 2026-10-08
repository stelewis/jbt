"""Local, bounded reads of manifest-pinned artifact files."""

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class IntegrityCode(StrEnum):
    """Failures that prevent interpreting an artifact."""

    DIGEST = "digest"
    PATH = "path"
    SYMLINK = "symlink"
    MISSING = "missing"
    SIZE = "size"
    INVENTORY = "inventory"
    JSON = "json"
    CHANGED = "changed"


@dataclass(frozen=True)
class ArtifactIntegrityError(ValueError):
    """Safe location and named integrity failure, without payload contents."""

    code: IntegrityCode
    filename: str

    def __str__(self) -> str:
        """Return a diagnostic without exposing file contents."""
        return f"artifact {self.code.value}: {self.filename}"


def byte_digest(payload: bytes) -> str:
    """Return a SHA-256 byte identity, not a semantic identity."""
    return hashlib.sha256(payload).hexdigest()


def require_digest(value: str) -> None:
    """Accept only the published lowercase SHA-256 encoding."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ArtifactIntegrityError(IntegrityCode.DIGEST, "<digest>")


def require_filename(filename: str) -> None:
    """Allow only a single portable publication filename, never a path."""
    if (
        not isinstance(filename, str)
        or re.fullmatch(
            r"[a-z][a-z0-9_-]*(?:\.(json|parquet|beancount|ledger|txt))?",
            filename,
        )
        is None
    ):
        raise ArtifactIntegrityError(IntegrityCode.PATH, "<invalid-name>")


def decode_document(payload: bytes, filename: str) -> dict[str, object]:
    """Reject duplicate keys and nonstandard JSON before schema validation."""
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_constant,
            parse_int=_read_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ArtifactIntegrityError(IntegrityCode.JSON, filename) from error
    if not isinstance(decoded, dict):
        raise ArtifactIntegrityError(IntegrityCode.JSON, filename)
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactIntegrityError(IntegrityCode.JSON, "<duplicate-key>")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ArtifactIntegrityError(IntegrityCode.JSON, "<non-integer-number>")


def _read_integer(value: str) -> int:
    if len(value.removeprefix("-")) > len(str(2**63)):
        raise ArtifactIntegrityError(IntegrityCode.JSON, "<integer-range>")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise ArtifactIntegrityError(IntegrityCode.JSON, "<integer-range>")
    return result


def _open_root(root: Path) -> int:
    if root.is_symlink():
        raise ArtifactIntegrityError(IntegrityCode.PATH, "<snapshot>")
    try:
        return os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ArtifactIntegrityError(IntegrityCode.PATH, "<snapshot>") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ArtifactIntegrityError(IntegrityCode.PATH, "<snapshot>") from error
        raise


def _open_payload(directory: int, filename: str) -> int:
    try:
        return os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except FileNotFoundError as error:
        raise ArtifactIntegrityError(IntegrityCode.MISSING, filename) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ArtifactIntegrityError(IntegrityCode.SYMLINK, filename) from error
        raise


def read_payload(
    root: Path,
    filename: str,
    expected_digest: str,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one stable ordinary opened file below a pinned snapshot root."""
    require_digest(expected_digest)
    require_filename(filename)
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        msg = "maximum_bytes must be a positive integer"
        raise ValueError(msg)
    directory = _open_root(root)
    try:
        descriptor = _open_payload(directory, filename)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactIntegrityError(IntegrityCode.PATH, filename)
            if before.st_size > maximum_bytes:
                raise ArtifactIntegrityError(IntegrityCode.SIZE, filename)
            content = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
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
            ):
                raise ArtifactIntegrityError(IntegrityCode.CHANGED, filename)
    finally:
        os.close(directory)
    if len(content) > maximum_bytes:
        raise ArtifactIntegrityError(IntegrityCode.SIZE, filename)
    if byte_digest(content) != expected_digest:
        raise ArtifactIntegrityError(IntegrityCode.DIGEST, filename)
    return content


def require_inventory(root: Path, filenames: frozenset[str]) -> None:
    """Reject extra entries instead of ignoring unpinned schema-bearing files."""
    if root.is_symlink() or not root.is_dir():
        raise ArtifactIntegrityError(IntegrityCode.PATH, "<snapshot>")
    actual = frozenset(path.name for path in root.iterdir())
    if actual != filenames:
        raise ArtifactIntegrityError(IntegrityCode.INVENTORY, "<snapshot>")
