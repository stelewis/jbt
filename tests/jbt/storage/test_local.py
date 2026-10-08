import errno
import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.execution import ExecutionFacts, Producer
from jbt.artifacts.integrity import byte_digest
from jbt.artifacts.parquet import Compression, WriterSettings
from jbt.artifacts.reader import read_snapshot
from jbt.artifacts.snapshot import BuildInput, SnapshotInput, assemble_snapshot
from jbt.contracts.catalog import catalog
from jbt.domain.canonical import encode_json
from jbt.storage.local import (
    Acquisition,
    StorageCode,
    StorageError,
    acquire,
    inventory,
    publish,
    put_retained,
    read_acquisition,
    read_current,
    read_object,
    read_retained,
    writer_lock,
)

if TYPE_CHECKING:
    from pathlib import Path


def _acquire(root: Path, source: Path, key: str = "attempt") -> Acquisition:
    with writer_lock(root) as lock:
        return acquire(
            root,
            source,
            retry_key=key,
            acquired_at="2026-01-01T00:00:00Z",
            source_scope_id="cash",
            importer_id="ofx",
            lock=lock,
        )


def _generation(root: Path, payload: bytes) -> tuple[Path, str]:
    staging = root / "staging"
    staging.mkdir(exist_ok=True)
    stage = staging / f"generation-{byte_digest(payload)}"
    stage.mkdir()
    (stage / "manifest.json").write_bytes(payload)
    descriptor = encode_json(
        {
            "schema_version": 1,
            "manifest_path": "manifest.json",
            "manifest_digest": byte_digest(payload),
            "payloads": [
                {"path": "record.parquet", "byte_digest": byte_digest(payload)}
            ],
        },
        integer_strings=True,
    )
    (stage / "record.parquet").write_bytes(payload)
    (stage / "descriptor.json").write_bytes(descriptor)
    return stage, byte_digest(descriptor)


