"""Retained selection and bounded project-file boundary."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import pytest

from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.importers.ofx import MAX_BYTES
from jbt.runtime import inputs
from jbt.runtime.inputs import (
    MAXIMUM_PROJECT_BYTES,
    canonical_root,
    confined,
    read_project_file,
    require_source_size,
)
from jbt.storage.local import Acquisition, acquire, put_retained, writer_lock

if TYPE_CHECKING:
    from pathlib import Path

    from jbt.runtime.execution import ObservedExecution


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/absolute",
        "../outside",
        "folder/../outside",
        "folder//file",
        "./file",
        "folder/./file",
        "C:/file",
        "folder\\file",
        "folder/",
    ],
)
def test_rejects_noncanonical_relative_paths(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ContractError, match="relative_path_required"):
        confined(tmp_path, relative)


def test_canonicalizes_root_alias_but_rejects_symlinked_root(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    selected = physical / "project"
    selected.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    assert canonical_root(alias / "project") == selected
    assert canonical_root(alias / "project" / ".") == selected
    with pytest.raises(ContractError, match="symlink_root"):
        canonical_root(alias)
    (selected / "project.json").write_bytes(b"{}")
    (selected / "linked.json").symlink_to(selected / "project.json")
    with pytest.raises(ContractError, match="symlink_path"):
        read_project_file(canonical_root(alias / "project"), "linked.json")


def test_refuses_symlinked_parent_and_file(tmp_path: Path) -> None:
    directory = tmp_path / "ordinary"
    directory.mkdir()
    (directory / "project.json").write_bytes(b"{}")
    (tmp_path / "alias").symlink_to(directory, target_is_directory=True)
    (tmp_path / "linked.json").symlink_to(directory / "project.json")
    (tmp_path / "loop").symlink_to("loop")
    for path in ("alias/project.json", "linked.json", "loop"):
        with pytest.raises(ContractError, match="symlink_path"):
            read_project_file(tmp_path, path)


def test_capture_requires_ordinary_bounded_file(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_bytes(b"{}")
    assert read_project_file(tmp_path, "project.json") == b"{}"
    with pytest.raises(ContractError, match="relative_path_required"):
        read_project_file(tmp_path, str(tmp_path / "project.json"))
    (tmp_path / "project.json").write_bytes(b"x" * (MAXIMUM_PROJECT_BYTES + 1))
    with pytest.raises(ContractError, match="project_file_type_or_size"):
        read_project_file(tmp_path, "project.json")
    (tmp_path / "directory").mkdir()
    with pytest.raises(ContractError, match="project_file_type_or_size"):
        read_project_file(tmp_path, "directory")


def test_capture_and_reload_only_selected_retained_inventory(
    tmp_path: Path,
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
    release_execution: ObservedExecution,
) -> None:
    (tmp_path / "project.json").write_bytes(
        encode_json(project_document, integer_strings=False)
    )
    acquisitions = (
        Acquisition(
            "retry",
            "source",
            "ofx",
            "2026-01-01T00:00:00Z",
            "opening.ofx",
            "a" * 64,
            1,
            "opening",
        ),
    )
    selected = acquisitions
    monkeypatch.setattr(inputs, "_bound_acquisitions", lambda *_args: selected)
    monkeypatch.setattr(inputs, "inventory", lambda *_args: None)
    with writer_lock(tmp_path) as lock:
        snapshot = inputs.capture_inputs(
            tmp_path,
            project_path="project.json",
            execution=release_execution,
            baseline=None,
            lock=lock,
        )
    loaded, project = inputs.load_inputs(tmp_path, snapshot.digest)
    assert loaded == snapshot
    assert project.entity.value == project_document["entity_id"]
    selected = ()
    with pytest.raises(ContractError, match="acquisition_inventory_changed"):
        inputs.load_inputs(tmp_path, snapshot.digest)


@pytest.mark.parametrize("operation", ["capture", "load"])
def test_oversized_source_binding_fails_before_inventory_reads(
    tmp_path: Path,
    project_document: dict,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source = tmp_path / "source.ofx"
    source.write_bytes(b"x" * (MAX_BYTES + 1))
    acquisitions = []
    with writer_lock(root) as lock:
        for index, selection in enumerate(project_document["sources"]):
            item = acquire(
                root,
                source,
                retry_key=f"source-{index}",
                acquired_at="2026-01-01T00:00:00Z",
                source_scope_id=selection["source_scope_id"],
                importer_id="ofx",
                lock=lock,
            )
            selection["accession_id"] = item.accession_id
            acquisitions.append(item)
        project_payload = encode_json(project_document, integer_strings=False)
        (root / "project.json").write_bytes(project_payload)
        project_digest = put_retained(root, project_payload, lock=lock)
        execution_digest = put_retained(root, release_execution.payload, lock=lock)
        digest = put_retained(
            root,
            encode_json(
                {
                    "schema_version": 1,
                    "project_digest": project_digest,
                    "execution_digest": execution_digest,
                    "acquisitions": [asdict(item) for item in acquisitions],
                    "baseline_path": None,
                    "baseline_digest": None,
                },
                integer_strings=False,
            ),
            lock=lock,
        )
        monkeypatch.setattr(
            inputs,
            "inventory",
            lambda *_args: pytest.fail("source inventory read before size guard"),
        )
        if operation == "capture":
            with pytest.raises(ContractError, match="source_size_limit"):
                inputs.capture_inputs(
                    root,
                    project_path="project.json",
                    execution=release_execution,
                    baseline=None,
                    lock=lock,
                )
        else:
            with pytest.raises(ContractError, match="source_size_limit"):
                inputs.load_inputs(root, digest)
        require_source_size(replace(acquisitions[0], size=MAX_BYTES))
