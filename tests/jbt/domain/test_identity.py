from dataclasses import FrozenInstanceError, replace
from datetime import date
from hashlib import sha256
from typing import cast

import pytest

from jbt.domain.errors import IdentityError
from jbt.domain.identity import (
    AcquiredAnchor,
    AuthoredAnchor,
    Occurrence,
    SourceKeyAnchor,
    child_id,
    declaration_id,
    event_id,
    revision_id,
    source_record_digest,
    successor_lot_id,
)
from jbt.domain.ids import EntityId, RecordKind, SemanticKey


@pytest.mark.golden
def test_source_record_digest_has_format_independent_acquired_framing() -> None:
    literal = (
        b"JBT\x00l186:s13:source-recordi1:1l160:l83:s11:blob_digests64:"
        + b"a" * 64
        + b"l19:s9:namespaces4:feedl20:s7:sections7:account"
        b"l22:s7:locators9:csv:row:2"
    )
    anchor = AcquiredAnchor(
        "a" * 64,
        SemanticKey("feed"),
        SemanticKey("account"),
        SemanticKey("csv:row:2"),
    )
    assert source_record_digest(anchor) == sha256(literal).hexdigest()


@pytest.mark.parametrize("value", [None, "untyped", 1])
def test_source_record_digest_requires_acquired_anchor(value: object) -> None:
    with pytest.raises(IdentityError, match="acquired_anchor_required"):
        source_record_digest(cast("AcquiredAnchor", value))


@pytest.fixture
def source_key_anchor() -> SourceKeyAnchor:
    return SourceKeyAnchor(
        SemanticKey("feed"),
        SemanticKey("account"),
        SemanticKey("fitid"),
        SemanticKey("tx-1"),
    )


@pytest.mark.golden
def test_source_key_anchor_independently_framed_identity(
    source_key_anchor: SourceKeyAnchor,
) -> None:
    literal = (
        b"JBT\x00l164:s17:economic-identityi1:1l134:"
        b"l21:s6:entitys9:synthetic"
        b"l15:s4:kinds5:event"
        b"l59:s6:anchorl46:s10:source_keys4:feeds7:accounts5:fitids4:tx-1"
        b"l23:s9:event_keys8:purchase"
    )
    identifier = event_id(
        EntityId("synthetic"), Occurrence(source_key_anchor, SemanticKey("purchase"))
    )
    assert identifier.entity == EntityId("synthetic")
    assert identifier.kind is RecordKind.EVENT
    assert identifier.value == sha256(literal).hexdigest()


@pytest.mark.golden
def test_acquired_anchor_independently_framed_identity_is_unchanged() -> None:
    literal = (
        b"JBT\x00l228:s17:economic-identityi1:1l198:"
        b"l21:s6:entitys9:synthetic"
        b"l15:s4:kinds5:event"
        b"l122:s6:anchorl108:s8:acquireds64:"
        + (b"a" * 64)
        + b"s4:feeds7:accounts9:csv:row:2"
        b"l23:s9:event_keys8:purchase"
    )
    anchor = AcquiredAnchor(
        "a" * 64,
        SemanticKey("feed"),
        SemanticKey("account"),
        SemanticKey("csv:row:2"),
    )
    identifier = event_id(
        EntityId("synthetic"), Occurrence(anchor, SemanticKey("purchase"))
    )
    assert identifier.value == sha256(literal).hexdigest()


@pytest.mark.parametrize(
    "dimension", ["namespace", "section", "key_domain", "source_key"]
)
@pytest.mark.parametrize("value", ["untyped", None, 1])
def test_source_key_anchor_rejects_untyped_dimensions(
    source_key_anchor: SourceKeyAnchor, dimension: str, value: object
) -> None:
    with pytest.raises(IdentityError, match="typed_anchor_keys_required"):
        replace(source_key_anchor, **{dimension: cast("SemanticKey", value)})


@pytest.mark.parametrize("value", ["purchase", None, 1])
def test_source_key_occurrence_rejects_untyped_event_key(
    source_key_anchor: SourceKeyAnchor, value: object
) -> None:
    with pytest.raises(IdentityError, match="typed_occurrence_required"):
        Occurrence(source_key_anchor, cast("SemanticKey", value))


def test_source_key_anchor_is_frozen(source_key_anchor: SourceKeyAnchor) -> None:
    with pytest.raises(FrozenInstanceError):
        source_key_anchor.namespace = SemanticKey("changed")  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "dimension", ["namespace", "section", "key_domain", "source_key"]
)
def test_source_key_identity_changes_with_each_anchor_dimension(
    source_key_anchor: SourceKeyAnchor, dimension: str
) -> None:
    entity = EntityId("synthetic")
    event_key = SemanticKey("purchase")
    changed = replace(source_key_anchor, **{dimension: SemanticKey("different")})
    assert event_id(entity, Occurrence(changed, event_key)) != event_id(
        entity, Occurrence(source_key_anchor, event_key)
    )


def test_source_key_identity_changes_with_event_key(
    source_key_anchor: SourceKeyAnchor,
) -> None:
    entity = EntityId("synthetic")
    assert event_id(
        entity, Occurrence(source_key_anchor, SemanticKey("purchase"))
    ) != event_id(entity, Occurrence(source_key_anchor, SemanticKey("fee")))


@pytest.mark.parametrize(
    "excluded", ["amount", "importer", "order", "blob_digest", "locator"]
)
def test_source_key_anchor_excludes_facts_and_occurrence_context(
    source_key_anchor: SourceKeyAnchor, excluded: str
) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        replace(source_key_anchor, **{excluded: SemanticKey("not-an-anchor-key")})


