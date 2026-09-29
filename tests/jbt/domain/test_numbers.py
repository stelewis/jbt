from fractions import Fraction
from typing import cast

import pytest

from jbt.domain.errors import (
    ArithmeticDomainError,
    NumericLimitError,
    NumericSyntaxError,
)
from jbt.domain.numbers import (
    BoundedRational,
    ExactDecimal,
    RoundingMode,
    RoundingPolicy,
    WeightedSlice,
    allocate_residual,
)


@pytest.mark.parametrize(
    ("text", "coefficient", "scale", "source_scale"),
    [
        ("84.20", "842", 1, 2),
        ("0.00", "0", 0, 2),
        ("-0.000", "0", 0, 3),
        ("-120.00", "-120", 0, 2),
        ("0.000001", "1", 6, 6),
        ("1." + "0" * 38, "1", 0, 38),
        ("0." + "0" * 37 + "1", "1", 38, 38),
    ],
)
def test_source_normalization_preserves_precision(
    text: str, coefficient: str, scale: int, source_scale: int
) -> None:
    assert ExactDecimal.parse(text) == ExactDecimal(coefficient, scale, source_scale)


@pytest.mark.parametrize(
    "text", ["NaN", "Inf", "1e2", "1.", ".1", "+1", "01", " 1", "\u0661"]
)
def test_nonfinite_or_ambiguous_source_syntax_fails(text: str) -> None:
    with pytest.raises(NumericSyntaxError, match="finite_decimal_required"):
        ExactDecimal.parse(text)


@pytest.mark.parametrize("coefficient", ["", "-0", "+1", "01", "1.0", "\u0661"])
def test_noncanonical_coefficients_fail(coefficient: str) -> None:
    with pytest.raises(NumericSyntaxError, match="canonical_coefficient"):
        ExactDecimal(coefficient, 0, None)


def test_storage_digit_and_scale_boundaries() -> None:
    assert ExactDecimal("9" * 96, 38, 38).coefficient == "9" * 96
    assert ExactDecimal("-" + "9" * 96, 0, None).scale == 0
    with pytest.raises(NumericLimitError, match="stored_digits"):
        ExactDecimal("1" * 97, 0, None)
    with pytest.raises(NumericLimitError, match="scale_limit"):
        ExactDecimal("1", 39, None)
    with pytest.raises(NumericLimitError, match="scale_limit"):
        ExactDecimal("1", 0, 39)
    with pytest.raises(NumericLimitError, match="scale_limit"):
        ExactDecimal.parse("0." + "0" * 39)


def test_scale_and_source_precision_are_separate_invariants() -> None:
    assert ExactDecimal("1", 0, None) != ExactDecimal("1", 0, 0)
    assert ExactDecimal("1", 0, None).rational() == ExactDecimal("1", 0, 2).rational()
    with pytest.raises(NumericSyntaxError, match="source_scale_below_value"):
        ExactDecimal("1", 2, 1)
    with pytest.raises(NumericSyntaxError, match="canonical_scale"):
        ExactDecimal("10", 1, None)
    with pytest.raises(NumericSyntaxError, match="canonical_scale"):
        ExactDecimal("0", 1, None)


@pytest.mark.parametrize("value", [True, 1, 1.0, None])
def test_decimal_constructor_rejects_nontext_coefficients(value: object) -> None:
    with pytest.raises(NumericSyntaxError, match="coefficient_string_required"):
        ExactDecimal(cast("str", value), 0, None)
    with pytest.raises(NumericSyntaxError, match="decimal_text_required"):
        ExactDecimal.parse(cast("str", value))


@pytest.mark.parametrize("value", [True, False])
def test_booleans_never_supply_numeric_integers(*, value: bool) -> None:
    with pytest.raises(NumericSyntaxError, match="integer_required"):
        ExactDecimal("1", value, None)
    with pytest.raises(NumericSyntaxError, match="integer_required"):
        ExactDecimal("1", 0, value)
    with pytest.raises(NumericSyntaxError, match="integer_required"):
        BoundedRational(value, 1, 0)
    with pytest.raises(NumericSyntaxError, match="integer_required"):
        BoundedRational(1, value, 0)
    with pytest.raises(NumericSyntaxError, match="integer_required"):
        RoundingPolicy(value, RoundingMode.HALF_EVEN)


