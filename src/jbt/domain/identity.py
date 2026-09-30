"""Canonical retained-anchor identity and explicit membership transitions."""

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


@dataclass(frozen=True, slots=True)
class AuthoredAnchor:
    """A stable authored-record key, never a revision digest."""

    record_key: SemanticKey

    def __post_init__(self) -> None:
        """Require an explicit stable authored key."""
        if not isinstance(self.record_key, SemanticKey):
            raise IdentityError(constraint="authored_key_required", location="anchor")


type Anchor = AcquiredAnchor | AuthoredAnchor


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One economic component of an acquired or authored source record."""

    anchor: Anchor
    event_key: SemanticKey

    def __post_init__(self) -> None:
        """Keep source occurrence and component identity separate."""
        if not isinstance(
            self.anchor, AcquiredAnchor | AuthoredAnchor
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
    return ("authored", anchor.record_key.value)


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


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    """Reviewed, disjoint membership with one retained canonical component."""

    canonical: Occurrence
    members: tuple[Occurrence, ...]
    reviewed_discriminants: tuple[SemanticKey, ...]

    def __post_init__(self) -> None:
        """Require explicit unique members and reviewed correspondence."""
        if (
            not isinstance(self.members, tuple)
            or not self.members
            or any(not isinstance(member, Occurrence) for member in self.members)
            or len(set(self.members)) != len(self.members)
            or self.canonical not in self.members
        ):
            raise IdentityError(
                constraint="canonical_unique_members_required",
                location="identity_decision",
            )
        if (
            not isinstance(self.reviewed_discriminants, tuple)
            or not self.reviewed_discriminants
            or any(
                not isinstance(key, SemanticKey) for key in self.reviewed_discriminants
            )
            or len(set(self.reviewed_discriminants)) != len(self.reviewed_discriminants)
        ):
            raise IdentityError(
                constraint="reviewed_discriminants_required",
                location="identity_decision",
            )


@dataclass(frozen=True, slots=True)
class Membership:
    """One admitted occurrence/component's resolved canonical event."""

    occurrence: Occurrence
    event: RecordId

    def __post_init__(self) -> None:
        """Retain an event-qualified identity for a typed occurrence."""
        if not isinstance(self.occurrence, Occurrence) or not isinstance(
            self.event, RecordId
        ):
            raise IdentityError(
                constraint="typed_membership_required", location="membership"
            )
        self.event.require(entity=self.event.entity, kind=RecordKind.EVENT)


def validate_membership(
    entity: EntityId,
    retained: tuple[Occurrence, ...],
    decisions: tuple[IdentityDecision, ...],
    *,
    required_correspondences: tuple[tuple[Occurrence, ...], ...],
) -> tuple[Membership, ...]:
    """Resolve explicit decisions and singleton anchors without matching.

    Correspondences are externally evidenced overlap obligations. A pure
    resolver cannot discover whether two otherwise distinct records match.
    Repeated acquisition of one retained occurrence does not multiply it.
    """
    available = set(retained)
    resolved: dict[Occurrence, RecordId] = {}
    for decision in decisions:
        if not set(decision.members) <= available:
            raise IdentityError(
                constraint="missing_retained_anchor", location="identity_decision"
            )
        identifier = event_id(entity, decision.canonical)
        for member in decision.members:
            if member in resolved:
                raise IdentityError(
                    constraint="contradictory_membership", location="identity_decision"
                )
            resolved[member] = identifier
    for group in required_correspondences:
        if len(set(group)) <= 1 or not set(group) <= available:
            raise IdentityError(
                constraint="invalid_correspondence", location="identity_decision"
            )
        assigned = {resolved.get(member) for member in group}
        if None in assigned or len(assigned) != 1:
            raise IdentityError(
                constraint="explicit_membership_required", location="identity_decision"
            )
    for occurrence in available:
        resolved.setdefault(occurrence, event_id(entity, occurrence))
    return tuple(
        Membership(occurrence, identifier)
        for occurrence, identifier in sorted(
            resolved.items(),
            key=lambda item: (
                item[1].value,
                event_id(entity, item[0]).value,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class Retirement:
    """An informational retired-to-survivor reference, not a redirect."""

    retired: RecordId
    survivor: RecordId

    def __post_init__(self) -> None:
        """Keep retirement within one entity and one record namespace."""
        self.survivor.require(entity=self.retired.entity, kind=self.retired.kind)
        if self.retired == self.survivor:
            raise IdentityError(constraint="self_retirement", location="retirement")


def validate_retirements(
    retirements: tuple[Retirement, ...], *, history: frozenset[RecordId]
) -> None:
    """Reject missing history, ambiguous successors, and retirement cycles."""
    redirects: dict[RecordId, RecordId] = {}
    for item in retirements:
        if item.retired not in history or item.survivor not in history:
            raise IdentityError(
                constraint="missing_retirement_history", location="retirement"
            )
        if item.retired in redirects:
            raise IdentityError(
                constraint="multiple_retirement_survivors", location="retirement"
            )
        redirects[item.retired] = item.survivor
    for identifier in redirects:
        visited: set[RecordId] = set()
        current = identifier
        while current in redirects:
            if current in visited:
                raise IdentityError(
                    constraint="retirement_cycle", location="retirement"
                )
            visited.add(current)
            current = redirects[current]


@dataclass(frozen=True, slots=True)
class IdentityReview:
    """The affected dependency closure and explicitly reviewed targets."""

    affected_targets: frozenset[RecordId]
    reviewed_targets: frozenset[RecordId]

    def validate(self) -> None:
        """Fail closed when any affected determination lacks review."""
        if not self.affected_targets <= self.reviewed_targets:
            raise IdentityError(
                constraint="unreviewed_affected_targets", location="identity_transition"
            )


def validate_transition(
    before: tuple[Membership, ...],
    after: tuple[Membership, ...],
    retirements: tuple[Retirement, ...],
    *,
    prior_retirements: tuple[Retirement, ...],
    review: IdentityReview,
) -> None:
    """Require explicit survivor/retirement and complete downstream review.

    Inputs are membership results from validated retained decisions. The
    caller supplies the affected dependency closure, not a guessed subset.
    """
    old = {member.occurrence: member.event for member in before}
    new = {member.occurrence: member.event for member in after}
    if len(old) != len(before) or len(new) != len(after) or old.keys() != new.keys():
        raise IdentityError(
            constraint="transition_membership_partition", location="identity_transition"
        )
    review.validate()
    old_ids, new_ids = set(old.values()), set(new.values())
    if {item.retired for item in prior_retirements} & new_ids:
        raise IdentityError(
            constraint="retired_identity_reuse", location="identity_transition"
        )
    entities = {identifier.entity for identifier in old_ids | new_ids}
    if len(entities) > 1:
        raise IdentityError(
            constraint="cross_entity_transition", location="identity_transition"
        )
    validate_retirements(retirements, history=frozenset(old_ids | new_ids))
    retired = {item.retired: item.survivor for item in retirements}
    if set(retired) != old_ids - new_ids:
        raise IdentityError(
            constraint="explicit_retirement_required", location="identity_transition"
        )
    for previous, survivor in retired.items():
        destinations = {
            new[occurrence] for occurrence in old if old[occurrence] == previous
        }
        if survivor not in destinations or survivor not in new_ids:
            raise IdentityError(
                constraint="unrelated_retirement_survivor",
                location="identity_transition",
            )


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