@pytest.fixture
def acquired_occurrence() -> Occurrence:
    return Occurrence(
        AcquiredAnchor(
            "a" * 64,
            SemanticKey("synthetic-binding"),
            SemanticKey("account-section"),
            SemanticKey("csv:row:2"),
        ),
        SemanticKey("purchase"),
    )


def test_same_bytes_in_distinct_scopes_and_source_components_remain_distinct(
    acquired_occurrence: Occurrence,
) -> None:
    original = acquired_occurrence
    assert isinstance(original.anchor, AcquiredAnchor)
    alternate = Occurrence(
        AcquiredAnchor(
            original.anchor.blob_digest,
            SemanticKey("another-binding"),
            original.anchor.section,
            original.anchor.locator,
        ),
        original.event_key,
    )
    fee = Occurrence(original.anchor, SemanticKey("paid-fee"))
    entity = EntityId("synthetic")
    assert len({event_id(entity, item) for item in (original, alternate, fee)}) == 3


def test_identical_purchases_at_distinct_locators_remain_distinct(
    acquired_occurrence: Occurrence,
) -> None:
    first = acquired_occurrence
    assert isinstance(first.anchor, AcquiredAnchor)
    second = Occurrence(
        AcquiredAnchor(
            first.anchor.blob_digest,
            first.anchor.namespace,
            first.anchor.section,
            SemanticKey("csv:row:3"),
        ),
        first.event_key,
    )
    entity = EntityId("synthetic")
    assert event_id(entity, first) != event_id(entity, second)


def test_revision_changes_meaning_not_stable_declaration_identity() -> None:
    stable = declaration_id(
        EntityId("synthetic"), RecordKind.POSITION, SemanticKey("p")
    )
    first = revision_id(
        EntityId("synthetic"),
        SemanticKey("p"),
        effective_from=date(2026, 1, 1),
        effective_to=None,
        payload={"kind": "position", "schema_version": 1, "name": "one"},
    )
    changed = revision_id(
        EntityId("synthetic"),
        SemanticKey("p"),
        effective_from=date(2026, 1, 1),
        effective_to=None,
        payload={"kind": "position", "schema_version": 1, "name": "two"},
    )
    assert first != changed
    assert stable == declaration_id(
        EntityId("synthetic"), RecordKind.POSITION, SemanticKey("p")
    )
    with pytest.raises(IdentityError, match="self_referential_revision"):
        revision_id(
            EntityId("synthetic"),
            SemanticKey("p"),
            effective_from=date(2026, 1, 1),
            effective_to=None,
            payload={
                "kind": "position",
                "schema_version": 1,
                "revision_id": first.value,
            },
        )


def test_authored_anchor_and_semantic_successor_identity() -> None:
    entity = EntityId("synthetic")
    event = event_id(
        entity,
        Occurrence(AuthoredAnchor(SemanticKey("opening")), SemanticKey("opening")),
    )
    leg = child_id(event, RecordKind.LEG, SemanticKey("asset"))
    root = child_id(leg, RecordKind.LOT, SemanticKey("opening"))
    first = successor_lot_id(root, event, SemanticKey("first-output"))
    second = successor_lot_id(root, event, SemanticKey("second-output"))
    assert first != root
    assert second != first
    with pytest.raises(IdentityError, match="invalid_semantic_child_kind"):
        child_id(event, RecordKind.POOL, SemanticKey("bad"))
    with pytest.raises(IdentityError, match="format_qualified_locator"):
        AcquiredAnchor(
            "a" * 64, SemanticKey("n"), SemanticKey("s"), SemanticKey("row2")
        )


@pytest.mark.golden
def test_authored_anchor_literal_identity_digest() -> None:
    occurrence = Occurrence(
        AuthoredAnchor(SemanticKey("opening")), SemanticKey("opening")
    )
    assert event_id(EntityId("synthetic"), occurrence).value == (
        "7b039582e0ae3d85d948f81cab60efba1b3c9e5f705d085dbe9d70e75b765e48"
    )


@pytest.mark.golden
@pytest.mark.parametrize(
    ("start", "end", "interval"),
    [
        (None, None, b'"valid_from":null,"valid_to":null'),
        (
            date(2026, 1, 1),
            None,
            b'"valid_from":"2026-01-01","valid_to":null',
        ),
        (
            None,
            date(2026, 2, 1),
            b'"valid_from":null,"valid_to":"2026-02-01"',
        ),
        (
            date(2026, 1, 1),
            date(2026, 2, 1),
            b'"valid_from":"2026-01-01","valid_to":"2026-02-01"',
        ),
    ],
)
def test_revision_uses_literal_json_not_identity_framing(
    start: date | None,
    end: date | None,
    interval: bytes,
) -> None:
    literal = (
        b'{"declaration_key":"p","entity_id":"synthetic","kind":"position",'
        b'"payload":{"kind":"position","name":"one","schema_version":1},'
        b'"schema_version":1,' + interval + b"}\n"
    )
    actual = revision_id(
        EntityId("synthetic"),
        SemanticKey("p"),
        effective_from=start,
        effective_to=end,
        payload={"schema_version": 1, "name": "one", "kind": "position"},
    )
    assert actual.value == sha256(literal).hexdigest()


@pytest.mark.parametrize("end", [date(2026, 1, 1), date(2025, 12, 31)])
def test_revision_rejects_empty_or_reversed_effective_interval(end: date) -> None:
    with pytest.raises(IdentityError, match="revision_effective_interval"):
        revision_id(
            EntityId("synthetic"),
            SemanticKey("p"),
            effective_from=date(2026, 1, 1),
            effective_to=end,
            payload={"kind": "position", "schema_version": 1},
        )
