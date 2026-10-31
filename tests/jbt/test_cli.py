from __future__ import annotations

import json
import sys
from datetime import date
from importlib import metadata
from typing import TYPE_CHECKING

import pytest

from jbt import cli
from jbt.artifacts.beancount import BeancountProjectionError
from jbt.domain.decisions import IdentityMode
from jbt.domain.errors import IdentityError, NumericLimitError
from jbt.runtime import decisions, execution, worker
from jbt.runtime.decisions import (
    DocumentLinkRequest,
    IdentityRequest,
    LifecycleLinkRequest,
    RevisionRequest,
    TransferRequest,
    report,
)

if TYPE_CHECKING:
    from pathlib import Path


def arguments(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    monkeypatch.setattr(sys, "argv", ["jbt", *values])


@pytest.mark.parametrize("importer", [None, "custom-not-installed"])
def test_acquisition_does_not_load_importers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    importer: str | None,
    project_document: dict,
) -> None:
    source = tmp_path / "evidence"
    source.write_bytes(b"synthetic archive")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **_kwargs: pytest.fail("acquisition discovered plugins"),
    )
    values = [
        "acquire",
        "--root",
        str(root),
        str(source),
        "--retry-key",
        "archive",
        "--acquired-at",
        "2026-01-01T00:00:00Z",
        "--source-scope",
        "bank-feed",
    ]
    if importer is not None:
        values.extend(("--importer", importer))
    arguments(monkeypatch, *values)
    assert cli.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["importer_id"] == importer
    if importer is None:
        values[5] = "archive-two"
        arguments(monkeypatch, *values)
        assert cli.main() == 0
        second = json.loads(capsys.readouterr().out)
        project_document["sources"] = [
            retained["accession_id"] for retained in (receipt, second)
        ]
        (root / "project.json").write_text(json.dumps(project_document))
        arguments(
            monkeypatch, "build", "--root", str(root), "--project", "project.json"
        )
        assert cli.main() == 1
        diagnostic = capsys.readouterr().err
        assert "archive_only_source" in diagnostic
        assert "attempt=" in diagnostic
        attempt = diagnostic.strip().rsplit("attempt=", 1)[1]
        retained = report(root, attempt)
        assert retained["model"] is None
        failure = retained["failure"]
        assert isinstance(failure, dict)
        assert failure["code"] == "archive_only_source"
        assert not (root / "current.json").exists()
        assert not (root / "cache").exists()


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


@pytest.mark.parametrize(
    ("command", "options", "method", "expected"),
    [
        ("review", (), "show", {"identifier": None}),
        ("show", ("target",), "show", {"identifier": "target"}),
        (
            "decide",
            (
                "--key",
                "move",
                "--reason",
                "review",
                "transfer",
                "--outgoing",
                "a" * 64,
                "--outgoing",
                "b" * 64,
                "--commodity",
                "NZD",
            ),
            "decide",
            {
                "decision_key": "move",
                "reason": "review",
                "supersede": None,
                "request": TransferRequest(("a" * 64, "b" * 64), (), "NZD"),
            },
        ),
        (
            "decide",
            (
                "--key",
                "receipt",
                "--reason",
                "review",
                "document",
                "--target",
                "a" * 64,
                "--document",
                "b" * 64,
                "--document",
                "c" * 64,
            ),
            "decide",
            {
                "decision_key": "receipt",
                "reason": "review",
                "supersede": None,
                "request": DocumentLinkRequest("a" * 64, ("b" * 64, "c" * 64)),
            },
        ),
        (
            "decide",
            (
                "--key",
                "failed",
                "--reason",
                "review",
                "lifecycle",
                "--failed-attempt",
                "a" * 64,
                "--fee",
                "b" * 64,
            ),
            "decide",
            {
                "decision_key": "failed",
                "reason": "review",
                "supersede": None,
                "request": LifecycleLinkRequest("a" * 64, "b" * 64),
            },
        ),
        (
            "correct",
            ("--set", "transactions.narration=a=b"),
            "correct",
            {"field": "transactions.narration", "value": "a=b", "children": ()},
        ),
        (
            "correct",
            ("--clear", "transactions.payee"),
            "correct",
            {"field": "transactions.payee", "value": None, "children": ()},
        ),
        (
            "correct",
            ("--split", "one=2", "--split", "two=3"),
            "correct",
            {"field": None, "value": None, "children": ("one=2", "two=3")},
        ),
        (
            "retract",
            ("entry", "--reason", "review"),
            "retract",
            {"decision_key": "entry"},
        ),
        (
            "decide",
            (
                "--key",
                "identity",
                "--reason",
                "review",
                "identity",
                "--mode",
                "key_family",
                "--canonical",
                "a" * 64,
                "--member",
                "b" * 64,
            ),
            "decide",
            {
                "decision_key": "identity",
                "reason": "review",
                "supersede": None,
                "request": IdentityRequest(
                    IdentityMode.KEY_FAMILY, "a" * 64, ("b" * 64,)
                ),
            },
        ),
        (
            "decide",
            (
                "--key",
                "reissue",
                "--reason",
                "review",
                "statement-revision",
                "--replace",
                "a" * 64,
                "--with",
                "b" * 64,
                "--account",
                "bank",
                "--fact-kind",
                "movement",
                "--basis",
                "posted",
                "--start",
                "2026-01-01",
                "--end",
                "2026-02-01",
            ),
            "decide",
            {
                "decision_key": "reissue",
                "reason": "review",
                "supersede": None,
                "request": RevisionRequest(
                    ("a" * 64,),
                    ("b" * 64,),
                    "bank",
                    "movement",
                    "posted",
                    date(2026, 1, 1),
                    date(2026, 2, 1),
                ),
            },
        ),
    ],
)
def test_review_commands_dispatch_without_building(  # noqa: PLR0913 - explicit CLI cases
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    command: str,
    options: tuple[str, ...],
    method: str,
    expected: dict,
) -> None:
    calls = []

    def dispatch(root: Path, **values: object) -> dict:
        assert root == tmp_path.resolve()
        calls.append(values)
        return {"entry": "recorded"}

    monkeypatch.setattr(decisions, method, dispatch)
    monkeypatch.setattr(worker, "build", lambda *_a, **_k: pytest.fail("built"))
    monkeypatch.setattr(
        execution, "observe_execution", lambda: pytest.fail("observed execution")
    )
    common = ("--target", "target", "--key", "choice", "--reason", "review")
    arguments(
        monkeypatch,
        command,
        "--root",
        str(tmp_path),
        "--attempt",
        "attempt",
        *options,
        *(common if command == "correct" else ()),
    )
    assert cli.main() == 0
    assert len(calls) == 1
    assert calls[0] == {
        "attempt": "attempt",
        **(
            {"identifier": "target", "decision_key": "choice", "supersede": None}
            if command == "correct"
            else {}
        ),
        **({"reason": "review"} if command in {"correct", "retract"} else {}),
        **expected,
    }
    assert json.loads(capsys.readouterr().out) == {"entry": "recorded"}


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
    assert captured.err.startswith("jbt: canonical_key_required: entity_id; attempt=")
    attempt = captured.err.strip().rsplit("attempt=", 1)[1]
    retained = report(tmp_path, attempt)
    assert retained["model"] is None
    assert retained["failure"] == {
        "code": "canonical_key_required",
        "location": "entity_id",
    }
    assert "invalid\nidentity" not in captured.err
    assert not (tmp_path / "current.json").exists()
