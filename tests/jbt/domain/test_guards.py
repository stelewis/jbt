from dataclasses import replace
from hashlib import sha256

import pytest

from jbt.domain.errors import GuardError
from jbt.domain.guards import (
    Guard,
    GuardField,
    GuardTarget,
    projection_digest,
    validate_guard,
    validate_rebinding,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind


@pytest.fixture
def target() -> GuardTarget:
    return GuardTarget(
        RecordId(EntityId("synthetic"), RecordKind.EVENT, "a" * 64),
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
    guard = Guard(target.identifier, fields, projection_digest(target, fields))
    assert validate_guard(guard, (target,)) == target
    relabeled = replace(
        target, fields=(*target.fields[:3], replace(target.fields[3], value="second"))
    )
    assert validate_guard(guard, (relabeled,)) == relabeled
    changed = replace(
        target, fields=(replace(target.fields[0], value="11"), *target.fields[1:])
    )
    with pytest.raises(GuardError, match="guard_projection_changed") as failure:
        validate_guard(guard, (changed,))
    assert failure.value.references == (target.identifier.value,)
    changed_schema = replace(
        target,
        fields=(
            *target.fields[:2],
            GuardField("date", "integer", nullable=True, value=None),
            target.fields[3],
        ),
    )
    with pytest.raises(GuardError, match="guard_projection_changed"):
        validate_guard(guard, (changed_schema,))


def test_missing_ambiguous_and_retired_targets_never_redirect(
    target: GuardTarget,
) -> None:
    fields = ("amount_coefficient",)
    guard = Guard(target.identifier, fields, projection_digest(target, fields))
    with pytest.raises(GuardError, match="unique_guard_target_required"):
        validate_guard(guard, ())
    with pytest.raises(GuardError, match="unique_guard_target_required"):
        validate_guard(guard, (target, target))
    with pytest.raises(GuardError, match="active_guard_target_required"):
        validate_guard(guard, (replace(target, active=False),))


def test_explicit_rebinding_checks_old_and_new_reviewed_projections(
    target: GuardTarget,
) -> None:
    fields = ("amount_coefficient",)
    replacement = replace(
        target,
        identifier=RecordId(target.identifier.entity, RecordKind.EVENT, "b" * 64),
        fields=(replace(target.fields[0], value="11"),),
    )
    old_guard = Guard(target.identifier, fields, projection_digest(target, fields))
    new_guard = Guard(
        replacement.identifier, fields, projection_digest(replacement, fields)
    )
    assert (
        validate_rebinding(
            old_guard, new_guard, (replace(target, active=False), replacement)
        )
        == replacement
    )
    changed_history = replace(target, fields=(replace(target.fields[0], value="9"),))
    with pytest.raises(GuardError, match="historical_guard_changed"):
        validate_rebinding(old_guard, new_guard, (changed_history, replacement))


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
