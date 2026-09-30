"""Bounded exact decimal and rational arithmetic, independent of context."""

import re
from dataclasses import dataclass
from enum import StrEnum
from math import gcd
from unicodedata import normalize

from jbt.domain.errors import (
    ArithmeticDomainError,
    NumericLimitError,
    NumericSyntaxError,
)

STORED_DIGITS = 96
STORED_SCALE = 38
WORK_DIGITS = 384
WORK_SCALE = 152
_WORK_MAX = 10**WORK_DIGITS - 1
_COEFFICIENT = re.compile(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)\Z", re.ASCII)
_LEXICAL = re.compile(r"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?\Z", re.ASCII)


def _scale(value: int, maximum: int, location: str) -> None:
    if type(value) is not int:
        raise NumericSyntaxError(constraint="integer_required", location=location)
    if not 0 <= value <= maximum:
        raise NumericLimitError(constraint="scale_limit", location=location)


def _integer(value: int, location: str) -> None:
    if type(value) is not int:
        raise NumericSyntaxError(constraint="integer_required", location=location)
    if abs(value) > _WORK_MAX:
        raise NumericLimitError(constraint="intermediate_digits", location=location)


def _digits(value: int) -> int:
    return len(str(abs(value)))


def _product(left: int, right: int) -> int:
    if left and abs(right) > _WORK_MAX // abs(left):
        raise NumericLimitError(constraint="product_digits", location="arithmetic")
    return left * right


def _power(scale: int) -> int:
    _scale(scale, WORK_SCALE, "arithmetic.scale")
    return 10**scale


def _sum(values: tuple[int, ...]) -> int:
    nonzero = tuple(value for value in values if value)
    if not nonzero:
        return 0
    growth = len(str(len(nonzero) - 1)) if len(nonzero) > 1 else 0
    if max(map(_digits, nonzero)) + growth > WORK_DIGITS:
        raise NumericLimitError(constraint="accumulation_digits", location="arithmetic")
    result = sum(nonzero)
    _integer(result, "arithmetic.result")
    return result


@dataclass(frozen=True, slots=True)
class ExactDecimal:
    """Canonical stored value; source scale is evidence, not value scale."""

    coefficient: str
    scale: int
    source_scale: int | None

    def __post_init__(self) -> None:
        """Validate the storage envelope before parsing any coefficient."""
        if not isinstance(self.coefficient, str):
            raise NumericSyntaxError(
                constraint="coefficient_string_required", location="number"
            )
        if len(self.coefficient.removeprefix("-")) > STORED_DIGITS:
            raise NumericLimitError(
                constraint="stored_digits", location="number.coefficient"
            )
        if not _COEFFICIENT.fullmatch(self.coefficient):
            raise NumericSyntaxError(
                constraint="canonical_coefficient", location="number.coefficient"
            )
        _scale(self.scale, STORED_SCALE, "number.scale")
        if self.source_scale is not None:
            _scale(self.source_scale, STORED_SCALE, "number.source_scale")
            if self.source_scale < self.scale:
                raise NumericSyntaxError(
                    constraint="source_scale_below_value",
                    location="number.source_scale",
                )
        if self.scale and self.coefficient.endswith("0"):
            raise NumericSyntaxError(
                constraint="canonical_scale", location="number.scale"
            )

    @classmethod
    def parse(cls, text: str) -> ExactDecimal:
        """Normalize finite source decimal text, preserving stated scale."""
        if not isinstance(text, str):
            raise NumericSyntaxError(
                constraint="decimal_text_required", location="number"
            )
        if len(text) > STORED_DIGITS + STORED_SCALE + 2:
            raise NumericLimitError(constraint="source_length", location="number")
        match = _LEXICAL.fullmatch(text)
        if match is None:
            raise NumericSyntaxError(
                constraint="finite_decimal_required", location="number"
            )
        sign, integral, fraction = match.groups()
        fraction = fraction or ""
        source_scale = len(fraction)
        _scale(source_scale, STORED_SCALE, "number.source_scale")
        coefficient = (integral + fraction).lstrip("0") or "0"
        scale = source_scale
        while scale and coefficient.endswith("0"):
            coefficient = coefficient[:-1] or "0"
            scale -= 1
        if coefficient == "0":
            scale = 0
        elif sign:
            coefficient = "-" + coefficient
        return cls(coefficient, scale, source_scale)

    def rational(self) -> BoundedRational:
        """Lift a stored value without carrying source precision forward."""
        return BoundedRational(int(self.coefficient), 1, self.scale)


