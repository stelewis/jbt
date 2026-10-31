"""Canonical retained-anchor and semantic child identities."""

from dataclasses import dataclass
from datetime import date
from hashlib import sha256

from jbt.domain.canonical import CanonicalValue, JsonValue, digest_record, encode_json
from jbt.domain.errors import IdentityError
from jbt.domain.ids import (
    EntityId,
    RecordId,
    RecordKind,
    SemanticKey,
    validate_digest,
    validate_key,
)


@dataclass(frozen=True, slots=True)
class AcquiredAnchor:
    """A source occurrence within a retained, format-qualified context."""

    blob_digest: str
    namespace: SemanticKey
    section: SemanticKey
    locator: SemanticKey

    def __post_init__(self) -> None:
        """Require retained scope and an explicitly format-qualified locator."""
        validate_digest(self.blob_digest, "anchor.blob_digest")
        if not all(
            isinstance(value, SemanticKey)
            for value in (self.namespace, self.section, self.locator)
        ):
            raise IdentityError(
                constraint="typed_anchor_keys_required", location="anchor"
            )
        qualifier, separator, position = self.locator.value.partition(":")
        if not qualifier or not separator or not position:
            raise IdentityError(
                constraint="format_qualified_locator_required",
                location="anchor.locator",
            )


def source_record_digest(anchor: AcquiredAnchor) -> str:
    """Name retained source evidence from its complete acquired anchor."""
    if not isinstance(anchor, AcquiredAnchor):
        raise IdentityError(
            constraint="acquired_anchor_required", location="source_record"
        )
    return digest_record(
        "source-record",
        (
            ("blob_digest", anchor.blob_digest),
            ("namespace", anchor.namespace.value),
            ("section", anchor.section.value),
            ("locator", anchor.locator.value),
        ),
    )


@dataclass(frozen=True, slots=True)
class AuthoredAnchor:
    """A stable authored-record key, never a revision digest."""

    record_key: SemanticKey

    def __post_init__(self) -> None:
        """Require an explicit stable authored key."""
        if not isinstance(self.record_key, SemanticKey):
            raise IdentityError(constraint="authored_key_required", location="anchor")


@dataclass(frozen=True, slots=True)
class SourceKeyAnchor:
    """An economic key family, independent of its source occurrences."""

    namespace: SemanticKey
    section: SemanticKey
    key_domain: SemanticKey
    source_key: SemanticKey

    def __post_init__(self) -> None:
        """Require explicit typed keys for every identity dimension."""
        if not all(
            isinstance(value, SemanticKey)
            for value in (
                self.namespace,
                self.section,
                self.key_domain,
                self.source_key,
            )
        ):
            raise IdentityError(
                constraint="typed_anchor_keys_required", location="anchor"
            )


