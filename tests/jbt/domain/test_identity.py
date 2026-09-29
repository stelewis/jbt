from datetime import date
from hashlib import sha256
from itertools import permutations

import pytest

from jbt.domain.errors import IdentityError
from jbt.domain.identity import (
    AcquiredAnchor,
    AuthoredAnchor,
    IdentityDecision,
    IdentityReview,
    Membership,
    Occurrence,
    Retirement,
    child_id,
    declaration_id,
    event_id,
    revision_id,
    successor_lot_id,
    validate_membership,
    validate_retirements,
    validate_transition,
)
from jbt.domain.ids import EntityId, RecordKind, SemanticKey


@pytest.fixture
def occurrences() -> tuple[Occurrence, Occurrence, Occurrence]:
    first, second, third = (
        Occurrence(
            AcquiredAnchor(
                digest * 64,
                SemanticKey("synthetic-binding"),
                SemanticKey("account-section"),
                SemanticKey("csv:row:2"),
            ),
            SemanticKey("purchase"),
        )
        for digest in ("a", "b", "c")
    )
    return first, second, third


def test_extra_support_and_acquisition_history_do_not_reidentify(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, third = occurrences
    entity = EntityId("synthetic")
    canonical_id = event_id(entity, first)
    leg = child_id(canonical_id, RecordKind.LEG, SemanticKey("asset"))
    root = child_id(leg, RecordKind.LOT, SemanticKey("acquisition"))
    for acquisition_order in permutations(occurrences):
        decision = IdentityDecision(
            first, acquisition_order, (SemanticKey("reviewed-bank-reference"),)
        )
        result = validate_membership(
            entity,
            (*acquisition_order, first),
            (decision,),
            required_correspondences=((first, second, third),),
        )
        assert {item.event for item in result} == {canonical_id}
        assert len(result) == 3
        assert child_id(canonical_id, RecordKind.LEG, SemanticKey("asset")) == leg
        assert child_id(leg, RecordKind.LOT, SemanticKey("acquisition")) == root
    singleton = validate_membership(
        entity, (first, first), (), required_correspondences=()
    )
    assert singleton == (Membership(first, canonical_id),)


def test_same_bytes_in_distinct_scopes_and_source_components_remain_distinct(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    original = occurrences[0]
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
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first = occurrences[0]
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
    result = validate_membership(
        EntityId("synthetic"), (first, second), (), required_correspondences=()
    )
    assert len({member.event for member in result}) == 2


def test_missing_overlap_decision_or_retained_anchor_fails(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, _ = occurrences
    decision = IdentityDecision(first, (first, second), (SemanticKey("review"),))
    with pytest.raises(IdentityError, match="explicit_membership_required"):
        validate_membership(
            EntityId("synthetic"),
            (first, second),
            (),
            required_correspondences=((first, second),),
        )
    with pytest.raises(IdentityError, match="missing_retained_anchor"):
        validate_membership(
            EntityId("synthetic"), (first,), (decision,), required_correspondences=()
        )
    with pytest.raises(IdentityError, match="contradictory_membership"):
        validate_membership(
            EntityId("synthetic"),
            (first, second),
            (decision, decision),
            required_correspondences=(),
        )


def test_identity_decisions_reject_ambiguous_or_unreviewed_membership(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, _ = occurrences
    with pytest.raises(IdentityError, match="canonical_unique_members"):
        IdentityDecision(first, (second,), (SemanticKey("review"),))
    with pytest.raises(IdentityError, match="canonical_unique_members"):
        IdentityDecision(first, (first, first), (SemanticKey("review"),))
    with pytest.raises(IdentityError, match="reviewed_discriminants"):
        IdentityDecision(first, (first, second), ())


def test_merge_and_split_require_retirement_and_affected_review(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, _ = occurrences
    entity = EntityId("synthetic")
    first_id, second_id = event_id(entity, first), event_id(entity, second)
    before = (Membership(first, first_id), Membership(second, second_id))
    merged = (Membership(first, first_id), Membership(second, first_id))
    retirement = Retirement(second_id, first_id)
    affected = frozenset({child_id(second_id, RecordKind.LEG, SemanticKey("cash"))})
    with pytest.raises(IdentityError, match="explicit_retirement_required"):
        validate_transition(
            before,
            merged,
            (),
            prior_retirements=(),
            review=IdentityReview(affected, affected),
        )
    with pytest.raises(IdentityError, match="unreviewed_affected_targets"):
        validate_transition(
            before,
            merged,
            (retirement,),
            prior_retirements=(),
            review=IdentityReview(affected, frozenset()),
        )
    validate_transition(
        before,
        merged,
        (retirement,),
        prior_retirements=(),
        review=IdentityReview(affected, affected),
    )
    with pytest.raises(IdentityError, match="retired_identity_reuse"):
        validate_transition(
            merged,
            before,
            (),
            prior_retirements=(retirement,),
            review=IdentityReview(affected, affected),
        )
    validate_transition(
        merged,
        before,
        (),
        prior_retirements=(),
        review=IdentityReview(affected, affected),
    )


def test_retirement_history_stays_addressable_and_acyclic(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, third = (
        event_id(EntityId("synthetic"), item) for item in occurrences
    )
    chain = (Retirement(first, second), Retirement(second, third))
    validate_retirements(chain, history=frozenset({first, second, third}))
    with pytest.raises(IdentityError, match="retirement_cycle"):
        validate_retirements(
            (*chain, Retirement(third, first)),
            history=frozenset({first, second, third}),
        )
    with pytest.raises(IdentityError, match="missing_retirement_history"):
        validate_retirements(chain, history=frozenset({first, third}))
    with pytest.raises(IdentityError, match="multiple_retirement_survivors"):
        validate_retirements(
            (Retirement(first, second), Retirement(first, third)),
            history=frozenset({first, second, third}),
        )


def test_split_can_explicitly_retire_old_anchor(
    occurrences: tuple[Occurrence, Occurrence, Occurrence],
) -> None:
    first, second, third = occurrences
    entity = EntityId("synthetic")
    original, left, right = (event_id(entity, item) for item in occurrences)
    merged = tuple(Membership(item, original) for item in occurrences)
    split = (
        Membership(first, left),
        Membership(second, left),
        Membership(third, right),
    )
    validate_transition(
        merged,
        split,
        (Retirement(original, left),),
        prior_retirements=(),
        review=IdentityReview(frozenset({original}), frozenset({original})),
    )


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
def test_revision_uses_literal_json_not_identity_framing() -> None:
    literal = (
        b'{"declaration_key":"p","entity_id":"synthetic","kind":"position",'
        b'"payload":{"kind":"position","name":"one","schema_version":1},'
        b'"schema_version":1,"valid_from":"2026-01-01","valid_to":null}\n'
    )
    actual = revision_id(
        EntityId("synthetic"),
        SemanticKey("p"),
        effective_from=date(2026, 1, 1),
        effective_to=None,
        payload={"schema_version": 1, "name": "one", "kind": "position"},
    )
    assert actual.value == sha256(literal).hexdigest()
