"""Retain complete build selections before executing any financial stage."""

# Exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

from __future__ import annotations

import errno
import os
import stat
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from jbt.artifacts.attempt import encode_review_report
from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    byte_digest,
    decode_document,
    require_digest,
)
from jbt.artifacts.journal import decode_entry, decode_head
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.domain.errors import DomainError
from jbt.domain.journal import (
    JournalHead,
    JournalSelection,
    encode_entry,
    encode_head,
    select_journal,
)
from jbt.runtime.execution import ObservedExecution, decode_execution, select_importers
from jbt.runtime.project import (
    Project,
    document,
    integer,
    objects,
    read_project,
    strings,
    text,
)
from jbt.runtime.temporal import timezone_provenance
from jbt.storage.attempts import read_report, retain_attempt, retain_report, selections
from jbt.storage.journal import initialize_journal, read_journal
from jbt.storage.local import (
    Acquisition,
    Current,
    StorageError,
    inventory,
    put_retained,
    read_acquisition,
    read_retained,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jbt.storage.local import WriterLock

MAXIMUM_PROJECT_BYTES = 16 * 1024 * 1024


def _timezone_rules(value: object) -> bytes:
    rules = document(value)
    require(
        set(rules)
        == {
            "distribution",
            "version",
            "iana_version",
            "rules_sha256",
            "gap_policy",
            "overlap_policy",
        }
        and rules["distribution"] == "tzdata"
        and rules["gap_policy"] == "reject"
        and rules["overlap_policy"] == "require_explicit_offset",
        "timezone_rules_schema",
        "inputs",
    )
    text(rules["version"], "timezone_rules.version")
    text(rules["iana_version"], "timezone_rules.iana_version")
    require_digest(text(rules["rules_sha256"], "timezone_rules.rules_sha256"))
    return encode_json(rules, integer_strings=False)


class InputCaptureError(ContractError):
    """A located capture failure with a separately sealed safe attempt report."""

    def __init__(self, code: str, location: str, attempt: str) -> None:
        """Keep input failure location and exact attempt reference."""
        super().__init__(code, location)
        self.attempt = attempt

    def __str__(self) -> str:
        """Expose the retained failure without describing financial values."""
        return f"{super().__str__()}; attempt={self.attempt}"


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """The complete expected recovery selection, not a filesystem scan."""

    digest: str
    project_digest: str
    execution_digest: str
    timezone_rules: bytes
    acquisitions: tuple[Acquisition, ...]
    documents: tuple[Acquisition, ...]
    baseline_path: str | None
    baseline_digest: str | None
    journal: JournalSelection
    journal_headers: tuple[str, ...]
    journal_bases: tuple[str, ...]
    journal_reports: tuple[tuple[str, str], ...]
    journal_uncommitted: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CapturedInputs:
    """An invocation witness and its independently sealed financial inputs."""

    attempt_digest: str
    snapshot: InputSnapshot


def retained_journal_heads(root: Path, *, lock: WriterLock) -> tuple[JournalHead, ...]:
    """Return the complete applicable financial history, not filesystem guesses."""
    witnesses = selections(root, lock=lock)
    return tuple(
        decode_head(
            encode_json(
                document(document(decode_document(payload, "attempt"))["journal"])[
                    "head"
                ],
                integer_strings=False,
            )
        )
        for payload in witnesses
        if document(decode_document(payload, "attempt"))["journal"] is not None
    )


def capture_journal(root: Path, *, lock: WriterLock) -> JournalSelection:
    """Check the live commit against every immutable earlier attempt witness."""
    from jbt.storage.local import _directory, _require_lock  # noqa: PLC0415

    retained = retained_journal_heads(root, lock=lock)
    try:
        directory = _directory(_require_lock(root, lock), "journal")
    except FileNotFoundError:
        require(not retained, "journal_lost", "journal")
        initialize_journal(root, lock=lock)
    else:
        os.close(directory)
    return read_journal(root, head=None, retained_heads=retained)


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


def require_source_size(acquisition: Acquisition, maximum_bytes: int) -> None:
    """Enforce the supported importer budget before reading source bytes."""
    require(acquisition.size <= maximum_bytes, "source_size_limit", "inputs")


def source_importers(acquisitions: tuple[Acquisition, ...]) -> tuple[str, ...]:
    """Archive-only evidence is retained, but cannot select an interpreter."""
    result = []
    for item in acquisitions:
        require(item.importer_id is not None, "archive_only_source", item.accession_id)
        result.append(text(item.importer_id, "importer_id"))
    return tuple(sorted(set(result)))


def _bound_acquisitions(root: Path, project: Project) -> tuple[Acquisition, ...]:
    result = []
    scopes = {}
    for source in project.sources:
        acquisition = read_acquisition(root, source.accession_id)
        require(
            acquisition.importer_id is not None,
            "archive_only_source",
            acquisition.accession_id,
        )
        require(
            acquisition.source_scope_id not in scopes
            or scopes[acquisition.source_scope_id] == acquisition.importer_id,
            "source_scope_importer_conflict",
            "inputs",
        )
        scopes[acquisition.source_scope_id] = acquisition.importer_id
        result.append(acquisition)
    return tuple(result)


def _document_acquisitions(root: Path, project: Project) -> tuple[Acquisition, ...]:
    result = tuple(
        read_acquisition(root, selection.accession_id)
        for selection in project.documents
    )
    for item in result:
        require(
            item.importer_id is None,
            "document_requires_archive_only",
            item.accession_id,
        )
    return result


def _capture_inputs(
    root: Path,
    *,
    payload: bytes,
    execution: ObservedExecution,
    baseline: Current | None,
    lock: WriterLock,
) -> InputSnapshot:
    """Retain the attempted selection independently of publication success."""
    project = read_project(payload)
    acquisitions = _bound_acquisitions(root, project)
    documents = _document_acquisitions(root, project)
    execution, importers = select_importers(execution, source_importers(acquisitions))
    for item in acquisitions:
        require_source_size(
            item, importers[text(item.importer_id, "importer")].maximum_bytes
        )
    inventory(root, (*acquisitions, *documents))
    journal = capture_journal(root, lock=lock)
    headers = tuple(
        put_retained(root, encode_entry(entry), lock=lock)
        for entry in journal.committed_entries
    )
    from jbt.artifacts.decisions import decode_decision_body  # noqa: PLC0415

    bases = tuple(
        sorted(
            {
                decode_decision_body(
                    read_retained(root, entry.payload_digest)
                ).review.basis_digest
                for entry in journal.committed_entries
                if entry.payload_digest is not None
            }
        )
    )
    reports = tuple(
        (attempt, byte_digest(read_report(root, attempt)))
        for attempt in sorted(
            {entry.reviewed_attempt_digest for entry in journal.committed_entries}
        )
    )
    from jbt.runtime.decisions import authenticate_decisions  # noqa: PLC0415

    authenticate_decisions(root, journal=journal, bases=bases, reports=reports)
    project_digest = put_retained(root, payload, lock=lock)
    execution_digest = put_retained(root, execution.payload, lock=lock)
    timezone_rules = _timezone_rules(timezone_provenance())
    baseline_path = baseline.path.relative_to(root).as_posix() if baseline else None
    if baseline_path is not None:
        confined(root, baseline_path)
    record = {
        "schema_version": 1,
        "project_digest": project_digest,
        "execution_digest": execution_digest,
        "timezone_rules": decode_document(timezone_rules, "timezone_rules"),
        "acquisitions": [asdict(item) for item in acquisitions],
        "documents": [asdict(item) for item in documents],
        "baseline_path": baseline_path,
        "baseline_digest": baseline.descriptor_digest if baseline else None,
        "journal": {
            "head": document(decode_document(encode_head(journal.head), "head")),
            "headers": list(headers),
            "bases": list(bases),
            "reports": [
                {"attempt_digest": attempt, "report_digest": seal}
                for attempt, seal in reports
            ],
            "uncommitted": [entry.sequence for entry in journal.unselected_entries],
        },
    }
    digest = put_retained(root, encode_json(record, integer_strings=False), lock=lock)
    return InputSnapshot(
        digest,
        project_digest,
        execution_digest,
        timezone_rules,
        acquisitions,
        documents,
        baseline_path,
        baseline.descriptor_digest if baseline else None,
        select_journal(journal.committed_entries, head=journal.head, retained_heads=()),
        headers,
        bases,
        reports,
        tuple(entry.sequence for entry in journal.unselected_entries),
    )


def capture_inputs(
    root: Path,
    *,
    project_path: str,
    execution: ObservedExecution,
    baseline: Current | None,
    lock: WriterLock,
) -> CapturedInputs:
    """Capture once, retaining malformed or integrity failures as located errors."""
    payload = None
    try:
        payload = read_project_file(root, project_path)
        snapshot = _capture_inputs(
            root,
            payload=payload,
            execution=execution,
            baseline=baseline,
            lock=lock,
        )
        return CapturedInputs(
            retain_attempt(root, snapshot.digest, lock=lock), snapshot
        )
    except (ContractError, DomainError, StorageError, ArtifactIntegrityError) as error:
        if isinstance(error, DomainError):
            code, location = error.constraint, error.location
        elif isinstance(error, ArtifactIntegrityError):
            code, location = error.code.value, error.filename
        elif isinstance(error, StorageError):
            code, location = error.code.value, error.location or "inputs"
        else:
            code, location = error.code, error.location
        failure = {"code": code, "location": location}
        selection = {
            "schema_version": 1,
            "project_path": project_path,
            "project_digest": put_retained(root, payload, lock=lock)
            if payload is not None
            else None,
            "execution_digest": put_retained(root, execution.payload, lock=lock),
            "journal": None,
            "failure": failure,
        }
        input_digest = put_retained(
            root, encode_json(selection, integer_strings=False), lock=lock
        )
        attempt = retain_attempt(root, input_digest, lock=lock)
        retained = {
            "schema_version": 1,
            "attempt_digest": attempt,
            "selection": {
                "input_digest": input_digest,
                "journal_head": None,
                "journal_headers": [],
                "journal_bases": [],
                "uncommitted_sequences": [],
            },
            "model": None,
            "checks": [],
            "failure": failure,
        }
        retain_report(root, attempt, encode_review_report(retained), lock=lock)
        raise InputCaptureError(code, location, attempt) from error


def load_inputs(root: Path, digest: str) -> tuple[InputSnapshot, Project]:
    """Load exact receipts and declarations; execution validates bounded blobs."""
    from jbt.artifacts.integrity import decode_document  # noqa: PLC0415

    record = document(decode_document(read_retained(root, digest), "inputs"))
    require(
        set(record)
        == {
            "schema_version",
            "project_digest",
            "execution_digest",
            "timezone_rules",
            "acquisitions",
            "documents",
            "baseline_path",
            "baseline_digest",
            "journal",
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
    execution = decode_execution(read_retained(root, execution_digest))
    timezone_rules = _timezone_rules(record["timezone_rules"])
    project = read_project(read_retained(root, project_digest))
    acquisitions = _bound_acquisitions(root, project)
    documents = _document_acquisitions(root, project)
    expected = objects(record["acquisitions"], "acquisitions")
    require(
        tuple(asdict(item) for item in acquisitions) == expected,
        "acquisition_inventory_changed",
        "inputs",
    )
    require(
        tuple(asdict(item) for item in documents)
        == objects(record["documents"], "documents"),
        "document_inventory_changed",
        "inputs",
    )
    records = {item["importer_id"]: item for item in execution.document()["importers"]}
    require(
        set(records) == set(source_importers(acquisitions)),
        "input_importer_inventory",
        "inputs",
    )
    for item in acquisitions:
        require_source_size(
            item, records[text(item.importer_id, "importer")]["maximum_bytes"]
        )
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
    selected_journal = document(record["journal"])
    require(
        set(selected_journal) == {"head", "headers", "bases", "reports", "uncommitted"},
        "input_journal_schema",
        "inputs",
    )
    headers = strings(selected_journal["headers"], "journal.headers")
    bases = strings(selected_journal["bases"], "journal.bases")
    reports = []
    for reference in objects(selected_journal["reports"], "journal.reports"):
        require(
            set(reference) == {"attempt_digest", "report_digest"},
            "input_journal_report_schema",
            "inputs",
        )
        reports.append(
            (
                text(reference["attempt_digest"], "attempt_digest"),
                text(reference["report_digest"], "report_digest"),
            )
        )
    head = decode_head(encode_json(selected_journal["head"], integer_strings=False))
    journal = select_journal(
        tuple(decode_entry(read_retained(root, header)) for header in headers),
        head=head,
        retained_heads=(),
    )
    uncommitted = selected_journal["uncommitted"]
    if not isinstance(uncommitted, list):
        code = "input_journal_suffix"
        raise ContractError(code, "inputs")
    uncommitted = tuple(integer(item, "suffix.sequence") for item in uncommitted)
    require(
        all(item > head.sequence for item in uncommitted),
        "input_journal_suffix",
        "inputs",
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
        timezone_rules,
        acquisitions,
        documents,
        baseline_path,
        baseline_digest,
        journal,
        headers,
        bases,
        tuple(reports),
        uncommitted,
    ), project
