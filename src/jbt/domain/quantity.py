"""Bounded quantity arithmetic shared by resolution and independent checks."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from jbt.domain.numbers import BoundedRational, ExactDecimal

if TYPE_CHECKING:
    from collections.abc import Iterable


class EventState(StrEnum):
    """Economic participation, never a resolution or review status."""

    RECOGNIZED = "recognized"
    PENDING = "pending"
    CANCELLED = "cancelled"
    RETRACTED = "retracted"


class FactKind(StrEnum):
    """Independently scoped financial evidence families."""

    MOVEMENT = "movement"
    BALANCE = "balance"
    ACTION = "action"


class ReviewState(StrEnum):
    """Descriptive review state, independent of economic recognition."""

    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    IGNORED = "ignored"


class LegKind(StrEnum):
    """Position quantities and their balancing economic boundary."""

    POSITION = "position"
    BOUNDARY = "boundary"


def total(values: Iterable[ExactDecimal]) -> ExactDecimal:
    """Sum one commodity's units exactly, retaining bounded arithmetic failures."""
    result = BoundedRational(0, 1, 0)
    for value in values:
        result = result.add(value.rational())
    return result.exact_decimal()


def negate(value: ExactDecimal) -> ExactDecimal:
    """Reverse signed units without preserving source lexical precision."""
    return ExactDecimal(str(-int(value.coefficient)), value.scale, None)


def equal(left: ExactDecimal, right: ExactDecimal) -> bool:
    """Compare numeric values independently of their stated decimal scales."""
    return left.rational().subtract(right.rational()).numerator == 0
