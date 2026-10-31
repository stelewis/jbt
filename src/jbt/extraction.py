"""The source adapter contract and its validated wire boundary."""

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from jbt.contracts.primitives import ContractError, require
from jbt.contracts.publication import validate_extraction
from jbt.domain.canonical import encode_json
from jbt.domain.ids import SemanticKey, validate_digest

CONTRACT_VERSION = 1


class ExtractionError(ValueError):
    """A safe, located rejection at a source adapter boundary."""

    def __init__(self, code: str, location: str) -> None:
        """Retain structural diagnostics, never source values."""
        self.code = code
        self.location = location
        super().__init__(f"{code}: {location}")


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """Caller-owned identity of the retained bytes and selected interpreter."""

    source_scope_id: str
    source_blob_digest: str
    importer_id: str

    def __post_init__(self) -> None:
        """Reject malformed namespace, blob and importer identities."""
        SemanticKey(self.source_scope_id)
        validate_digest(self.source_blob_digest, "extraction.source_blob_digest")
        SemanticKey(self.importer_id)


@runtime_checkable
class Importer(Protocol):
    """A selected adapter declares bounded input and a versioned profile."""

    contract_version: int
    profile: str
    profile_version: int
    maximum_bytes: int

    def extract(self, data: bytes, context: ExtractionContext) -> dict:
        """Return observations, without financial interpretation."""
        ...


def validate(value: dict, context: ExtractionContext) -> None:
    """Validate the closed wire shape and the caller's exact source binding."""
    validate_extraction(value)
    require(
        all(
            value[field] == getattr(context, field)
            for field in ("source_scope_id", "source_blob_digest", "importer_id")
        ),
        "extraction_context_mismatch",
        "extraction",
    )


def encode(value: dict, context: ExtractionContext) -> bytes:
    """Reject invalid fresh adapter output before serialization or caching."""
    validate(value, context)
    return encode_json(value, integer_strings=False)


def extract(importer: Importer, data: bytes, context: ExtractionContext) -> bytes:
    """Locate adapter failures and reject invalid output before caching."""
    require(type(data) is bytes, "source_bytes_required", context.importer_id)
    require(
        len(data) <= importer.maximum_bytes, "source_size_limit", context.importer_id
    )
    try:
        value = importer.extract(data, context)
    except ExtractionError, ContractError:
        raise
    except ValueError, TypeError, OverflowError:
        code = "importer_extraction"
        raise ExtractionError(code, context.importer_id) from None
    return encode(value, context)


def decode(payload: bytes, context: ExtractionContext) -> dict:
    """Reject invalid cached observations as well as malformed wire encoding."""
    try:
        value = json.loads(payload.decode("utf-8"))
    except UnicodeError, ValueError, RecursionError:
        code = "extraction_encoding"
        raise ContractError(code, "extraction") from None
    validate(value, context)
    require(
        encode_json(value, integer_strings=False) == payload,
        "extraction_canonical",
        "extraction",
    )
    return value