def test_real_snapshot_assembly_publishes_canonical_wire(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    stage = root / "staging" / "generation-real"
    stage.mkdir(parents=True)
    configuration = encode_json(
        {
            "schema_version": 1,
            "as_of": "2026-01-31",
            "source_authority": [],
            "rendering": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "declaration_source_order": [],
        },
        integer_strings=False,
    )
    definitions = encode_json(
        {"schema_version": 1, "definitions": {}}, integer_strings=False
    )
    runtime = b"synthetic-runtime"
    producer = b"synthetic-producer"
    inputs = SnapshotInput(
        tables={table.name: [] for table in catalog() if table.name != "build"},
        builds=[BuildInput("E", "synthetic-producer", "2026-01-31", "a" * 64)],
        metadata={
            "artifacts": [],
            "registries": [],
            "configuration": {
                "path": "configuration.json",
                "byte_digest": byte_digest(configuration),
            },
            "derivation_definitions": {
                "path": "derivation_definitions.json",
                "byte_digest": byte_digest(definitions),
            },
            "effective_declarations": [],
            "bindings": [],
            "recovery_set": {
                "vault_objects": [],
                "accession_records": [],
                "authored_history": [],
                "dependencies": [],
                "external_assets": [],
                "runtime": [
                    {
                        "runtime_id": "synthetic-runtime",
                        "version": "1",
                        "platform": "synthetic-platform",
                        "path": "runtime.bin",
                        "byte_digest": byte_digest(runtime),
                    }
                ],
            },
            "inputs": [],
            "toolchain": [
                {
                    "component": "synthetic-producer",
                    "version": "1",
                    "content_digest": byte_digest(producer),
                }
            ],
            "derivations": [],
            "capabilities": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "checks": [],
        },
        resources={
            "configuration.json": configuration,
            "derivation_definitions.json": definitions,
        },
        execution=ExecutionFacts(
            Producer("synthetic-producer", "1", byte_digest(producer)),
            byte_digest(runtime),
        ),
        writer_version="synthetic",
    )
    result = assemble_snapshot(
        stage,
        inputs,
        WriterSettings(
            compression=Compression.ZSTD,
            row_group_size=100,
            dictionary=False,
            statistics=True,
        ),
    )
    assert b'"schema_version":"1"' in (stage / "descriptor.json").read_bytes()
    with writer_lock(root) as lock:
        current = publish(root, stage, result.descriptor_digest, lock=lock)
    assert current == read_current(root)
    assert (
        read_snapshot(current.path, current.descriptor_digest).manifest[
            "logical_content_digest"
        ]
        == result.financial_digest
    )


def test_publication_rejects_native_integer_descriptor_wire(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    stage, _ = _generation(root, b"manifest")
    descriptor = stage / "descriptor.json"
    descriptor.write_bytes(
        descriptor.read_bytes().replace(b'"schema_version":"1"', b'"schema_version":1')
    )
    with (
        writer_lock(root) as lock,
        pytest.raises(StorageError, match=r"corrupt: descriptor\.json wire encoding"),
    ):
        publish(root, stage, byte_digest(descriptor.read_bytes()), lock=lock)
    assert not (root / "current.json").exists()


def test_publication_reports_descriptor_json_corruption(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    stage, _ = _generation(root, b"manifest")
    descriptor = stage / "descriptor.json"
    descriptor.write_bytes(b"{broken")
    with (
        writer_lock(root) as lock,
        pytest.raises(StorageError, match=r"corrupt: descriptor\.json JSON"),
    ):
        publish(root, stage, byte_digest(descriptor.read_bytes()), lock=lock)


def test_acquire_retry_conflict_inventory_and_distinct_accessions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "statement.ofx"
    source.write_bytes(b"source bytes")
    first = _acquire(root, source)
    assert first == _acquire(root, source)
    assert first.object_digest == byte_digest(source.read_bytes())
    assert _acquire(root, source, "another").accession_id != first.accession_id
    inventory(root, (first,))
    assert source.read_bytes() == b"source bytes"
    assert (
        stat.S_IMODE((root / "objects" / first.object_digest).stat().st_mode) == 0o600
    )
    assert stat.S_IMODE((root / "receipts").stat().st_mode) == 0o700
    source.write_bytes(b"different")
    with pytest.raises(StorageError) as error:
        _acquire(root, source)
    assert error.value.code is StorageCode.CONFLICT
    inventory(root, (first,))
    (root / "objects" / first.object_digest).write_bytes(b"corrupted")
    with pytest.raises(StorageError) as error:
        inventory(root, (first,))
    assert error.value.code is StorageCode.CORRUPT
    source.write_bytes(b"source bytes")
    with pytest.raises(StorageError) as error:
        _acquire(root, source, "third")
    assert error.value.code is StorageCode.CORRUPT


def test_missing_expected_receipt_and_object(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "input"
    source.write_bytes(b"original")
    item = _acquire(root, source)
    (root / "objects" / item.object_digest).unlink()
    with pytest.raises(StorageError, match="missing"):
        inventory(root, (item,))
    (root / "receipts" / f"{byte_digest(item.retry_key.encode())}.json").unlink()
    with pytest.raises(StorageError, match="missing"):
        inventory(root, (item,))
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("boundary", ["intents", "objects", "accessions", "receipts"])
def test_acquisition_retry_reestablishes_each_installed_directory_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    original = os.fsync
    failures = 0

    def fail_installed_directory(fd: int) -> None:
        nonlocal failures
        directory = root / boundary
        if directory.exists() and os.fstat(fd).st_ino == directory.stat().st_ino:
            failures += 1
            raise OSError(errno.ENOSPC, "injected installed directory fsync failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_installed_directory)
    for _ in range(2):
        with pytest.raises(StorageError) as error:
            _acquire(root, source)
        assert error.value.code is StorageCode.UNCERTAIN
    assert failures == 2
    assert source.read_bytes() == b"evidence"
    monkeypatch.setattr(os, "fsync", original)
    item = _acquire(root, source)
    inventory(root, (item,))


def test_retained_retry_does_not_acknowledge_unsynchronized_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    original = os.fsync
    failures = 0

    def fail_installed_directory(fd: int) -> None:
        nonlocal failures
        inputs = root / "inputs"
        if inputs.exists() and os.fstat(fd).st_ino == inputs.stat().st_ino:
            failures += 1
            raise OSError(errno.ENOSPC, "injected retained directory fsync failure")
        original(fd)

    with writer_lock(root) as lock:
        monkeypatch.setattr(os, "fsync", fail_installed_directory)
        for _ in range(2):
            with pytest.raises(StorageError) as error:
                put_retained(root, b"retained", lock=lock)
            assert error.value.code is StorageCode.UNCERTAIN
        assert failures == 2
        monkeypatch.setattr(os, "fsync", original)
        digest = put_retained(root, b"retained", lock=lock)
    assert read_retained(root, digest) == b"retained"


def test_directory_creation_retry_synchronizes_existing_parent_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    failures = 0
    inode = tmp_path.stat().st_ino

    def fail_parent_sync(fd: int) -> None:
        nonlocal failures
        if os.fstat(fd).st_ino == inode:
            failures += 1
            raise OSError(errno.ENOSPC, "injected parent fsync failure")
        original(fd)

    with writer_lock(tmp_path) as lock:
        monkeypatch.setattr(os, "fsync", fail_parent_sync)
        for _ in range(2):
            with pytest.raises(OSError, match="parent fsync"):
                put_retained(tmp_path, b"retained", lock=lock)
        assert failures == 2
        assert (tmp_path / "inputs").is_dir()
        monkeypatch.setattr(os, "fsync", original)
        digest = put_retained(tmp_path, b"retained", lock=lock)
    assert read_retained(tmp_path, digest) == b"retained"


@pytest.mark.parametrize(
    "boundary", ["inputs", "intents", "objects", "accessions", "receipts"]
)
def test_existing_immutable_file_sync_failure_is_not_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    item = _acquire(root, source)
    with writer_lock(root) as lock:
        retained_digest = put_retained(root, b"retained", lock=lock)
    retry_id = byte_digest(item.retry_key.encode())
    name = {
        "inputs": retained_digest,
        "intents": f"{retry_id}.json",
        "objects": item.object_digest,
        "accessions": f"{item.accession_id}.json",
        "receipts": f"{retry_id}.json",
    }[boundary]
    inode = (root / boundary / name).stat().st_ino
    original = os.fsync
    failures = 0

    def fail_file_sync(fd: int) -> None:
        nonlocal failures
        if os.fstat(fd).st_ino == inode:
            failures += 1
            raise OSError(errno.ENOSPC, "injected immutable file fsync failure")
        original(fd)

    def retry() -> None:
        if boundary == "inputs":
            with writer_lock(root) as lock:
                put_retained(root, b"retained", lock=lock)
        else:
            _acquire(root, source)

    monkeypatch.setattr(os, "fsync", fail_file_sync)
    for _ in range(2):
        with pytest.raises(StorageError) as error:
            retry()
        assert error.value.code is StorageCode.UNCERTAIN
    assert failures == 2
    monkeypatch.setattr(os, "fsync", original)
    retry()
    inventory(root, (item,))
    assert read_retained(root, retained_digest) == b"retained"


@pytest.mark.parametrize("size", [1, 7])
def test_inventory_rejects_wrong_object_size_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"source")
    item = _acquire(root, source)
    target = root / "objects" / item.object_digest
    target.write_bytes(b"x" * size)
    inode = target.stat().st_ino
    original = os.read

    def reject_object_read(fd: int, length: int) -> bytes:
        if os.fstat(fd).st_ino == inode:
            pytest.fail("read object despite known size mismatch")
        return original(fd, length)

    monkeypatch.setattr(os, "read", reject_object_read)
    with pytest.raises(StorageError) as error:
        inventory(root, (item,))
    assert error.value.code is StorageCode.CORRUPT


@pytest.mark.parametrize("operation", ["acquire", "inventory", "publication"])
def test_fingerprinting_and_acquisition_bound_reads_of_growing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    item = _acquire(root, source)
    stage, digest = _generation(root, source.read_bytes())
    target = {
        "acquire": source,
        "inventory": root / "objects" / item.object_digest,
        "publication": stage / "record.parquet",
    }[operation]
    inode = target.stat().st_ino
    size = target.stat().st_size
    original = os.read
    consumed = 0

    def append_during_read(fd: int, length: int) -> bytes:
        nonlocal consumed
        chunk = original(fd, length)
        if os.fstat(fd).st_ino == inode:
            consumed += len(chunk)
            if consumed > size + 1:
                pytest.fail("unbounded read of a growing file")
            with target.open("ab") as stream:
                stream.write(b"x" * (1024 * 1024))
        return chunk

    def check_growth() -> None:
        if operation == "acquire":
            _acquire(root, source)
        elif operation == "inventory":
            inventory(root, (item,))
        else:
            with writer_lock(root) as lock:
                publish(root, stage, digest, lock=lock)

    monkeypatch.setattr(os, "read", append_during_read)
    with pytest.raises(StorageError) as error:
        check_growth()
    assert error.value.code is StorageCode.CHANGED
    assert consumed == size + 1
    assert not (root / "current.json").exists()


@pytest.mark.parametrize("replacement", ["missing", "file", "symlink"])
def test_replaced_lock_file_invalidates_held_writer(
    tmp_path: Path, replacement: str
) -> None:
    with writer_lock(tmp_path) as lock:
        path = tmp_path / ".writer.lock"
        path.unlink()
        if replacement == "file":
            path.write_bytes(b"replacement")
        elif replacement == "symlink":
            target = tmp_path / "replacement"
            target.write_bytes(b"replacement")
            path.symlink_to(target)
        with pytest.raises(StorageError) as error:
            put_retained(tmp_path, b"must not install", lock=lock)
        assert error.value.code is StorageCode.LOCK_REQUIRED
        assert not (tmp_path / "inputs").exists()


def test_indexed_acquisition_and_verified_object_reads(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"private source")
    item = _acquire(root, source)
    assert read_acquisition(root, item.accession_id) == item
    assert read_object(root, item) == b"private source"
    with pytest.raises(StorageError, match="missing"):
        read_acquisition(root, "0" * 64)
    with pytest.raises(StorageError, match="path"):
        read_acquisition(root, "../escape")
    index = root / "accessions" / f"{item.accession_id}.json"
    canonical = index.read_bytes()
    index.write_bytes(canonical.replace(b'"size":14', b'"size":true'))
    with pytest.raises(StorageError, match="corrupt"):
        read_acquisition(root, item.accession_id)
    with pytest.raises(StorageError, match="corrupt"):
        read_object(root, item)
    index.write_bytes(canonical.replace(item.accession_id.encode(), b"0" * 64))
    with pytest.raises(StorageError, match="corrupt"):
        read_acquisition(root, item.accession_id)
    index.write_bytes(canonical)
    receipt = root / "receipts" / f"{byte_digest(item.retry_key.encode())}.json"
    receipt.write_bytes(b"invalid")
    with pytest.raises(StorageError, match="corrupt"):
        read_acquisition(root, item.accession_id)


@pytest.mark.parametrize("payload", [b"larger than receipt", b"small", b"XXXXXX"])
def test_object_size_mismatch_precedes_reads_and_digest_is_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"source")
    item = _acquire(root, source)
    object_path = root / "objects" / item.object_digest
    object_path.write_bytes(payload)
    inode = object_path.stat().st_ino
    original_read = os.read
    requested: list[int] = []

    def record(fd: int, length: int) -> bytes:
        if os.fstat(fd).st_ino == inode:
            requested.append(length)
        return original_read(fd, length)

    monkeypatch.setattr(os, "read", record)
    with pytest.raises(StorageError) as error:
        read_object(root, item)
    assert error.value.code is StorageCode.CORRUPT
    assert bool(requested) == (len(payload) == item.size)


@pytest.mark.parametrize("mutation", ["grow", "shrink", "rewrite"])
def test_object_read_is_bounded_and_rejects_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    item = _acquire(root, source)
    object_path = root / "objects" / item.object_digest
    inode = object_path.stat().st_ino
    original_read = os.read
    consumed = 0
    reads = 0

    def mutate(fd: int, length: int) -> bytes:
        nonlocal consumed, reads
        chunk = original_read(fd, length)
        if os.fstat(fd).st_ino == inode:
            consumed += len(chunk)
            reads += 1
            if mutation == "grow":
                with object_path.open("ab") as stream:
                    stream.write(b"x" * (1024 * 1024))
            elif reads == 1:
                object_path.write_bytes(
                    b"short" if mutation == "shrink" else b"y" * item.size
                )
        return chunk

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(StorageError) as error:
        read_object(root, item)
    assert error.value.code is StorageCode.CHANGED
    assert consumed <= item.size + 1
    assert reads <= 3


def test_generic_retained_read_has_no_importer_size_cap(tmp_path: Path) -> None:
    payload = b"x" * (2 * 1024 * 1024)
    with writer_lock(tmp_path) as lock:
        digest = put_retained(tmp_path, payload, lock=lock)
    assert read_retained(tmp_path, digest) == payload


def test_committed_retry_does_not_recreate_missing_evidence(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"required")
    item = _acquire(root, source)
    object_path = root / "objects" / item.object_digest
    object_path.unlink()
    with pytest.raises(StorageError, match="missing"):
        _acquire(root, source)
    assert not object_path.exists()
    object_path.write_bytes(b"required")
    (root / "accessions" / f"{item.accession_id}.json").unlink()
    with pytest.raises(StorageError, match="missing"):
        _acquire(root, source)
    with pytest.raises(StorageError, match="missing"):
        inventory(root, (item,))


def test_pinned_storage_reads_do_not_discover_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"statement")
    item = _acquire(root, source)
    with writer_lock(root) as lock:
        digest = put_retained(root, b"declaration", lock=lock)

    def reject_discovery(_fd: int) -> list[str]:
        message = "unexpected directory scan"
        raise AssertionError(message)

    monkeypatch.setattr(os, "listdir", reject_discovery)
    assert read_acquisition(root, item.accession_id) == item
    assert read_object(root, item) == b"statement"
    assert read_retained(root, digest) == b"declaration"


def test_retained_bytes_are_deduplicated_verified_and_separate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    with writer_lock(root) as lock:
        digest = put_retained(root, b"authored input", lock=lock)
        assert put_retained(root, b"authored input", lock=lock) == digest
    assert digest == byte_digest(b"authored input")
    assert read_retained(root, digest) == b"authored input"
    assert not (root / "receipts").exists()
    assert stat.S_IMODE((root / "inputs" / digest).stat().st_mode) == 0o600
    with pytest.raises(StorageError, match="missing"):
        read_retained(root, "0" * 64)
    (root / "inputs" / digest).write_bytes(b"changed")
    with pytest.raises(StorageError, match="corrupt"):
        read_retained(root, digest)
    with writer_lock(root) as lock, pytest.raises(StorageError, match="corrupt"):
        put_retained(root, b"authored input", lock=lock)


def test_retained_write_directory_fsync_failure_is_uncertain_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    original = os.fsync
    failed = False

    def fail_after_install(fd: int) -> None:
        nonlocal failed
        if (
            not failed
            and (root / "inputs").exists()
            and os.fstat(fd).st_ino == (root / "inputs").stat().st_ino
        ):
            failed = True
            raise OSError(errno.ENOSPC, "injected directory synchronization failure")
        original(fd)

    with writer_lock(root) as lock:
        monkeypatch.setattr(os, "fsync", fail_after_install)
        with pytest.raises(StorageError) as error:
            put_retained(root, b"evidence", lock=lock)
        assert error.value.code is StorageCode.UNCERTAIN
        monkeypatch.setattr(os, "fsync", original)
        assert put_retained(root, b"evidence", lock=lock) == byte_digest(b"evidence")
    assert failed
    assert read_retained(root, byte_digest(b"evidence")) == b"evidence"


def test_disk_full_copy_keeps_source_and_no_committed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"non-destructive")
    original = os.write

    def fail_write(fd: int, data: bytes) -> int:
        if data == b"non-destructive":
            raise OSError(errno.ENOSPC, "injected full disk")
        return original(fd, data)

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="full disk"):
        _acquire(root, source)
    assert source.read_bytes() == b"non-destructive"
    assert not (root / "receipts").exists()
    monkeypatch.setattr(os, "write", original)
    item = _acquire(root, source)
    assert read_object(root, item) == b"non-destructive"


