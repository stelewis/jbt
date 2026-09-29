"""Entity-qualified record identities and stable semantic keys."""

import re
from dataclasses import dataclass
from enum import StrEnum
from unicodedata import category, normalize

from jbt.domain.errors import IdentityError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def validate_key(value: str, location: str) -> None:
    """Require nonempty canonical Unicode without invisible control codes."""
    if (
        not isinstance(value, str)
        or not value
        or normalize("NFC", value) != value
        or any(category(character) == "Cc" for character in value)
    ):
        raise IdentityError(constraint="canonical_key_required", location=location)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise IdentityError(constraint="utf8_required", location=location) from error


def validate_digest(value: str, location: str) -> None:
    """Require a lowercase SHA-256 hexadecimal digest."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise IdentityError(constraint="sha256_digest_required", location=location)


@dataclass(frozen=True, slots=True)
class EntityId:
    """A stable entity declaration key, independent of display names."""

    value: str

    def __post_init__(self) -> None:
        """Validate the stable entity key."""
        validate_key(self.value, "entity_id")


@dataclass(frozen=True, slots=True)
class SemanticKey:
    """An authored semantic discriminator, never a serialization index."""

    value: str

    def __post_init__(self) -> None:
        """Validate the explicit discriminator."""
        validate_key(self.value, "semantic_key")


class RecordKind(StrEnum):
    """Identity namespaces for referenced domain records."""

    ACCOUNT = "account"
    CATEGORY = "category"
    COMMODITY = "commodity"
    COMMODITY_SYMBOL = "commodity_symbol"
    POSITION = "position"
    STATEMENT = "statement"
    EVENT = "event"
    LEG = "leg"
    STEP = "step"
    LOT = "lot"
    POOL = "pool"
    ORIGIN = "origin"
    EFFECT = "effect"
    ALLOCATION = "allocation"
    CHANGE = "change"
    ACTION = "action"
    COUNTERPARTY = "counterparty"
    SOURCE_RECORD = "source_record"
    DECLARATION = "declaration"
    DECLARATION_REVISION = "declaration_revision"
    ASSERTION = "assertion"
    ASSERTION_SCOPE = "assertion_scope"
    PRICE = "price"
    OBSERVATION = "observation"
    LINK = "link"
    CHECK = "check"
    FINDING = "finding"
    COVERAGE_BASIS = "coverage_basis"


@dataclass(frozen=True, slots=True)
class RecordId:
    """An immutable ID whose entity and record kind cannot be interchanged."""

    entity: EntityId
    kind: RecordKind
    value: str

    def __post_init__(self) -> None:
        """Validate all identity dimensions at construction."""
        if not isinstance(self.entity, EntityId) or not isinstance(
            self.kind, RecordKind
        ):
            raise IdentityError(
                constraint="typed_identity_required", location="record_id"
            )
        validate_digest(self.value, "record_id.value")

    def require(self, *, entity: EntityId, kind: RecordKind) -> None:
        """Reject cross-entity and wrong-kind references."""
        if self.entity != entity or self.kind != kind:
            raise IdentityError(
                constraint="reference_kind_or_entity", location="record_id"
            )
