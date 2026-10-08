import logging
import os
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.execution import ExecutionFacts, InputEdge, Producer
from jbt.artifacts.integrity import ArtifactIntegrityError, byte_digest
from jbt.contracts.primitives import ContractError
from jbt.runtime import stages
from jbt.runtime.stages import Stage, StageRunner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def facts() -> ExecutionFacts:
    return ExecutionFacts(Producer("jbt", "1", "a" * 64), "b" * 64)


@pytest.fixture
def stage() -> Stage:
    return Stage(
        "extract",
        "extraction-v1",
        "c" * 64,
        (InputEdge("source", 0, "source", "d" * 64),),
    )


def test_warm_hit_does_not_compute(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
) -> None:
    runner = StageRunner(tmp_path / "cache", facts)
    cold = runner.run(stage, lambda: b"original")

    def unexpected() -> bytes:
        pytest.fail("warm stage recomputed")

    warm = runner.run(stage, unexpected)
    assert warm == cold
    assert runner.executed == ["extract"]
    assert runner.reused == ["extract"]
    assert (
        tmp_path / "cache" / cold.fingerprint / "payload"
    ).read_bytes() == cold.payload
    assert (
        tmp_path / "cache" / cold.fingerprint / "envelope.json"
    ).read_bytes() == cold.envelope


def test_identity_changes_miss(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
) -> None:
    runner = StageRunner(tmp_path / "cache", facts)
    first = stage.inputs[0]
    other = InputEdge("source", 1, "source", first.digest)
    variations = (
        stage,
        replace(stage, configuration_digest="e" * 64),
        replace(stage, inputs=(replace(first, role="other"),)),
        replace(stage, inputs=(first, other)),
        replace(stage, inputs=(other, first)),
    )
    results = [runner.run(item, lambda: b"same") for item in variations]
    assert len({result.fingerprint for result in results}) == len(variations)
    assert runner.executed == ["extract"] * len(variations)
    assert not runner.reused
    changed_facts = replace(facts, execution_digest="f" * 64)
    assert (
        StageRunner(tmp_path / "cache", changed_facts)
        .run(stage, lambda: b"new")
        .fingerprint
        != results[0].fingerprint
    )


@pytest.mark.parametrize(
    "case",
    [
        ("envelope_missing", "artifact missing: envelope.json"),
        ("envelope_truncated", "artifact json: envelope.json"),
        ("envelope_corrupt", "artifact json: envelope.json"),
        ("envelope_mismatched", "execution facts mismatch"),
        ("envelope_unknown_version", "invalid cached stage envelope"),
        ("payload_missing", "artifact missing: payload"),
        ("payload_truncated", "artifact digest: payload"),
        ("payload_corrupt", "artifact digest: payload"),
    ],
)
def test_corrupt_cache_recomputes_with_named_cause(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, str],
) -> None:
    damaged, cause = case
    runner = StageRunner(tmp_path / "cache", facts)
    original = runner.run(stage, lambda: b"original")
    directory = tmp_path / "cache" / original.fingerprint
    envelope = directory / "envelope.json"
    payload = directory / "payload"
    if damaged == "envelope_missing":
        envelope.unlink()
    elif damaged == "envelope_truncated":
        envelope.write_bytes(original.envelope[:20])
    elif damaged == "envelope_corrupt":
        envelope.write_bytes(b"{no-json")
    elif damaged == "envelope_mismatched":
        envelope.write_bytes(original.envelope.replace(b'"extract"', b'"model"'))
    elif damaged == "envelope_unknown_version":
        envelope.write_bytes(
            original.envelope.replace(b'"schema_version":1', b'"schema_version":2')
        )
    elif damaged == "payload_missing":
        payload.unlink()
    elif damaged == "payload_truncated":
        payload.write_bytes(b"orig")
    else:
        payload.write_bytes(b"tampered")
    with caplog.at_level(logging.WARNING, logger="jbt.runtime.stages"):
        recovered = runner.run(stage, lambda: b"replacement")
    assert recovered.payload == b"replacement"
    assert runner.executed == ["extract", "extract"]
    assert not runner.reused
    assert cause in caplog.text
    assert (
        StageRunner(tmp_path / "cache", facts).run(
            stage, lambda: pytest.fail("repaired cache missed")
        )
        == recovered
    )