def test_receipt_fsync_failure_reports_uncertainty_and_retry_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    original = os.fsync
    failed = False

    def fail_receipt_sync(fd: int) -> None:
        nonlocal failed
        if (
            not failed
            and (root / "receipts").exists()
            and os.fstat(fd).st_ino == (root / "receipts").stat().st_ino
        ):
            failed = True
            raise OSError(errno.ENOSPC, "injected receipt fsync failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_receipt_sync)
    with pytest.raises(StorageError) as error:
        _acquire(root, source)
    assert error.value.code is StorageCode.UNCERTAIN
    assert failed
    assert source.read_bytes() == b"evidence"
    monkeypatch.setattr(os, "fsync", original)
    item = _acquire(root, source)
    assert read_acquisition(root, item.accession_id) == item
    assert read_object(root, item) == b"evidence"


def test_generation_directory_fsync_failure_preserves_old_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    first, first_digest = _generation(root, b"old")
    with writer_lock(root) as lock:
        publish(root, first, first_digest, lock=lock)
    stage, digest = _generation(root, b"new")
    original = os.fsync
    generations_inode = (root / "generations").stat().st_ino
    failed = False

    def fail_generation_sync(fd: int) -> None:
        nonlocal failed
        if not failed and os.fstat(fd).st_ino == generations_inode:
            failed = True
            raise OSError(errno.ENOSPC, "injected generation fsync failure")
        original(fd)

    with writer_lock(root) as lock:
        monkeypatch.setattr(os, "fsync", fail_generation_sync)
        with pytest.raises(OSError, match="generation fsync"):
            publish(root, stage, digest, lock=lock)
        monkeypatch.setattr(os, "fsync", original)
    assert failed
    assert read_current(root).descriptor_digest == first_digest
    assert not stage.exists()
    with writer_lock(root) as lock:
        publish(root, stage, digest, lock=lock)
    assert read_current(root).descriptor_digest == digest


def test_pointer_file_fsync_failure_preserves_old_pointer_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    first, first_digest = _generation(root, b"before")
    with writer_lock(root) as lock:
        publish(root, first, first_digest, lock=lock)
    stage, digest = _generation(root, b"after")
    original = os.fsync
    failed = False

    def fail_pointer_sync(fd: int) -> None:
        nonlocal failed
        temporary = next(root.glob(".current-*"), None)
        if (
            not failed
            and temporary is not None
            and os.fstat(fd).st_ino == temporary.stat().st_ino
        ):
            failed = True
            raise OSError(errno.ENOSPC, "injected pointer fsync failure")
        original(fd)

    with writer_lock(root) as lock:
        monkeypatch.setattr(os, "fsync", fail_pointer_sync)
        with pytest.raises(OSError, match="pointer fsync"):
            publish(root, stage, digest, lock=lock)
        monkeypatch.setattr(os, "fsync", original)
    assert failed
    assert read_current(root).descriptor_digest == first_digest
    assert not list(root.glob(".current-*"))
    with writer_lock(root) as lock:
        publish(root, stage, digest, lock=lock)
    assert read_current(root).descriptor_digest == digest


def test_corrupt_existing_object_is_never_replaced(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"input")
    item = _acquire(root, source)
    object_path = root / "objects" / item.object_digest
    object_path.unlink()
    object_path.symlink_to(source)
    with pytest.raises(StorageError) as error:
        _acquire(root, source, "another")
    assert error.value.code is StorageCode.PATH
    assert object_path.is_symlink()
    assert source.read_bytes() == b"input"


def test_subprocess_writer_refusal_and_token_lifetime(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "input"
    source.write_bytes(b"abc")
    code = (
        "from pathlib import Path; "
        "from jbt.storage.local import writer_lock, StorageError, StorageCode; "
        "import sys; "
        "root=Path(sys.argv[1]); "
        "try_code=''; "
        "exec('try:\\n with writer_lock(root): pass\\n"
        "except StorageError as error:\\n assert error.code is StorageCode.LOCKED\\n"
        'else:\\n raise AssertionError("lock acquired")\')'
    )
    with writer_lock(root) as lock:
        result = subprocess.run(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", code, str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(StorageError) as error:
        acquire(
            root,
            source,
            retry_key="key",
            acquired_at="time",
            lock=lock,
            source_scope_id=None,
            importer_id=None,
        )
    assert error.value.code is StorageCode.LOCK_REQUIRED


def test_publication_pinned_read_and_inventory(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    one, first_digest = _generation(root, b"one")
    with writer_lock(root) as lock:
        first = publish(root, one, first_digest, lock=lock)
    assert first == read_current(root)
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((first.path / "descriptor.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "current.json").stat().st_mode) == 0o600
    two, next_digest = _generation(root, b"two")
    with writer_lock(root) as lock:
        publish(root, two, next_digest, lock=lock)
    assert read_current(root).descriptor_digest == next_digest
    assert first.path.joinpath("manifest.json").read_bytes() == b"one"
    (root / "generations" / next_digest / "record.parquet").write_bytes(b"bad")
    with pytest.raises(StorageError, match="corrupt: payload digest"):
        read_current(root)


def test_generation_collision_rejects_corruption(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    stage, digest = _generation(root, b"complete")
    with writer_lock(root) as lock:
        publish(root, stage, digest, lock=lock)
    record = root / "generations" / digest / "record.parquet"
    record.write_bytes(b"tampered")
    retry, _ = _generation(root, b"complete")
    with writer_lock(root) as lock, pytest.raises(StorageError, match="corrupt"):
        publish(root, retry, digest, lock=lock)
    assert record.read_bytes() == b"tampered"
    assert retry.exists()


def test_failed_publication_preserves_current_and_post_replace_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    one, digest = _generation(root, b"one")
    with writer_lock(root) as lock:
        publish(root, one, digest, lock=lock)
    broken, broken_digest = _generation(root, b"broken")
    (broken / "extra").write_bytes(b"unexpected")
    with writer_lock(root) as lock, pytest.raises(StorageError, match="inventory"):
        publish(root, broken, broken_digest, lock=lock)
    assert read_current(root).descriptor_digest == digest
    broken.joinpath("extra").unlink()
    original_fsync = os.fsync
    root_inode = root.stat().st_ino

    def fail_root_sync(fd: int) -> None:
        if (
            os.fstat(fd).st_ino == root_inode
            and broken_digest.encode() in (root / "current.json").read_bytes()
        ):
            message = "injected post-replace failure"
            raise OSError(message)
        original_fsync(fd)

    with writer_lock(root) as lock:
        monkeypatch.setattr(os, "fsync", fail_root_sync)
        with pytest.raises(StorageError) as error:
            publish(root, broken, broken_digest, lock=lock)
        assert error.value.code is StorageCode.UNCERTAIN
        monkeypatch.setattr(os, "fsync", original_fsync)
    assert read_current(root).descriptor_digest == broken_digest
    with writer_lock(root) as lock:
        assert publish(root, broken, broken_digest, lock=lock) == read_current(root)


def test_reject_symlinked_source_and_generation(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    link = tmp_path / "link"
    link.symlink_to(source)
    with writer_lock(root) as lock, pytest.raises(StorageError, match="path"):
        acquire(
            root,
            link,
            retry_key="a",
            acquired_at="time",
            lock=lock,
            source_scope_id=None,
            importer_id=None,
        )
    stage, digest = _generation(root, b"valid")
    (stage / "link").symlink_to(source)
    with writer_lock(root) as lock, pytest.raises(StorageError, match="path"):
        publish(root, stage, digest, lock=lock)


@pytest.mark.parametrize("boundary", ["intent", "object", "receipt"])
def test_interrupted_acquisition_retries_without_losing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    original_link = os.link
    failed = False

    def fail_once(
        src: str,
        dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal failed
        matched = (boundary == "object") == (not dst.endswith(".json"))
        if not failed and matched:
            failed = True
            message = "injected installation failure"
            raise OSError(message)
        original_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    if boundary == "receipt":

        def fail_receipt(
            src: str,
            dst: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
        ) -> None:
            nonlocal failed
            if (
                not failed
                and dst.endswith(".json")
                and (root / "receipts").exists()
                and os.fstat(dst_dir_fd).st_ino == (root / "receipts").stat().st_ino
            ):
                failed = True
                message = "injected receipt failure"
                raise OSError(message)
            original_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        monkeypatch.setattr(os, "link", fail_receipt)
    else:
        monkeypatch.setattr(os, "link", fail_once)
    with pytest.raises((OSError, StorageError)):
        _acquire(root, source)
    assert failed
    assert source.read_bytes() == b"evidence"
    monkeypatch.setattr(os, "link", original_link)
    item = _acquire(root, source)
    inventory(root, (item,))


def test_source_mutation_during_copy_refuses_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"before")
    original_read = os.read
    changed = False

    def mutate(fd: int, length: int) -> bytes:
        nonlocal changed
        content = original_read(fd, length)
        if content and not changed and os.fstat(fd).st_ino == source.stat().st_ino:
            changed = True
            source.write_bytes(b"after")
        return content

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(StorageError) as error:
        _acquire(root, source)
    assert error.value.code is StorageCode.CHANGED
    assert not (root / "receipts").exists()
    monkeypatch.setattr(os, "read", original_read)
    item = _acquire(root, source)
    assert item.object_digest == byte_digest(b"after")


def test_pointer_replace_failure_preserves_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    first, first_digest = _generation(root, b"first")
    with writer_lock(root) as lock:
        publish(root, first, first_digest, lock=lock)
    next_stage, next_digest = _generation(root, b"next")
    original_replace = os.replace

    def fail_replace(
        src: str,
        dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        assert src.startswith(".current-")
        assert dst == "current.json"
        assert src_dir_fd == dst_dir_fd
        message = "injected pointer failure"
        raise OSError(message)

    monkeypatch.setattr(os, "replace", fail_replace)
    with writer_lock(root) as lock, pytest.raises(OSError, match="injected"):
        publish(root, next_stage, next_digest, lock=lock)
    assert read_current(root).descriptor_digest == first_digest
    monkeypatch.setattr(os, "replace", original_replace)
    with writer_lock(root) as lock:
        publish(root, next_stage, next_digest, lock=lock)
    assert read_current(root).descriptor_digest == next_digest
