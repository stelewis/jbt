"""Retain complete build selections before executing any financial stage."""

# Exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

from __future__ import annotations

import errno
import os
import stat
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import require_digest
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.importers.ofx import MAX_BYTES
from jbt.runtime.execution import ObservedExecution, decode_execution
from jbt.runtime.project import Project, document, objects, read_project, text
from jbt.storage.local import (
    Acquisition,
    Current,
    inventory,
    put_retained,
    read_acquisition,
    read_retained,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jbt.storage.local import WriterLock

MAXIMUM_PROJECT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """The complete expected recovery selection, not a filesystem scan."""

    digest: str
    project_digest: str
    execution_digest: str
    acquisitions: tuple[Acquisition, ...]
    baseline_path: str | None
    baseline_digest: str | None


def canonical_root(root: Path) -> Path:
    """Canonicalize selected root ancestors, not retained descendants."""
    selected = root.absolute()
    require(not selected.is_symlink(), "symlink_root", "root")
    try:
        return selected.resolve()
    except (OSError, RuntimeError) as error:
        raise ContractError("symlink_root", "root") from error


def confined(root: Path, relative: str) -> Path:
    """Resolve only ordinary descendants without symlink traversal."""
    parts = relative.split("/")
    require(
        bool(relative)
        and not relative.startswith("/")
        and "\\" not in relative
        and ":" not in relative
        and all(part not in {"", ".", ".."} for part in parts),
        "relative_path_required",
        "inputs",
    )
    path = root.joinpath(*parts)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise ContractError("symlink_path", "inputs") from error
    require(resolved == path.absolute(), "symlink_path", "inputs")
    return path


def read_project_file(root: Path, relative: str) -> bytes:
    """Capture an ordinary quiescent file once, with a bounded read."""
    confined(root, relative)
    parts = relative.split("/")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError as error:
        os.close(directory)
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ContractError("symlink_path", "project") from error
        raise
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode) and before.st_size <= MAXIMUM_PROJECT_BYTES,
            "project_file_type_or_size",
            "project",
        )
        chunks = []
        remaining = MAXIMUM_PROJECT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        after = os.fstat(descriptor)
        live = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        require(
            len(result) == before.st_size
            and (
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
            )
            == (
                live.st_dev,
                live.st_ino,
                live.st_size,
                live.st_mtime_ns,
                live.st_ctime_ns,
            ),
            "project_changed_during_capture",
            "project",
        )
        return result
    finally:
        os.close(descriptor)
        os.close(directory)


def require_source_size(acquisition: Acquisition) -> None:
    """Enforce the supported importer budget before reading source bytes."""
    require(acquisition.size <= MAX_BYTES, "source_size_limit", "inputs")


def _bound_acquisitions(root: Path, project: Project) -> tuple[Acquisition, ...]:
    result = []
    for source in project.sources:
        acquisition = read_acquisition(root, source.accession_id)
        require(
            acquisition.source_scope_id == source.source_scope_id
            and acquisition.importer_id == "ofx",
            "acquisition_binding_mismatch",
            "inputs",
        )
        require_source_size(acquisition)
        result.append(acquisition)
    return tuple(result)


def capture_inputs(
    root: Path,
    *,
    project_path: str,
    execution: ObservedExecution,
    baseline: Current | None,
    lock: WriterLock,
) -> InputSnapshot:
    """Retain the attempted selection independently of publication success."""
    payload = read_project_file(root, project_path)
    project = read_project(payload)
    acquisitions = _bound_acquisitions(root, project)
    inventory(root, acquisitions)
    project_digest = put_retained(root, payload, lock=lock)
    execution_digest = put_retained(root, execution.payload, lock=lock)
    baseline_path = baseline.path.relative_to(root).as_posix() if baseline else None
    if baseline_path is not None:
        confined(root, baseline_path)
    record = {
        "schema_version": 1,
        "project_digest": project_digest,
        "execution_digest": execution_digest,
        "acquisitions": [asdict(item) for item in acquisitions],
        "baseline_path": baseline_path,
        "baseline_digest": baseline.descriptor_digest if baseline else None,
    }
    digest = put_retained(root, encode_json(record, integer_strings=False), lock=lock)
    return InputSnapshot(
        digest,
        project_digest,
        execution_digest,
        acquisitions,
        baseline_path,
        baseline.descriptor_digest if baseline else None,
    )


def load_inputs(root: Path, digest: str) -> tuple[InputSnapshot, Project]:
    """Validate every expected receipt, evidence object and execution record."""
    from jbt.artifacts.integrity import decode_document  # noqa: PLC0415

    record = document(decode_document(read_retained(root, digest), "inputs"))
    require(
        set(record)
        == {
            "schema_version",
            "project_digest",
            "execution_digest",
            "acquisitions",
            "baseline_path",
            "baseline_digest",
        }
        and type(record["schema_version"]) is int
        and record["schema_version"] == 1,
        "input_snapshot_schema",
        "inputs",
    )
    project_digest = text(record["project_digest"], "project_digest")
    require_digest(project_digest)
    execution_digest = text(record["execution_digest"], "execution_digest")
    require_digest(execution_digest)
    decode_execution(read_retained(root, execution_digest))
    project = read_project(read_retained(root, project_digest))
    acquisitions = _bound_acquisitions(root, project)
    expected = objects(record["acquisitions"], "acquisitions")
    require(
        tuple(asdict(item) for item in acquisitions) == expected,
        "acquisition_inventory_changed",
        "inputs",
    )
    inventory(root, acquisitions)
    baseline_path = (
        text(record["baseline_path"], "baseline_path")
        if record["baseline_path"] is not None
        else None
    )
    baseline_digest = (
        text(record["baseline_digest"], "baseline_digest")
        if record["baseline_digest"] is not None
        else None
    )
    require(
        (baseline_path is None) == (baseline_digest is None), "baseline_pair", "inputs"
    )
    if baseline_path is not None:
        baseline_path = text(baseline_path, "baseline_path")
        baseline_digest = text(baseline_digest, "baseline_digest")
        confined(root, baseline_path)
        require_digest(baseline_digest)
    return InputSnapshot(
        digest,
        project_digest,
        execution_digest,
        acquisitions,
        baseline_path,
        baseline_digest,
    ), project