def test_intermediate_boundaries_and_positive_denominator() -> None:
    maximum = 10**384 - 1
    assert BoundedRational(maximum, 1, 152).numerator == maximum
    assert BoundedRational(1, maximum, 0).denominator == maximum
    with pytest.raises(NumericLimitError, match="intermediate_digits"):
        BoundedRational(maximum + 1, 1, 0)
    with pytest.raises(NumericLimitError, match="intermediate_digits"):
        BoundedRational(1, maximum + 1, 0)
    with pytest.raises(NumericLimitError, match="scale_limit"):
        BoundedRational(1, 1, 153)
    with pytest.raises(ArithmeticDomainError, match="positive_denominator"):
        BoundedRational(1, 0, 0)
    with pytest.raises(ArithmeticDomainError, match="positive_denominator"):
        BoundedRational(1, -1, 0)


def test_products_cancel_before_bounded_multiplication() -> None:
    maximum = 10**384 - 1
    left = BoundedRational(maximum, 2, 0)
    right = BoundedRational(2, maximum, 0)
    assert left.multiply(right) == BoundedRational(1, 1, 0)
    assert left.multiply(BoundedRational(1, 1, 0)) == left
    with pytest.raises(NumericLimitError, match="product_digits"):
        left.multiply(BoundedRational(3, 1, 0))


def test_four_storage_factors_fit_but_fifth_can_fail() -> None:
    factor = ExactDecimal("9" * 96, 38, None).rational()
    product = factor.multiply(factor).multiply(factor).multiply(factor)
    assert product.numerator == (10**96 - 1) ** 4
    assert product.scale == 152
    with pytest.raises(NumericLimitError, match="scale_limit"):
        product.multiply(factor)
    with pytest.raises(NumericLimitError, match="stored_digits"):
        product.exact_decimal()


def test_rounded_results_must_still_fit_storage() -> None:
    with pytest.raises(NumericLimitError, match="stored_digits"):
        BoundedRational(10**96, 1, 0).round(RoundingPolicy(0, RoundingMode.TOWARD_ZERO))
    with pytest.raises(NumericLimitError, match="scale_limit"):
        BoundedRational(1, 1, 39).exact_decimal()
    with pytest.raises(NumericLimitError, match="product_digits"):
        BoundedRational(10**383, 1, 0).round(RoundingPolicy(2, RoundingMode.HALF_EVEN))


def test_alignment_and_accumulation_are_preflighted() -> None:
    left = BoundedRational(10**383, 1, 0)
    assert left.add(BoundedRational(0, 1, 0)) == left
    with pytest.raises(NumericLimitError, match="accumulation_digits"):
        left.add(BoundedRational(1, 1, 0))
    with pytest.raises(NumericLimitError, match="product_digits"):
        left.add(BoundedRational(1, 1, 1))


@pytest.mark.parametrize(
    ("numerator", "mode", "expected"),
    [
        (25, RoundingMode.HALF_EVEN, "2"),
        (35, RoundingMode.HALF_EVEN, "4"),
        (-25, RoundingMode.HALF_EVEN, "-2"),
        (-35, RoundingMode.HALF_EVEN, "-4"),
        (25, RoundingMode.HALF_UP, "3"),
        (-25, RoundingMode.HALF_UP, "-3"),
        (29, RoundingMode.TOWARD_ZERO, "2"),
        (-29, RoundingMode.TOWARD_ZERO, "-2"),
        (24, RoundingMode.HALF_UP, "2"),
        (26, RoundingMode.HALF_EVEN, "3"),
    ],
)
def test_rounding_signs_and_ties(
    numerator: int, mode: RoundingMode, expected: str
) -> None:
    value = BoundedRational(numerator, 10, 0)
    assert value.round(RoundingPolicy(0, mode)) == ExactDecimal(expected, 0, None)


def test_nonterminating_division_retains_authoritative_total() -> None:
    total = ExactDecimal.parse("10").rational()
    units = ExactDecimal.parse("3").rational()
    rate = total.divide(units)
    assert rate == BoundedRational(10, 3, 0)
    assert rate.multiply(units) == total
    with pytest.raises(ArithmeticDomainError, match="nonterminating"):
        rate.exact_decimal()
    assert rate.round(RoundingPolicy(2, RoundingMode.HALF_EVEN)) == ExactDecimal(
        "333", 2, None
    )
    with pytest.raises(ArithmeticDomainError, match="division_by_zero"):
        total.divide(BoundedRational(0, 1, 0))


