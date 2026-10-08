"""Sequential stage execution with disposable, integrity-checked reuse."""

import errno
import logging
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jbt.artifacts.execution import (
    ExecutionFacts,
    InputEdge,
    StageCacheError,
    execution_fingerprint,
    read_stage_cache,
    stage_envelope,
)
from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    IntegrityCode,
    byte_digest,
)
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)
MAXIMUM_ARTIFACT_BYTES = 64 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _real_directory(path: Path, stage: str) -> bool:
    require(
        path.resolve() == path.absolute() and not path.is_symlink(),
        "unsafe_cache_path",
        stage,
    )
    require(not path.exists() or path.is_dir(), "unsafe_cache_path", stage)
    return path.is_dir()


def _read_envelope(directory: Path, stage: str) -> bytes:
    root_fd = os.open(directory, _DIRECTORY_FLAGS)
    try:
        try:
            descriptor = os.open("envelope.json", _FILE_FLAGS, dir_fd=root_fd)
        except FileNotFoundError as error:
            raise ArtifactIntegrityError(
                IntegrityCode.MISSING, "envelope.json"
            ) from error
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ArtifactIntegrityError(
                    IntegrityCode.SYMLINK, "envelope.json"
                ) from error
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            code = "unsafe_cache_path"
            raise ContractError(code, stage)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAXIMUM_ARTIFACT_BYTES:
                raise ArtifactIntegrityError(IntegrityCode.SIZE, "envelope.json")
            envelope = stream.read(MAXIMUM_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
            require(
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ),
                "unsafe_cache_path",
                stage,
            )
    finally:
        os.close(root_fd)
    if len(envelope) > MAXIMUM_ARTIFACT_BYTES:
        raise ArtifactIntegrityError(IntegrityCode.SIZE, "envelope.json")
    return envelope


def _write_file(directory_fd: int, name: str, content: bytes, stage: str) -> None:
    _ordinary_target(directory_fd, name, stage)
    temporary = f"cache-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def _ordinary_target(directory_fd: int, name: str, stage: str) -> None:
    try:
        target = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    require(stat.S_ISREG(target.st_mode), "unsafe_cache_path", stage)


def _write_cache(
    root: Path, fingerprint: str, payload: bytes, envelope: bytes, stage: str
) -> None:
    if not _real_directory(root, stage):
        root.mkdir(mode=0o700, parents=True)
    require(_real_directory(root, stage), "unsafe_cache_path", stage)
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        with suppress(FileExistsError):
            os.mkdir(fingerprint, mode=0o700, dir_fd=root_fd)
        stage_fd = os.open(fingerprint, _DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            _ordinary_target(stage_fd, "payload", stage)
            _ordinary_target(stage_fd, "envelope.json", stage)
            _write_file(stage_fd, "payload", payload, stage)
            _write_file(stage_fd, "envelope.json", envelope, stage)
        finally:
            os.close(stage_fd)
    finally:
        os.close(root_fd)


@dataclass(frozen=True, slots=True)
class Stage:
    """A fixed application-owned computation and its explicit prerequisites."""

    kind: str
    schema_id: str
    configuration_digest: str
    inputs: tuple[InputEdge, ...]


@dataclass(frozen=True, slots=True)
class StageResult:
    """Canonical payload and envelope, independent of cache hit or miss."""

    fingerprint: str
    payload: bytes
    envelope: bytes


class StageRunner:
    """A bypassed runner performs neither cache reads nor cache writes."""

    def __init__(self, cache: Path | None, facts: ExecutionFacts) -> None:
        """Construct at the application boundary under its exclusive lock."""
        self.cache = cache
        self.facts = facts
        self.executed: list[str] = []
        self.reused: list[str] = []

    def run(self, stage: Stage, compute: Callable[[], bytes]) -> StageResult:
        """Verify the full cached envelope before returning its payload."""
        fingerprint = execution_fingerprint(
            stage=stage.kind,
            schema_id=stage.schema_id,
            configuration_digest=stage.configuration_digest,
            inputs=stage.inputs,
            facts=self.facts,
        )
        cache = self.cache
        if cache is not None:
            directory = cache / fingerprint
            _real_directory(cache, stage.kind)
            if _real_directory(directory, stage.kind):
                try:
                    envelope = _read_envelope(directory, stage.kind)
                    payload = read_stage_cache(
                        directory,
                        envelope_digest=byte_digest(envelope),
                        stage=stage.kind,
                        schema_id=stage.schema_id,
                        configuration_digest=stage.configuration_digest,
                        inputs=stage.inputs,
                        facts=self.facts,
                        logical_digest=byte_digest,
                        maximum_bytes=MAXIMUM_ARTIFACT_BYTES,
                    )
                except (ArtifactIntegrityError, StageCacheError) as error:
                    if isinstance(error, ArtifactIntegrityError) and error.code in (
                        IntegrityCode.SYMLINK,
                        IntegrityCode.PATH,
                    ):
                        raise
                    LOGGER.warning(
                        "Discarding corrupt derived stage %s: %s", stage.kind, error
                    )
                else:
                    self.reused.append(stage.kind)
                    return StageResult(fingerprint, payload, envelope)
        payload = compute()
        require(len(payload) <= MAXIMUM_ARTIFACT_BYTES, "stage_size_limit", stage.kind)
        digest = byte_digest(payload)
        envelope = encode_json(
            stage_envelope(
                stage=stage.kind,
                schema_id=stage.schema_id,
                configuration_digest=stage.configuration_digest,
                inputs=stage.inputs,
                facts=self.facts,
                path="payload",
                byte_digest_value=digest,
                logical_digest=digest,
            ),
            integer_strings=False,
        )
        if cache is not None:
            _write_cache(cache, fingerprint, payload, envelope, stage.kind)
        self.executed.append(stage.kind)
        return StageResult(fingerprint, payload, envelope)