def test_oversized_envelope_recomputes_without_full_read(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = StageRunner(tmp_path / "cache", facts)
    original = runner.run(stage, lambda: b"original")
    envelope = tmp_path / "cache" / original.fingerprint / "envelope.json"
    with envelope.open("wb") as stream:
        stream.truncate(1024)
    monkeypatch.setattr("jbt.runtime.stages.MAXIMUM_ARTIFACT_BYTES", 512)
    with caplog.at_level(logging.WARNING, logger="jbt.runtime.stages"):
        assert runner.run(stage, lambda: b"new").payload == b"new"
    assert "artifact size: envelope.json" in caplog.text


def test_failed_compute_never_publishes_partial_cache(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
) -> None:
    cache = tmp_path / "cache"
    runner = StageRunner(cache, facts)

    def fail() -> bytes:
        msg = "compute failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="compute failed"):
        runner.run(stage, fail)
    assert not cache.exists()
    assert not runner.executed
    assert not runner.reused


def test_oversized_compute_never_publishes(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr("jbt.runtime.stages.MAXIMUM_ARTIFACT_BYTES", 3)
    with pytest.raises(ContractError, match="stage_size_limit"):
        StageRunner(cache, facts).run(stage, lambda: b"large")
    assert not cache.exists()


@pytest.mark.parametrize("target", ["root", "stage", "envelope", "payload"])
def test_symlink_rejected(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    target: str,
) -> None:
    cache = tmp_path / "cache"
    runner = StageRunner(cache, facts)
    if target == "root":
        outside = tmp_path / "outside"
        outside.mkdir()
        cache.symlink_to(outside, target_is_directory=True)
    else:
        result = runner.run(stage, lambda: b"original")
        directory = cache / result.fingerprint
        if target == "stage":
            outside = tmp_path / "outside"
            directory.rename(outside)
            directory.symlink_to(outside, target_is_directory=True)
        else:
            file = directory / ("envelope.json" if target == "envelope" else "payload")
            file.unlink()
            file.symlink_to(tmp_path / "outside")
    with pytest.raises(
        (ContractError, ArtifactIntegrityError), match=r"symlink|unsafe_cache_path"
    ):
        runner.run(stage, lambda: pytest.fail("unsafe path computed"))


@pytest.mark.parametrize("special", ["directory", "fifo"])
def test_envelope_must_be_ordinary_file(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    special: str,
) -> None:
    runner = StageRunner(tmp_path / "cache", facts)
    result = runner.run(stage, lambda: b"original")
    envelope = tmp_path / "cache" / result.fingerprint / "envelope.json"
    envelope.unlink()
    if special == "directory":
        envelope.mkdir()
    else:
        os.mkfifo(envelope)
    with pytest.raises(ContractError, match="unsafe_cache_path"):
        runner.run(stage, lambda: pytest.fail("special file computed"))


def test_bypass_avoids_all_cache_io(
    tmp_path: Path,
    facts: ExecutionFacts,
    stage: Stage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("cache accessed in bypass")

    monkeypatch.setattr(stages, "_real_directory", unexpected)
    monkeypatch.setattr(stages, "_read_envelope", unexpected)
    monkeypatch.setattr(stages, "_write_cache", unexpected)
    runner = StageRunner(None, facts)
    result = runner.run(stage, lambda: b"fresh")
    assert result.payload == b"fresh"
    assert result.fingerprint
    assert byte_digest(result.envelope)
    assert runner.executed == ["extract"]
    assert runner.reused == []
    assert list(tmp_path.iterdir()) == []