type Anchor = AcquiredAnchor | AuthoredAnchor | SourceKeyAnchor


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One economic component of an occurrence or economic key family."""

    anchor: Anchor
    event_key: SemanticKey

    def __post_init__(self) -> None:
        """Keep anchor and component identity separate."""
        if not isinstance(
            self.anchor, AcquiredAnchor | AuthoredAnchor | SourceKeyAnchor
        ) or not isinstance(self.event_key, SemanticKey):
            raise IdentityError(
                constraint="typed_occurrence_required", location="occurrence"
            )


def _anchor_value(anchor: Anchor) -> tuple[CanonicalValue, ...]:
    if isinstance(anchor, AcquiredAnchor):
        return (
            "acquired",
            anchor.blob_digest,
            anchor.namespace.value,
            anchor.section.value,
            anchor.locator.value,
        )
    if isinstance(anchor, AuthoredAnchor):
        return ("authored", anchor.record_key.value)
    return (
        "source_key",
        anchor.namespace.value,
        anchor.section.value,
        anchor.key_domain.value,
        anchor.source_key.value,
    )


def event_id(entity: EntityId, canonical: Occurrence) -> RecordId:
    """Derive identity from one retained anchor, not mutable event facts."""
    digest = digest_record(
        "economic-identity",
        (
            ("entity", entity.value),
            ("kind", RecordKind.EVENT.value),
            ("anchor", _anchor_value(canonical.anchor)),
            ("event_key", canonical.event_key.value),
        ),
    )
    return RecordId(entity, RecordKind.EVENT, digest)


def declaration_id(entity: EntityId, kind: RecordKind, key: SemanticKey) -> RecordId:
    """Derive a stable declaration identity without its revision payload."""
    allowed = {
        RecordKind.ACCOUNT,
        RecordKind.CATEGORY,
        RecordKind.COMMODITY,
        RecordKind.COMMODITY_SYMBOL,
        RecordKind.POSITION,
        RecordKind.POOL,
        RecordKind.COUNTERPARTY,
        RecordKind.DECLARATION,
        RecordKind.ASSERTION_SCOPE,
        RecordKind.COVERAGE_BASIS,
    }
    if kind not in allowed:
        raise IdentityError(constraint="declaration_kind_required", location="identity")
    digest = digest_record(
        "declaration-identity",
        (("entity", entity.value), ("kind", kind.value), ("key", key.value)),
    )
    return RecordId(entity, kind, digest)


def child_id(parent: RecordId, kind: RecordKind, key: SemanticKey) -> RecordId:
    """Derive semantic children, excluding sequence and matched-lot order."""
    allowed = {
        RecordKind.EVENT: {
            RecordKind.LEG,
            RecordKind.STEP,
            RecordKind.ACTION,
            RecordKind.CHANGE,
        },
        RecordKind.LEG: {RecordKind.LOT, RecordKind.ORIGIN, RecordKind.LEG},
        RecordKind.ACTION: {RecordKind.EFFECT},
        RecordKind.EFFECT: {RecordKind.ALLOCATION, RecordKind.CHANGE},
        RecordKind.STEP: {RecordKind.ALLOCATION, RecordKind.CHANGE},
        RecordKind.ALLOCATION: {RecordKind.ALLOCATION, RecordKind.CHANGE},
    }
    if kind not in allowed.get(parent.kind, set()):
        raise IdentityError(
            constraint="invalid_semantic_child_kind", location="identity"
        )
    digest = digest_record(
        "semantic-child",
        (
            ("entity", parent.entity.value),
            ("parent_kind", parent.kind.value),
            ("parent", parent.value),
            ("kind", kind.value),
            ("key", key.value),
        ),
    )
    return RecordId(parent.entity, kind, digest)


def successor_lot_id(
    root: RecordId, determining_event: RecordId, output_key: SemanticKey
) -> RecordId:
    """Name a successor branch by retained lineage and determining output."""
    root.require(entity=root.entity, kind=RecordKind.LOT)
    determining_event.require(entity=root.entity, kind=RecordKind.EVENT)
    digest = digest_record(
        "successor-lot",
        (
            ("entity", root.entity.value),
            ("root", root.value),
            ("event", determining_event.value),
            ("output", output_key.value),
        ),
    )
    return RecordId(root.entity, RecordKind.LOT, digest)


def revision_id(
    entity: EntityId,
    key: SemanticKey,
    *,
    effective_from: date | None,
    effective_to: date | None,
    payload: dict[str, JsonValue],
) -> RecordId:
    """Hash revision meaning; a null interval endpoint is unbounded."""
    if any(
        endpoint is not None and type(endpoint) is not date
        for endpoint in (effective_from, effective_to)
    ) or (
        effective_from is not None
        and effective_to is not None
        and effective_to <= effective_from
    ):
        raise IdentityError(
            constraint="revision_effective_interval", location="declaration_revision"
        )
    forbidden = {"revision_id", "declaration_id", "digest"}
    if forbidden.intersection(payload):
        raise IdentityError(
            constraint="self_referential_revision", location="declaration_revision"
        )
    kind = payload.get("kind")
    version = payload.get("schema_version")
    if not isinstance(kind, str) or type(version) is not int or version != 1:
        raise IdentityError(
            constraint="revision_kind_and_version_required",
            location="declaration_revision",
        )
    validate_key(kind, "declaration_revision.kind")
    projection = {
        "entity_id": entity.value,
        "declaration_key": key.value,
        "valid_from": effective_from.isoformat()
        if effective_from is not None
        else None,
        "valid_to": effective_to.isoformat() if effective_to is not None else None,
        "kind": kind,
        "schema_version": version,
        "payload": payload,
    }
    digest = sha256(encode_json(projection, integer_strings=False)).hexdigest()
    return RecordId(entity, RecordKind.DECLARATION_REVISION, digest)
