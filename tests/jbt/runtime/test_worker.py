import json
import socket
from contextlib import suppress
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.beancount import BeancountProjectionError
from jbt.artifacts.reader import read_pinned
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.domain.errors import NumericLimitError
from jbt.importers.ofx import OfxError
from jbt.runtime import build as build_module
from jbt.runtime import inputs, stages, worker
from jbt.runtime.decisions import report
from jbt.runtime.execution import ObservedExecution, decode_execution
from jbt.runtime.network import NetworkAccessError
from jbt.storage.local import acquire, writer_lock

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jbt.runtime.build import BuildResult
    from jbt.runtime.inputs import InputSnapshot
    from jbt.runtime.project import Project


@pytest.fixture
def financial_root(tmp_path: Path, project_document: dict) -> Path:
    source = Path(__file__).parents[1] / "importers/fixtures_synthetic"
    with writer_lock(tmp_path) as lock:
        for name in ("prior.ofx", "main.ofx"):
            acquire(
                tmp_path,
                source / name,
                retry_key=name,
                acquired_at="2026-02-02T00:00:00Z",
                source_scope_id="bank-feed",
                importer_id="ofx",
                lock=lock,
            )
    (tmp_path / "project.json").write_bytes(
        encode_json(project_document, integer_strings=False)
    )
    return tmp_path


def run_build(root: Path, execution: ObservedExecution) -> worker.BuildReport:
    return worker.build(
        root, project="project.json", execution=execution, comparison_baseline=None
    )


def test_missing_importer_retains_located_failure_without_cache(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "entry_points", lambda **_kwargs: [])
    monkeypatch.setattr(
        stages.StageRunner, "__init__", lambda *_args: pytest.fail("constructed runner")
    )
    with pytest.raises(inputs.InputCaptureError, match="importer_missing") as failure:
        run_build(financial_root, release_execution)
    retained = report(financial_root, failure.value.attempt)
    assert retained["model"] is None
    assert not (financial_root / "cache").exists()