def test_exact_finite_conversion_and_division_scale() -> None:
    assert BoundedRational(1, 8, 0).exact_decimal() == ExactDecimal("125", 3, None)
    assert BoundedRational(-1, 5, 2).exact_decimal() == ExactDecimal("-2", 3, None)
    assert BoundedRational(1, 2, 0).divide(
        BoundedRational(-1, 1, 2)
    ).exact_decimal() == ExactDecimal("-50", 0, None)
    assert BoundedRational(1, 1, 2).divide(
        BoundedRational(2, 1, 1)
    ).exact_decimal() == ExactDecimal("5", 2, None)


def test_rational_operations_match_independent_fraction_oracle() -> None:
    for left_numerator in range(-3, 4):
        for right_numerator in range(-3, 4):
            left = BoundedRational(left_numerator, 7, 2)
            right = BoundedRational(right_numerator, 3, 1)
            expected_left = Fraction(left_numerator, 700)
            expected_right = Fraction(right_numerator, 30)
            for actual, expected in (
                (left.add(right), expected_left + expected_right),
                (left.subtract(right), expected_left - expected_right),
                (left.multiply(right), expected_left * expected_right),
            ):
                assert (
                    Fraction(actual.numerator, actual.denominator * 10**actual.scale)
                    == expected
                )
            if right_numerator:
                quotient = left.divide(right)
                assert (
                    Fraction(
                        quotient.numerator, quotient.denominator * 10**quotient.scale
                    )
                    == expected_left / expected_right
                )


def test_residual_is_recorded_on_explicit_final_slice() -> None:
    slices = tuple(
        WeightedSlice(key, ExactDecimal("1", 0, None)) for key in ("a", "b", "c")
    )
    result = allocate_residual(
        ExactDecimal("10", 0, None), slices, RoundingPolicy(2, RoundingMode.HALF_EVEN)
    )
    assert tuple(item.amount for item in result) == (
        ExactDecimal("333", 2, None),
        ExactDecimal("333", 2, None),
        ExactDecimal("334", 2, None),
    )
    assert tuple(item.residual for item in result) == (
        ExactDecimal("0", 0, None),
        ExactDecimal("0", 0, None),
        ExactDecimal("1", 2, None),
    )
    assert result[-1].key == "c"


def test_negative_residual_and_sequential_remaining_totals() -> None:
    slices = tuple(
        WeightedSlice(key, ExactDecimal("1", 0, None))
        for key in ("first", "second", "last")
    )
    policy = RoundingPolicy(2, RoundingMode.HALF_EVEN)
    negative = allocate_residual(ExactDecimal("-10", 0, None), slices, policy)
    assert tuple(item.amount.coefficient for item in negative) == (
        "-333",
        "-333",
        "-334",
    )
    assert negative[-1].residual == ExactDecimal("-1", 2, None)
    remaining = ExactDecimal("10", 0, None).rational()
    first = remaining.divide(BoundedRational(3, 1, 0)).round(policy)
    remaining = remaining.subtract(first.rational())
    second = remaining.divide(BoundedRational(2, 1, 0)).round(policy)
    last = remaining.subtract(second.rational()).exact_decimal()
    assert (first, second, last) == (
        ExactDecimal("333", 2, None),
        ExactDecimal("334", 2, None),
        ExactDecimal("333", 2, None),
    )


def test_residual_policy_rejects_unknown_order_or_unrepresentable_total() -> None:
    one = WeightedSlice("a", ExactDecimal("1", 0, None))
    policy = RoundingPolicy(2, RoundingMode.HALF_EVEN)
    with pytest.raises(ArithmeticDomainError, match="unique_nonempty"):
        allocate_residual(ExactDecimal("1", 0, None), (one, one), policy)
    with pytest.raises(ArithmeticDomainError, match="total_not_at_rounding_scale"):
        allocate_residual(ExactDecimal("1", 3, None), (one,), policy)
    with pytest.raises(ArithmeticDomainError, match="positive_total_weight"):
        allocate_residual(
            ExactDecimal("1", 0, None),
            (WeightedSlice("zero", ExactDecimal("0", 0, None)),),
            policy,
        )


def test_residual_cannot_reverse_a_positive_allocation() -> None:
    slices = tuple(
        WeightedSlice(key, ExactDecimal("1", 0, None)) for key in ("a", "b", "c", "d")
    )
    with pytest.raises(ArithmeticDomainError, match="residual_reverses_slice_sign"):
        allocate_residual(
            ExactDecimal("2", 2, None), slices, RoundingPolicy(2, RoundingMode.HALF_UP)
        )
