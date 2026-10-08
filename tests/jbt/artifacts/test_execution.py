from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from jsonschema import ValidationError

from jbt.artifacts.execution import (
    ExecutionFacts,
    InputEdge,
    Producer,
    StageCacheError,
    execution_fingerprint,
    read_stage_cache,
    stage_envelope,
)
from jbt.artifacts.integrity import ArtifactIntegrityError, byte_digest, decode_document
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def facts() -> ExecutionFacts:
    return ExecutionFacts(Producer("producer", "1", "a" * 64), "b" * 64)


def _write_cache(root: Path, facts: ExecutionFacts, payload: bytes) -> str:
    envelope = stage_envelope(
        stage="extract",
        schema_id="extraction_schema",
        configuration_digest="c" * 64,
        inputs=(InputEdge("object", 0, "blob", "d" * 64),),
        facts=facts,
        path="payload.json",
        byte_digest_value=byte_digest(payload),
        logical_digest=byte_digest(payload),
    )
    (root / "payload.json").write_bytes(payload)
    document = encode_json(envelope, integer_strings=False)
    (root / "envelope.json").write_bytes(document)
    return byte_digest(document)


def _read_cache(root: Path, facts: ExecutionFacts, digest: str) -> bytes:
    return read_stage_cache(
        root,
        envelope_digest=digest,
        stage="extract",
        schema_id="extraction_schema",
        configuration_digest="c" * 64,
        inputs=(InputEdge("object", 0, "blob", "d" * 64),),
        facts=facts,
        logical_digest=byte_digest,
        maximum_bytes=10000,
    )


def test_verified_cache_and_corruption(tmp_path: Path, facts: ExecutionFacts) -> None:
    digest = _write_cache(tmp_path, facts, b"source")
    assert _read_cache(tmp_path, facts, digest) == b"source"
    (tmp_path / "payload.json").write_bytes(b"damage")
    with pytest.raises(ArtifactIntegrityError, match="artifact digest"):
        _read_cache(tmp_path, facts, digest)


def test_semantic_verifier_and_producer_claims(
    tmp_path: Path, facts: ExecutionFacts
) -> None:
    digest = _write_cache(tmp_path, facts, b"source")
    with pytest.raises(StageCacheError, match="logical digest mismatch"):
        read_stage_cache(
            tmp_path,
            envelope_digest=digest,
            stage="extract",
            schema_id="extraction_schema",
            configuration_digest="c" * 64,
            inputs=(InputEdge("object", 0, "blob", "d" * 64),),
            facts=facts,
            logical_digest=lambda _: "0" * 64,
            maximum_bytes=10000,
        )
    with pytest.raises(StageCacheError, match="execution facts mismatch"):
        _read_cache(
            tmp_path,
            replace(facts, producer=replace(facts.producer, version="2")),
            digest,
        )


def test_cached_envelope_rejects_unknown_version(
    tmp_path: Path, facts: ExecutionFacts
) -> None:
    _write_cache(tmp_path, facts, b"source")
    document = decode_document(
        (tmp_path / "envelope.json").read_bytes(), "envelope.json"
    )
    document["schema_version"] = 2
    payload = encode_json(document, integer_strings=False)
    (tmp_path / "envelope.json").write_bytes(payload)
    with pytest.raises(StageCacheError, match="invalid cached stage envelope"):
        _read_cache(tmp_path, facts, byte_digest(payload))


def test_cached_envelope_rejects_old_environment_claim(
    tmp_path: Path, facts: ExecutionFacts
) -> None:
    _write_cache(tmp_path, facts, b"source")
    document = decode_document(
        (tmp_path / "envelope.json").read_bytes(), "envelope.json"
    )
    document["environment_digest"] = document.pop("execution_digest")
    payload = encode_json(document, integer_strings=False)
    (tmp_path / "envelope.json").write_bytes(payload)
    with pytest.raises(StageCacheError, match="invalid cached stage envelope"):
        _read_cache(tmp_path, facts, byte_digest(payload))


def test_observed_execution_change_invalidates_cache_independently_of_producer(
    tmp_path: Path, facts: ExecutionFacts
) -> None:
    digest = _write_cache(tmp_path, facts, b"source")
    changed = replace(facts, execution_digest="e" * 64)
    assert changed.producer == facts.producer
    with pytest.raises(StageCacheError, match="execution facts mismatch"):
        _read_cache(tmp_path, changed, digest)


def test_invalid_expected_execution_is_not_a_corrupt_cache(
    tmp_path: Path, facts: ExecutionFacts
) -> None:
    with pytest.raises(ArtifactIntegrityError, match="artifact digest"):
        read_stage_cache(
            tmp_path,
            envelope_digest="a" * 64,
            stage="extract",
            schema_id="extraction_schema",
            configuration_digest="invalid",
            inputs=(),
            facts=facts,
            logical_digest=byte_digest,
            maximum_bytes=10000,
        )
    with pytest.raises(ValidationError, match="not one of"):
        read_stage_cache(
            tmp_path,
            envelope_digest="a" * 64,
            stage="unknown",
            schema_id="extraction_schema",
            configuration_digest="c" * 64,
            inputs=(),
            facts=facts,
            logical_digest=byte_digest,
            maximum_bytes=10000,
        )


def test_identity_keeps_roles_order_multiplicity(facts: ExecutionFacts) -> None:
    def fingerprint(edges: tuple[InputEdge, ...]) -> str:
        return execution_fingerprint(
            stage="extract",
            schema_id="extraction_schema",
            configuration_digest="c" * 64,
            inputs=edges,
            facts=facts,
        )

    first = InputEdge("left", 0, "blob", "d" * 64)
    second = InputEdge("right", 0, "blob", "d" * 64)
    assert (
        len(
            {
                fingerprint((first, second)),
                fingerprint((second, first)),
                fingerprint((first, replace(first, ordinal=1))),
                fingerprint((first,)),
            }
        )
        == 4
    )
    with pytest.raises(ValueError, match="duplicate execution input ordinal"):
        fingerprint((first, first))


@pytest.mark.parametrize(
    ("kind", "path", "message"),
    [
        ("unknown", "payload.json", "not one of"),
        ("ledger", "envelope.json", "cannot overwrite"),
    ],
)
def test_envelope_rejects_unknown_kind_or_invalid_payload(
    facts: ExecutionFacts, kind: str, path: str, message: str
) -> None:
    with pytest.raises((ValidationError, ValueError), match=message):
        stage_envelope(
            stage=kind,
            schema_id="extraction_schema",
            configuration_digest="c" * 64,
            inputs=(),
            facts=facts,
            path=path,
            byte_digest_value="d" * 64,
            logical_digest="e" * 64,
        )
