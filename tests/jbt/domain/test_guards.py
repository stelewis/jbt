from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from hashlib import sha256

import pytest

from jbt.domain.errors import GuardError
from jbt.domain.guards import (
    Guard,
    GuardEvaluation,
    GuardField,
    GuardOutcome,
    GuardReference,
    GuardTarget,
    evaluate_guard,
    projection_digest,
)
from jbt.domain.ids import EntityId, SemanticKey


@pytest.fixture
def target() -> GuardTarget:
    return GuardTarget(
        GuardReference(EntityId("synthetic"), SemanticKey("events"), ("a",)),
        (
            GuardField("amount_coefficient", "string", nullable=False, value="10"),
            GuardField("amount_scale", "integer", nullable=False, value=0),
            GuardField("date", "date", nullable=True, value=None),
            GuardField("label", "string", nullable=False, value="first"),
        ),
        active=True,
    )


def test_guard_covers_exact_selected_fields_including_unknowns(
    target: GuardTarget,
) -> None:
    fields = ("amount_coefficient", "amount_scale", "date")
    guard = Guard(target.reference, fields, projection_digest(target, fields))
    evaluated = evaluate_guard(guard, (target,))
    assert evaluated.outcome is GuardOutcome.APPLIED
    assert evaluated.guard == guard
    assert evaluated.reference == target.reference
    assert evaluated.observed_fields == target.fields[:3]
    assert evaluated.observed_digest == guard.expected_digest
    relabeled = replace(
        target, fields=(*target.fields[:3], replace(target.fields[3], value="second"))
    )
    assert evaluate_guard(guard, (relabeled,)) == evaluated
    changed = replace(
        target, fields=(replace(target.fields[0], value="11"), *target.fields[1:])
    )
    changed_result = evaluate_guard(guard, (changed,))
    assert changed_result.outcome is GuardOutcome.CHANGED
    assert changed_result.observed_fields == changed.fields[:3]
    assert changed_result.observed_digest == projection_digest(changed, fields)
    changed_schema = replace(
        target,
        fields=(
            *target.fields[:2],
            GuardField("date", "integer", nullable=True, value=None),
            target.fields[3],
        ),
    )
    assert evaluate_guard(guard, (changed_schema,)).outcome is GuardOutcome.CHANGED
    absent = replace(target, fields=target.fields[:2])
    absent_result = evaluate_guard(guard, (absent,))
    assert absent_result.outcome is GuardOutcome.CHANGED
    assert absent_result.observed_fields == absent.fields
    assert absent_result.observed_digest is None


def test_guard_projection_is_exact_for_descriptive_unicode(
    target: GuardTarget,
) -> None:
    text = " e\u0301\n\x00 "
    nfd = replace(
        target, fields=(*target.fields[:3], replace(target.fields[3], value=text))
    )
    nfc = replace(
        target,
        fields=(*target.fields[:3], replace(target.fields[3], value=" é\n\x00 ")),
    )
    fields = ("label",)
    guard = Guard(nfd.reference, fields, projection_digest(nfd, fields))
    assert projection_digest(nfd, fields) != projection_digest(nfc, fields)
    assert evaluate_guard(guard, (nfd,)).outcome is GuardOutcome.APPLIED
    assert evaluate_guard(guard, (nfc,)).outcome is GuardOutcome.CHANGED
    assert nfd.fields[3].value == text


def test_missing_ambiguous_and_retired_targets_never_redirect(
    target: GuardTarget,
) -> None:
    fields = ("amount_coefficient",)
    guard = Guard(target.reference, fields, projection_digest(target, fields))
    for targets, outcome in (
        ((), GuardOutcome.MISSING),
        ((target, target), GuardOutcome.AMBIGUOUS),
    ):
        result = evaluate_guard(guard, targets)
        assert result.outcome is outcome
        assert result.observed_fields == ()
        assert result.observed_digest is None
    replacement = replace(target, reference=replace(target.reference, key=("b",)))
    inactive = evaluate_guard(guard, (replace(target, active=False), replacement))
    assert inactive.outcome is GuardOutcome.INACTIVE
    assert inactive.reference == target.reference
    assert inactive.observed_digest == guard.expected_digest
    retired_changed = replace(target, active=False, fields=())
    assert (
        evaluate_guard(guard, (retired_changed, replacement)).outcome
        is GuardOutcome.INACTIVE
    )


def test_projection_rejects_absence_duplicates_and_self_reference(
    target: GuardTarget,
) -> None:
    with pytest.raises(GuardError, match="guard_field_missing"):
        projection_digest(target, ("missing",))
    with pytest.raises(GuardError, match="sorted_unique_guard_fields"):
        projection_digest(target, ("date", "amount_scale"))
    with pytest.raises(GuardError, match="sorted_unique_guard_fields"):
        projection_digest(target, ("amount_scale", "amount_scale"))
    self_reference = replace(
        target,
        fields=(
            GuardField("expected_digest", "string", nullable=False, value="a" * 64),
        ),
    )
    with pytest.raises(GuardError, match="self_referential_guard"):
        projection_digest(self_reference, ("expected_digest",))


