"""Versioned envelopes for deterministic derived artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jbt.core.serde import CanonicalValue, canonical_bytes, sha256_digest

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ArtifactEnvelope:
    """Metadata and payload shared by every derived artifact."""

    kind: str
    schema_version: int
    producer: str
    config_digest: str
    inputs: tuple[str, ...]
    payload: CanonicalValue

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("kind must not be empty")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if not self.producer:
            raise ValueError("producer must not be empty")
        _validate_digest(self.config_digest, "config_digest")
        for input_digest in self.inputs:
            _validate_digest(input_digest, "input digest")
        if tuple(sorted(self.inputs)) != self.inputs:
            raise ValueError("inputs must be sorted")
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("inputs must not contain duplicates")

    def to_bytes(self) -> bytes:
        """Serialize this envelope canonically."""

        return canonical_bytes(
            {
                "config_digest": self.config_digest,
                "inputs": self.inputs,
                "kind": self.kind,
                "payload": self.payload,
                "producer": self.producer,
                "schema_version": self.schema_version,
            }
        )

    def digest(self) -> str:
        """Return the content digest of the canonical envelope."""

        return sha256_digest(self.to_bytes())


def _validate_digest(value: str, field: str) -> None:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a qualified lowercase SHA-256 digest")