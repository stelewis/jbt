from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from jbt import cli
from jbt.artifacts.beancount import BeancountProjectionError
from jbt.domain.errors import IdentityError, NumericLimitError
from jbt.runtime import execution, worker

if TYPE_CHECKING:
    from pathlib import Path


def arguments(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    monkeypatch.setattr(sys, "argv", ["jbt", *values])


def test_help_has_only_local_financial_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments(monkeypatch, "--help")
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "acquire" in text
    assert "prepare" not in text
    assert "environment" not in text


def test_build_dispatches_once_observed_execution_and_canonical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    physical = tmp_path / "physical"
    root = physical / "project"
    root.mkdir(parents=True)
    (root / "project.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    observed = object()
    calls: list[str] = []

    def observe() -> object:
        calls.append("observe")
        return observed

    def build(selected: Path, **kwargs: object) -> dict:
        assert selected == root
        assert kwargs == {
            "project": "project.json",
            "execution": observed,
            "comparison_baseline": None,
        }
        calls.append("build")
        return {"generation": "pinned"}

    monkeypatch.setattr(execution, "observe_execution", observe)
    monkeypatch.setattr(worker, "build", build)
    arguments(
        monkeypatch,
        "build",
        "--root",
        str(alias / "project"),
        "--project",
        "project.json",
    )
    assert cli.main() == 0
    assert calls == ["observe", "build"]
    assert json.loads(capsys.readouterr().out) == {"generation": "pinned"}


@pytest.mark.parametrize("project", ["../outside", "/absolute"])
def test_confined_project_rejected_before_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: str,
) -> None:
    monkeypatch.setattr(execution, "observe_execution", lambda: pytest.fail("observed"))
    arguments(monkeypatch, "build", "--root", str(tmp_path), "--project", project)
    assert cli.main() == 1
    assert "relative_path_required" in capsys.readouterr().err


def test_acquisition_preserves_evidence_and_rejects_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.ofx"
    source.write_bytes(b"synthetic evidence")
    root = tmp_path / "project"
    root.mkdir()
    base = [
        "acquire",
        "--root",
        str(root),
        str(source),
        "--retry-key",
        "first",
        "--acquired-at",
        "2026-02-02T00:00:00Z",
        "--source-scope",
        "bank-feed",
        "--importer",
        "ofx",
    ]
    arguments(monkeypatch, *base)
    assert cli.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert source.read_bytes() == b"synthetic evidence"
    assert (
        root / "objects" / receipt["object_digest"]
    ).read_bytes() == source.read_bytes()
    linked = tmp_path / "linked.ofx"
    linked.symlink_to(source)
    base[3] = str(linked)
    arguments(monkeypatch, *base)
    assert cli.main() == 1
    assert "local storage: path" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    [
        IdentityError(constraint="canonical_key_required", location="entity_id"),
        NumericLimitError(constraint="stored_digits", location="number.coefficient"),
        BeancountProjectionError("category_root_mismatch", "income"),
    ],
)
def test_domain_and_sink_failures_emit_only_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: ValueError,
) -> None:
    def reject(_arguments: object) -> None:
        raise failure

    monkeypatch.setattr(cli, "_run", reject)
    arguments(monkeypatch, "verify", "--root", ".", "--baseline", "a" * 64)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"jbt: {failure}\n"


def test_invalid_project_identity_is_reported_without_a_traceback(
    tmp_path: Path,
    project_document: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_document["entity_id"] = "invalid\nidentity"
    (tmp_path / "project.json").write_text(json.dumps(project_document))
    arguments(
        monkeypatch, "build", "--root", str(tmp_path), "--project", "project.json"
    )
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "jbt: entity_id: canonical_key_required\n"
    assert not (tmp_path / "current.json").exists()