@pytest.mark.golden
def test_projection_uses_literal_schema_typed_row_json(target: GuardTarget) -> None:
    literal = (
        b'{"columns":[{"kind":"integer","name":"amount_scale","nullable":false},'
        b'{"kind":"date","name":"date","nullable":true}],"schema_version":"1"}\n'
        b'["0",null]\n'
    )
    assert (
        projection_digest(target, ("amount_scale", "date"))
        == sha256(literal).hexdigest()
    )


def test_field_types_reject_boolean_integer_confusion() -> None:
    with pytest.raises(GuardError, match="field_value_type_mismatch"):
        GuardField("scale", "integer", nullable=False, value=True)
    with pytest.raises(GuardError, match="nonnull_field_required"):
        GuardField("scale", "integer", nullable=False, value=None)


@pytest.mark.parametrize("key", [(), ("step", 3, date(2026, 1, 1))])
def test_logical_keys_support_empty_and_composite_addresses(
    target: GuardTarget, key: tuple[str | int | date, ...]
) -> None:
    target = replace(target, reference=replace(target.reference, key=key))
    guard = Guard(target.reference, ("date",), projection_digest(target, ("date",)))
    assert evaluate_guard(guard, (target,)).outcome is GuardOutcome.APPLIED


@pytest.mark.parametrize(
    "key",
    [[1], (True,), (1.0,), (None,), (["a"],), (datetime(2026, 1, 1, tzinfo=UTC),)],
)
def test_reference_rejects_untyped_mutable_and_boolean_keys(key: object) -> None:
    with pytest.raises(GuardError, match="immutable_typed_reference_required"):
        GuardReference(EntityId("e"), SemanticKey("events"), key)  # ty: ignore[invalid-argument-type]


def test_reference_requires_typed_entity_and_table() -> None:
    with pytest.raises(GuardError, match="immutable_typed_reference_required"):
        GuardReference("e", SemanticKey("events"), ())  # ty: ignore[invalid-argument-type]
    with pytest.raises(GuardError, match="immutable_typed_reference_required"):
        GuardReference(EntityId("e"), "events", ())  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("entity", EntityId("other")),
        ("table", SemanticKey("legs")),
    ],
)
def test_guard_reference_preserves_entity_and_table_identity(
    target: GuardTarget, dimension: str, value: EntityId | SemanticKey
) -> None:
    fields = ("date",)
    guard = Guard(target.reference, fields, projection_digest(target, fields))
    replacement = replace(
        target, reference=replace(target.reference, **{dimension: value})
    )
    assert evaluate_guard(guard, (replacement,)).outcome is GuardOutcome.MISSING


def test_reference_key_scalar_types_are_not_coerced() -> None:
    reference = GuardReference(EntityId("e"), SemanticKey("events"), (1,))
    assert reference != replace(reference, key=("1",))
    assert reference != replace(reference, key=(date(1, 1, 1),))


def test_evaluation_construction_rejects_contradictory_evidence(
    target: GuardTarget,
) -> None:
    guard = Guard(target.reference, ("date",), projection_digest(target, ("date",)))
    applied = evaluate_guard(guard, (target,))
    for changes in (
        {"observed_fields": (), "observed_digest": None},
        {"observed_digest": "0" * 64},
        {"observed_fields": (replace(target.fields[2], value=date(2026, 1, 1)),)},
        {"outcome": GuardOutcome.CHANGED},
        {"outcome": GuardOutcome.MISSING},
    ):
        with pytest.raises(GuardError, match="consistent_evaluation_required"):
            replace(applied, **changes)
    with pytest.raises(GuardError, match="typed_evaluation_required"):
        GuardEvaluation(guard, "applied", (), None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(FrozenInstanceError):
        applied.outcome = GuardOutcome.CHANGED  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        target.reference.key = ("changed",)  # ty: ignore[invalid-assignment]


def test_malformed_guard_contracts_remain_observable(target: GuardTarget) -> None:
    with pytest.raises(GuardError, match="sha256_digest_required"):
        Guard(target.reference, ("date",), "invalid")
    with pytest.raises(GuardError, match="self_referential_guard"):
        Guard(target.reference, ("guard_digest",), "0" * 64)
    with pytest.raises(GuardError, match="typed_guard_required"):
        Guard("a", ("date",), "0" * 64)  # ty: ignore[invalid-argument-type]
    with pytest.raises(GuardError, match="sorted_unique_guard_fields"):
        Guard(target.reference, (), "0" * 64)
    with pytest.raises(GuardError, match="immutable_typed_target_required"):
        projection_digest("a", ("date",))  # ty: ignore[invalid-argument-type]
    with pytest.raises(GuardError, match="field_schema_required"):
        GuardField("date", [], nullable=True, value=None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(GuardError, match="int64_required"):
        GuardField("scale", "integer", nullable=False, value=2**63)
    with pytest.raises(GuardError, match="utf8_required"):
        GuardField("label", "string", nullable=False, value="\ud800")
