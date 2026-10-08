"""Explicit stage execution identity and verified local payload reuse."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jsonschema import ValidationError

from jbt.artifacts.integrity import (
    byte_digest,
    decode_document,
    read_payload,
    require_digest,
    require_filename,
)
from jbt.contracts.schemas import envelope_schema, validate_document
from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass(frozen=True)
class Producer:
    """Identity of the verified producer actually used for execution."""

    name: str
    version: str
    content_digest: str


@dataclass(frozen=True)
class ExecutionFacts:
    """Independent producer identity and observed execution record digest."""

    producer: Producer
    execution_digest: str


@dataclass(frozen=True)
class InputEdge:
    """One named edge; repeated references retain distinct ordinals."""

    role: str
    ordinal: int
    kind: str
    digest: str


class StageCacheError(ValueError):
    """Cached execution claims or logical payload content are inconsistent."""


def execution_fingerprint(
    *,
    stage: str,
    schema_id: str,
    configuration_digest: str,
    inputs: tuple[InputEdge, ...],
    facts: ExecutionFacts,
) -> str:
    """Hash caller-captured facts and ordered edges, never ambient state."""
    document = stage_envelope(
        stage=stage,
        schema_id=schema_id,
        configuration_digest=configuration_digest,
        inputs=inputs,
        facts=facts,
        path="payload.json",
        byte_digest_value="0" * 64,
        logical_digest="0" * 64,
    )
    return byte_digest(
        encode_json(
            {key: value for key, value in document.items() if key != "payload"},
            integer_strings=True,
        )
    )


def stage_envelope(  # noqa: PLR0913 - explicit stage identity and payload fields
    *,
    stage: str,
    schema_id: str,
    configuration_digest: str,
    inputs: tuple[InputEdge, ...],
    facts: ExecutionFacts,
    path: str,
    byte_digest_value: str,
    logical_digest: str,
) -> dict[str, object]:
    """Construct a closed envelope from verified execution claims."""
    require_digest(facts.execution_digest)
    require_digest(facts.producer.content_digest)
    require_digest(configuration_digest)
    require_digest(byte_digest_value)
    require_digest(logical_digest)
    require_filename(path)
    for edge in inputs:
        require_digest(edge.digest)
    if len({(edge.role, edge.ordinal) for edge in inputs}) != len(inputs):
        msg = "duplicate execution input ordinal"
        raise ValueError(msg)
    if path == "envelope.json":
        msg = "stage payload cannot overwrite its envelope"
        raise ValueError(msg)
    document: dict[str, object] = {
        "schema_version": 1,
        "kind": stage,
        "producer": {
            "name": facts.producer.name,
            "version": facts.producer.version,
            "content_digest": facts.producer.content_digest,
        },
        "execution_digest": facts.execution_digest,
        "configuration_digest": configuration_digest,
        "inputs": [
            {
                "role": edge.role,
                "ordinal": edge.ordinal,
                "kind": edge.kind,
                "digest": edge.digest,
            }
            for edge in inputs
        ],
        "payload": {
            "schema_id": schema_id,
            "path": path,
            "byte_digest": byte_digest_value,
            "logical_digest": logical_digest,
        },
    }
    validate_document(document, envelope_schema())
    return document


def read_stage_cache(  # noqa: PLR0913 - expected identity and verifier are mandatory
    root: Path,
    *,
    envelope_digest: str,
    stage: str,
    schema_id: str,
    configuration_digest: str,
    inputs: tuple[InputEdge, ...],
    facts: ExecutionFacts,
    logical_digest: Callable[[bytes], str],
    maximum_bytes: int,
) -> bytes:
    """Reject corrupt cache bytes or semantic drift, never treat as a miss."""
    expected = stage_envelope(
        stage=stage,
        schema_id=schema_id,
        configuration_digest=configuration_digest,
        inputs=inputs,
        facts=facts,
        path="payload",
        byte_digest_value="0" * 64,
        logical_digest="0" * 64,
    )
    envelope_bytes = read_payload(
        root, "envelope.json", envelope_digest, maximum_bytes=maximum_bytes
    )
    document = decode_document(envelope_bytes, "envelope.json")
    if envelope_bytes != encode_json(document, integer_strings=False):
        msg = "noncanonical cached stage envelope"
        raise StageCacheError(msg)
    try:
        validate_document(document, envelope_schema())
    except ValidationError as error:
        msg = "invalid cached stage envelope"
        raise StageCacheError(msg) from error
    payload = document["payload"]
    if not isinstance(payload, dict):
        msg = "invalid cached stage payload"
        raise StageCacheError(msg)
    if (
        {key: value for key, value in document.items() if key != "payload"}
        != {key: value for key, value in expected.items() if key != "payload"}
        or payload["schema_id"] != schema_id
        or payload["path"] == "envelope.json"
    ):
        msg = "cached stage execution facts mismatch"
        raise StageCacheError(msg)
    content = read_payload(
        root, payload["path"], payload["byte_digest"], maximum_bytes=maximum_bytes
    )
    if logical_digest(content) != payload["logical_digest"]:
        msg = "cached stage logical digest mismatch"
        raise StageCacheError(msg)
    return content
