from dataclasses import replace

# GuardField booleans describe its physical schema.
# ruff: noqa: FBT003
import pytest

from jbt.domain.errors import GuardError
from jbt.domain.guards import (
    Guard,
    GuardField,
    GuardOutcome,
    GuardReference,
    GuardTarget,
    evaluate_guard,
    projection_digest,
)
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.review import compound_candidates, compound_target


def _target(key: str) -> GuardTarget:
    return GuardTarget(
        GuardReference(EntityId("synthetic"), SemanticKey("review_sources"), (key,)),
        (GuardField("facts", "string", False, '{"amount":"-5","account":"card"}'),),
        active=True,
    )


def test_compound_guard_seals_every_operand_and_uses_native_outcomes() -> None:
    first, second = _target("a" * 64), _target("b" * 64)
    expected = compound_target((first, second))
    assert expected == compound_target((second, first))
    guard = Guard(
        expected.reference, ("members",), projection_digest(expected, ("members",))
    )
    changed = replace(
        second,
        fields=(
            GuardField("facts", "string", False, '{"amount":"-6","account":"card"}'),
        ),
    )
    for inventory, outcome in (
        ((first, second), GuardOutcome.APPLIED),
        ((first, changed), GuardOutcome.CHANGED),
        ((first, replace(second, active=False)), GuardOutcome.INACTIVE),
        ((first,), GuardOutcome.MISSING),
        ((first, second, changed), GuardOutcome.AMBIGUOUS),
    ):
        assert (
            evaluate_guard(guard, compound_candidates(expected, inventory)).outcome
            == outcome
        )
    unrelated = _target("c" * 64)
    assert compound_candidates(expected, (first, second, unrelated)) == (expected,)
    with pytest.raises(GuardError, match="unique_operands"):
        compound_target((first, first))
    with pytest.raises(GuardError, match="same_entity"):
        compound_target(
            (
                first,
                replace(
                    second,
                    reference=replace(second.reference, entity=EntityId("another")),
                ),
            )
        )