def test_development_importer_disables_entire_release_execution_cache(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = metadata.entry_points(group="jbt.importers")[0]
    assert entry.dist is not None
    monkeypatch.setattr(
        entry.dist, "read_text", lambda _name: '{"dir_info":{"editable":false}}'
    )
    monkeypatch.setattr(
        stages, "_real_directory", lambda *_args: pytest.fail("read cache")
    )
    monkeypatch.setattr(
        stages, "_write_cache", lambda *_args: pytest.fail("wrote cache")
    )
    result = run_build(financial_root, release_execution)
    assert result["executed"] > 0
    assert result["reused"] == 0
    assert not (financial_root / "cache").exists()
    baseline = read_pinned(financial_root, result["descriptor_digest"])
    recorded = json.loads(baseline.outputs["execution.json"])
    assert recorded["producer"]["kind"] == "release"
    assert recorded["importers"][0]["kind"] == "development"


def test_verify_refuses_changed_producer_importer_or_timezone_rules(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = metadata.entry_points(group="jbt.importers")[0]
    assert entry.dist is not None
    monkeypatch.setattr(
        entry.dist,
        "metadata",
        SimpleNamespace(
            get_all=lambda key: ["synthetic-plugin"] if key == "Name" else ["1"]
        ),
    )
    document = release_execution.document()
    document["distributions"].append({"name": "synthetic-plugin", "version": "1"})
    release_execution = decode_execution(encode_json(document, integer_strings=False))
    result = run_build(financial_root, release_execution)
    current = (financial_root / "current.json").read_bytes()
    rules = build_module.timezone_provenance()
    for change in (
        "distribution_version",
        "profile_version",
        "producer_version",
        "timezone_rules",
    ):
        document = release_execution.document()
        with monkeypatch.context() as patch:
            if change == "distribution_version":
                patch.setattr(
                    entry.dist,
                    "metadata",
                    SimpleNamespace(
                        get_all=lambda key: (
                            ["synthetic-plugin"] if key == "Name" else ["2"]
                        )
                    ),
                )
                document["distributions"][-1]["version"] = "2"
            elif change == "profile_version":
                selected_importer = entry.load()
                patch.setattr(
                    selected_importer,
                    "profile_version",
                    selected_importer.profile_version + 1,
                )
            elif change == "producer_version":
                document["producer"]["version"] = "0.2.0"
                document["distributions"][0]["version"] = "0.2.0"
            else:
                patch.setattr(
                    build_module,
                    "timezone_provenance",
                    lambda: {**rules, "iana_version": "different-rules"},
                )
            observed = decode_execution(encode_json(document, integer_strings=False))
            component = {
                "producer_version": "producer",
                "timezone_rules": "timezone_rules",
            }.get(change, "importer")
            code = f"historical_{component}_mismatch_install_recorded_release"
            with pytest.raises(ContractError, match=code):
                worker.verify(
                    financial_root,
                    baseline_digest=result["descriptor_digest"],
                    execution=observed,
                )
        assert (financial_root / "current.json").read_bytes() == current


def test_build_reuses_release_but_replay_allows_environment_changes(
    financial_root: Path, release_execution: ObservedExecution
) -> None:
    result = run_build(financial_root, release_execution)
    warm = run_build(financial_root, release_execution)
    assert warm["executed"] == 0
    assert warm["reused"] > 0
    document = release_execution.document()
    document["runtime"]["version"] = "3.14.8"
    document["runtime"]["platform"] = "darwin"
    document["producer"]["source_commit"] = None
    document["distributions"].insert(0, {"name": "harmless-extra", "version": "1"})
    changed = decode_execution(encode_json(document, integer_strings=False))
    current = (financial_root / "current.json").read_bytes()
    replay = worker.verify(
        financial_root, baseline_digest=result["descriptor_digest"], execution=changed
    )
    assert replay["equal"] is True
    assert replay["reused"] == 0
    assert replay["recorded_environment"] != replay["observed_environment"]
    assert (financial_root / "current.json").read_bytes() == current
    drifted = run_build(financial_root, changed)
    assert drifted["reused"] == 0
    assert drifted["financial_digest"] == result["financial_digest"]
    assert drifted["descriptor_digest"] != result["descriptor_digest"]


def test_development_bypasses_existing_cache_and_cannot_verify_release(
    financial_root: Path, release_execution: ObservedExecution
) -> None:
    released = run_build(financial_root, release_execution)
    for file in (financial_root / "cache").rglob("*"):
        if file.is_file():
            file.write_bytes(b"deliberately corrupt cache")
    cached = {
        file.relative_to(financial_root): file.read_bytes()
        for file in (financial_root / "cache").rglob("*")
        if file.is_file()
    }
    document = release_execution.document()
    document["producer"].update(kind="development", source_commit=None)
    development = decode_execution(encode_json(document, integer_strings=False))
    first = run_build(financial_root, development)
    assert first["reused"] == 0
    assert first["executed"] > 0
    assert cached == {
        file.relative_to(financial_root): file.read_bytes()
        for file in (financial_root / "cache").rglob("*")
        if file.is_file()
    }
    with pytest.raises(ContractError, match="historical_producer_mismatch"):
        worker.verify(
            financial_root,
            baseline_digest=released["descriptor_digest"],
            execution=development,
        )


def test_caught_network_attempt_blocks_publication_and_verification(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = run_build(financial_root, release_execution)
    current = (financial_root / "current.json").read_bytes()
    generations = set((financial_root / "generations").iterdir())
    document = release_execution.document()
    document["runtime"]["version"] = "3.14.8"
    changed = decode_execution(encode_json(document, integer_strings=False))
    original = worker.execute

    def attempted(  # noqa: PLR0913 - mirrors the intercepted application boundary
        root: Path,
        snapshot: InputSnapshot,
        project: Project,
        destination: Path,
        *,
        execution: ObservedExecution,
        cache: Path | None,
        baseline: Mapping[str, Sequence[dict[str, object]]] | None,
        review: dict[str, object] | None = None,
    ) -> BuildResult:
        result = original(
            root,
            snapshot,
            project,
            destination,
            execution=execution,
            cache=cache,
            baseline=baseline,
            review=review,
        )
        with suppress(NetworkAccessError):
            socket.getaddrinfo("example.invalid", 443)
        return result

    monkeypatch.setattr(worker, "execute", attempted)
    with pytest.raises(
        worker.BuildAttemptError, match="network_access_denied"
    ) as failure:
        run_build(financial_root, changed)
    assert report(financial_root, failure.value.attempt)["failure"] == {
        "code": "network_access_denied",
        "location": "processing",
    }
    assert (financial_root / "current.json").read_bytes() == current
    assert set((financial_root / "generations").iterdir()) == generations
    with pytest.raises(NetworkAccessError):
        worker.verify(
            financial_root,
            baseline_digest=first["descriptor_digest"],
            execution=changed,
        )
    assert (financial_root / "current.json").read_bytes() == current
    assert set((financial_root / "generations").iterdir()) == generations
    assert (
        read_pinned(financial_root, first["descriptor_digest"]).descriptor_digest
        == first["descriptor_digest"]
    )


def test_failed_invocation_does_not_poison_identical_input_retry(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = worker.execute

    def unavailable(*_args: object, **_kwargs: object) -> None:
        code = "synthetic_transient_failure"
        raise ContractError(code, "execute")

    monkeypatch.setattr(worker, "execute", unavailable)
    with pytest.raises(worker.BuildAttemptError) as failure:
        run_build(financial_root, release_execution)
    blocked = report(financial_root, failure.value.attempt)
    assert blocked["model"] is None
    assert not (financial_root / "current.json").exists()
    monkeypatch.setattr(worker, "execute", original)
    success = run_build(financial_root, release_execution)
    retained = report(financial_root, success["attempt_digest"])
    assert success["attempt_digest"] != failure.value.attempt
    retained_selection, blocked_selection = retained["selection"], blocked["selection"]
    assert isinstance(retained_selection, dict)
    assert isinstance(blocked_selection, dict)
    assert retained_selection["input_digest"] == blocked_selection["input_digest"]
    assert retained["failure"] is None
    assert report(financial_root, failure.value.attempt) == blocked


def test_compare_checks_financial_fields_outputs_and_findings(
    financial_root: Path, release_execution: ObservedExecution
) -> None:
    result = run_build(financial_root, release_execution)
    original = read_pinned(financial_root, result["descriptor_digest"])
    for table in ("postings", "provenance", "observations"):
        changed = {
            name: [dict(row) for row in rows] for name, rows in original.tables.items()
        }
        key = next(key for key in changed[table][0] if key != "entity_id")
        changed[table][0][key] = "changed"
        with pytest.raises(ContractError, match=rf"table_mismatch: {table}"):
            worker._compare(original, replace(original, tables=changed))  # noqa: SLF001
    for artifact in original.manifest["artifacts"]:
        if artifact["kind"] not in {"ledger", "summary"}:
            continue
        path = artifact["path"]
        with pytest.raises(ContractError, match="output_mismatch"):
            worker._compare(  # noqa: SLF001
                original,
                replace(original, outputs={**original.outputs, path: b"changed"}),
            )
    with pytest.raises(ContractError, match="financial_manifest_mismatch"):
        worker._compare(  # noqa: SLF001
            original, replace(original, manifest={**original.manifest, "checks": []})
        )


def test_replay_diagnostic_preserves_location_and_environment_facts(
    release_execution: ObservedExecution,
) -> None:
    original = ContractError("table_mismatch", "postings[row=0].amount_coefficient")
    mismatch = worker.ReplayMismatchError(
        original, release_execution, release_execution
    )
    location, facts = str(mismatch).split("; ", 1)
    assert location == str(original)
    assert mismatch.code == original.code
    assert mismatch.location == original.location
    environments = json.loads(facts)
    assert environments["recorded_environment"]["runtime"]["version"] == "3.14.7"
    assert environments["observed_environment"]["distributions"] == [
        {"name": "jbt", "version": "0.1.0a1"}
    ]


def test_replay_processing_failures_keep_environment_and_leave_baseline_unchanged(
    financial_root: Path,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_build(financial_root, release_execution)
    retained = {
        path.relative_to(financial_root): path.read_bytes()
        for path in financial_root.rglob("*")
        if path.is_file()
    }

    failures: tuple[tuple[worker.ReplayFailure, tuple[str, str]], ...] = (
        (
            ContractError("source_key_conflict", "statement"),
            ("source_key_conflict", "statement"),
        ),
        (OfxError("unsupported_element", "OFX"), ("unsupported_element", "OFX")),
        (
            NumericLimitError(
                constraint="stored_digits", location="number.coefficient"
            ),
            ("stored_digits", "number.coefficient"),
        ),
        (
            BeancountProjectionError("category_root_mismatch", "income"),
            ("category_root_mismatch", "income"),
        ),
    )
    for failure, expected in failures:

        def reject(
            *_args: object,
            failure: worker.ReplayFailure = failure,
            **_kwargs: object,
        ) -> None:
            raise failure

        monkeypatch.setattr(worker, "execute", reject)
        with pytest.raises(worker.ReplayMismatchError) as caught:
            worker.verify(
                financial_root,
                baseline_digest=result["descriptor_digest"],
                execution=release_execution,
            )
        assert caught.value.__cause__ is failure
        assert (caught.value.code, caught.value.location) == expected
        assert "recorded_environment" in str(caught.value)
        assert "observed_environment" in str(caught.value)
        assert retained == {
            path.relative_to(financial_root): path.read_bytes()
            for path in financial_root.rglob("*")
            if path.is_file()
        }