class RoundingMode(StrEnum):
    """Supported quotient/remainder decisions."""

    HALF_EVEN = "half_even"
    HALF_UP = "half_up"
    TOWARD_ZERO = "toward_zero"


@dataclass(frozen=True, slots=True)
class RoundingPolicy:
    """A declared storage scale and rounding mode."""

    scale: int
    mode: RoundingMode

    def __post_init__(self) -> None:
        """Reject implicit modes and scales outside the storage contract."""
        _scale(self.scale, STORED_SCALE, "rounding.scale")
        if not isinstance(self.mode, RoundingMode):
            raise NumericSyntaxError(
                constraint="rounding_mode_required", location="rounding.mode"
            )


@dataclass(frozen=True, slots=True)
class BoundedRational:
    """Exact numerator / denominator * 10^-scale, reduced on construction."""

    numerator: int
    denominator: int
    scale: int

    def __post_init__(self) -> None:
        """Bound inputs before cancellation and keep a positive denominator."""
        _integer(self.numerator, "rational.numerator")
        _integer(self.denominator, "rational.denominator")
        _scale(self.scale, WORK_SCALE, "rational.scale")
        if self.denominator <= 0:
            raise ArithmeticDomainError(
                constraint="positive_denominator_required", location="rational"
            )
        common = gcd(self.numerator, self.denominator)
        object.__setattr__(self, "numerator", self.numerator // common)
        object.__setattr__(self, "denominator", self.denominator // common)

    def add(self, other: BoundedRational) -> BoundedRational:
        """Add exactly after checked alignment and denominator cancellation."""
        scale = max(self.scale, other.scale)
        common = gcd(self.denominator, other.denominator)
        left_factor = other.denominator // common
        right_factor = self.denominator // common
        left = _product(self.numerator, _power(scale - self.scale))
        right = _product(other.numerator, _power(scale - other.scale))
        numerator = _sum((_product(left, left_factor), _product(right, right_factor)))
        denominator = _product(self.denominator, left_factor)
        return BoundedRational(numerator, denominator, scale)

    def subtract(self, other: BoundedRational) -> BoundedRational:
        """Subtract without rounding."""
        return self.add(
            BoundedRational(-other.numerator, other.denominator, other.scale)
        )

    def multiply(self, other: BoundedRational) -> BoundedRational:
        """Cancel cross factors before admitting products."""
        scale = self.scale + other.scale
        _scale(scale, WORK_SCALE, "arithmetic.scale")
        left_common = gcd(self.numerator, other.denominator)
        right_common = gcd(other.numerator, self.denominator)
        numerator = _product(
            self.numerator // left_common, other.numerator // right_common
        )
        denominator = _product(
            self.denominator // right_common, other.denominator // left_common
        )
        return BoundedRational(numerator, denominator, scale)

    def divide(self, other: BoundedRational) -> BoundedRational:
        """Preserve nonterminating results as bounded rationals."""
        if other.numerator == 0:
            raise ArithmeticDomainError(
                constraint="division_by_zero", location="arithmetic"
            )
        sign = -1 if other.numerator < 0 else 1
        reciprocal = BoundedRational(sign * other.denominator, abs(other.numerator), 0)
        quotient = self.multiply(reciprocal)
        if quotient.scale >= other.scale:
            return BoundedRational(
                quotient.numerator, quotient.denominator, quotient.scale - other.scale
            )
        expansion = _power(other.scale - quotient.scale)
        common = gcd(expansion, quotient.denominator)
        return BoundedRational(
            _product(quotient.numerator, expansion // common),
            quotient.denominator // common,
            0,
        )

    def round(self, policy: RoundingPolicy) -> ExactDecimal:
        """Round using integer quotient/remainder, then enforce storage."""
        numerator, denominator = self.numerator, self.denominator
        if policy.scale >= self.scale:
            expansion = _power(policy.scale - self.scale)
            common = gcd(expansion, denominator)
            numerator = _product(numerator, expansion // common)
            denominator //= common
        else:
            expansion = _power(self.scale - policy.scale)
            common = gcd(expansion, numerator)
            numerator //= common
            denominator = _product(denominator, expansion // common)
        quotient, remainder = divmod(abs(numerator), denominator)
        above_half = remainder > denominator - remainder
        tie = remainder == denominator - remainder
        increment = policy.mode != RoundingMode.TOWARD_ZERO and (
            above_half
            or (tie and (policy.mode == RoundingMode.HALF_UP or quotient % 2 == 1))
        )
        coefficient = quotient + int(increment)
        if numerator < 0:
            coefficient = -coefficient
        return _stored(coefficient, policy.scale)

    def exact_decimal(self) -> ExactDecimal:
        """Store only a terminating value, never approximate to fit."""
        denominator = self.denominator
        twos = fives = 0
        while denominator % 2 == 0:
            denominator //= 2
            twos += 1
        while denominator % 5 == 0:
            denominator //= 5
            fives += 1
        if denominator != 1:
            raise ArithmeticDomainError(
                constraint="nonterminating_decimal", location="arithmetic.result"
            )
        extra_scale = max(twos, fives)
        _scale(self.scale + extra_scale, WORK_SCALE, "arithmetic.result.scale")
        factor = _power(extra_scale) // self.denominator
        return _stored(_product(self.numerator, factor), self.scale + extra_scale)


def _stored(coefficient: int, scale: int) -> ExactDecimal:
    while scale and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    return ExactDecimal(str(coefficient), scale, None)


@dataclass(frozen=True, slots=True)
class WeightedSlice:
    """A caller-selected stable allocation key and nonnegative weight."""

    key: str
    weight: ExactDecimal

    def __post_init__(self) -> None:
        """Require a named slice and a known nonnegative weight."""
        if (
            not isinstance(self.key, str)
            or not self.key
            or normalize("NFC", self.key) != self.key
        ):
            raise ArithmeticDomainError(
                constraint="slice_key_required", location="allocation"
            )
        if not isinstance(
            self.weight, ExactDecimal
        ) or self.weight.coefficient.startswith("-"):
            raise ArithmeticDomainError(
                constraint="nonnegative_weight_required", location="allocation"
            )


@dataclass(frozen=True, slots=True)
class AllocatedSlice:
    """An exact allocation with its separately observable rounding residual."""

    key: str
    amount: ExactDecimal
    residual: ExactDecimal


def allocate_residual(
    total: ExactDecimal,
    slices: tuple[WeightedSlice, ...],
    policy: RoundingPolicy,
) -> tuple[AllocatedSlice, ...]:
    """Allocate in supplied stable order; assign the residual to its last key."""
    if (
        not isinstance(slices, tuple)
        or not slices
        or len({item.key for item in slices}) != len(slices)
    ):
        raise ArithmeticDomainError(
            constraint="unique_nonempty_slices_required", location="allocation"
        )
    if total.scale > policy.scale:
        raise ArithmeticDomainError(
            constraint="total_not_at_rounding_scale", location="allocation"
        )
    weight = BoundedRational(0, 1, 0)
    for item in slices:
        weight = weight.add(item.weight.rational())
    if not weight.numerator:
        raise ArithmeticDomainError(
            constraint="positive_total_weight_required", location="allocation"
        )
    allocated = BoundedRational(0, 1, 0)
    result: list[AllocatedSlice] = []
    zero = ExactDecimal("0", 0, None)
    for item in slices:
        share = total.rational().multiply(item.weight.rational()).divide(weight)
        amount = share.round(policy)
        result.append(AllocatedSlice(item.key, amount, zero))
        allocated = allocated.add(amount.rational())
    residual = total.rational().subtract(allocated).exact_decimal()
    final = result[-1]
    amount = final.amount.rational().add(residual.rational()).exact_decimal()
    if amount.coefficient != "0" and amount.coefficient.startswith(
        "-"
    ) != total.coefficient.startswith("-"):
        raise ArithmeticDomainError(
            constraint="residual_reverses_slice_sign", location="allocation"
        )
    result[-1] = AllocatedSlice(final.key, amount, residual)
    return tuple(result)
