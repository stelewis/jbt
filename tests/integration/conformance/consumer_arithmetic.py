"""Independent bounded arithmetic; no producer code or decimal context."""

from dataclasses import dataclass
from enum import StrEnum
from math import gcd
from re import fullmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from duckdb import DuckDBPyConnection

STORED_DIGITS = 96
STORED_SCALE = 38
WORK_DIGITS = 384
WORK_SCALE = 152


class NumericError(ValueError):
    """An invalid representation or inadmissible exact operation."""


class Rounding(StrEnum):
    HALF_EVEN = "half_even"
    HALF_UP = "half_up"
    TOWARD_ZERO = "toward_zero"


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise NumericError(message)


def _scale(value: int, maximum: int) -> None:
    _require(type(value) is int and 0 <= value <= maximum, "scale limit")


def _digits(value: int) -> int:
    _require(
        type(value) is int and -(10**WORK_DIGITS) < value < 10**WORK_DIGITS,
        "coefficient limit",
    )
    return len(str(abs(value)))


def _expand(value: int, places: int) -> int:
    _scale(places, WORK_SCALE)
    _require(value == 0 or _digits(value) + places <= WORK_DIGITS, "alignment limit")
    return value * 10**places


def _product(left: int, right: int) -> int:
    _require(
        right == 0 or abs(left) <= (10**WORK_DIGITS - 1) // abs(right),
        "product limit",
    )
    return left * right


@dataclass(frozen=True)
class Number:
    coefficient: int
    scale: int

    def __post_init__(self) -> None:
        _digits(self.coefficient)
        _scale(self.scale, WORK_SCALE)
        coefficient, scale = self.coefficient, self.scale
        while scale and coefficient % 10 == 0:
            coefficient //= 10
            scale -= 1
        object.__setattr__(self, "coefficient", coefficient)
        object.__setattr__(self, "scale", scale)

    def stored(self) -> Number:
        _require(
            _digits(self.coefficient) <= STORED_DIGITS, "storage coefficient limit"
        )
        _scale(self.scale, STORED_SCALE)
        return self

    def wire(self) -> dict[str, str | int]:
        return {"coefficient": str(self.coefficient), "scale": self.scale}


def read_number(
    coefficient: str | None, scale: int | None, source_scale: int | None
) -> Number | None:
    """Validate a stored nullable group before parsing any integer."""
    if coefficient is None:
        _require(scale is None and source_scale is None, "partial null numeric group")
        return None
    _require(type(coefficient) is str, "coefficient must be text")
    _require(
        len(coefficient.removeprefix("-")) <= STORED_DIGITS, "storage coefficient limit"
    )
    _require(
        fullmatch(r"(?:0|-?[1-9][0-9]*)", coefficient) is not None,
        "noncanonical coefficient",
    )
    _require(scale is not None, "partial null numeric group")
    assert scale is not None
    _scale(scale, STORED_SCALE)
    if source_scale is not None:
        _scale(source_scale, STORED_SCALE)
    number = Number(int(coefficient), scale)
    _require(
        str(number.coefficient) == coefficient and number.scale == scale,
        "noncanonical numeric value",
    )
    return number


def _read_work(coefficient: str, scale: int) -> Number:
    _require(type(coefficient) is str, "coefficient must be text")
    _require(len(coefficient.removeprefix("-")) <= WORK_DIGITS, "coefficient limit")
    _require(
        fullmatch(r"(?:0|-?[1-9][0-9]*)", coefficient) is not None,
        "noncanonical coefficient",
    )
    _scale(scale, WORK_SCALE)
    number = Number(int(coefficient), scale)
    _require(
        str(number.coefficient) == coefficient and number.scale == scale,
        "noncanonical numeric value",
    )
    return number


def add(values: Sequence[Number]) -> Number:
    if not values:
        return Number(0, 0)
    scale = max(value.scale for value in values)
    aligned_digits = (
        max(
            _digits(value.coefficient) + scale - value.scale
            for value in values
            if value.coefficient
        )
        if any(value.coefficient for value in values)
        else 1
    )
    carry = len(str(len(values) - 1)) if len(values) > 1 else 0
    _require(aligned_digits + carry <= WORK_DIGITS, "accumulation limit")
    total = sum(_expand(value.coefficient, scale - value.scale) for value in values)
    return Number(total, scale)


def multiply(left: Number, right: Number) -> Number:
    scale = left.scale + right.scale
    _scale(scale, WORK_SCALE)
    return Number(_product(left.coefficient, right.coefficient), scale)


@dataclass(frozen=True)
class Ratio:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _digits(self.numerator)
        _digits(self.denominator)
        _require(self.denominator > 0, "denominator must be positive")
        common = gcd(self.numerator, self.denominator)
        object.__setattr__(self, "numerator", self.numerator // common)
        object.__setattr__(self, "denominator", self.denominator // common)

    def wire(self) -> dict[str, str]:
        return {"numerator": str(self.numerator), "denominator": str(self.denominator)}


def divide(left: Number, right: Number) -> Ratio:
    _require(right.coefficient != 0, "division by zero")
    numerator, denominator = left.coefficient, right.coefficient
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    common = gcd(numerator, denominator)
    numerator, denominator = numerator // common, denominator // common
    places = right.scale - left.scale
    _scale(abs(places), WORK_SCALE)
    # Cancel the alignment factor before admitting the expanded product.
    factor = 10 ** abs(places)
    if places >= 0:
        common = gcd(denominator, factor)
        denominator //= common
        factor //= common
        _require(
            numerator == 0 or _digits(numerator) + _digits(factor) - 1 <= WORK_DIGITS,
            "division alignment limit",
        )
        numerator = _product(numerator, factor)
    else:
        common = gcd(numerator, factor)
        numerator //= common
        factor //= common
        _require(
            _digits(denominator) + _digits(factor) - 1 <= WORK_DIGITS,
            "division alignment limit",
        )
        denominator = _product(denominator, factor)
    return Ratio(numerator, denominator)


def round_ratio(value: Ratio, scale: int, mode: Rounding) -> Number:
    _scale(scale, WORK_SCALE)
    _require(isinstance(mode, Rounding), "unknown rounding mode")
    factor = 10**scale
    common = gcd(value.denominator, factor)
    denominator, factor = value.denominator // common, factor // common
    quotient, remainder = divmod(_product(abs(value.numerator), factor), denominator)
    # Compare without doubling a potentially full-width remainder.
    above_half = remainder > denominator - remainder
    tie = remainder == denominator - remainder
    if (mode is Rounding.HALF_UP and (above_half or tie)) or (
        mode is Rounding.HALF_EVEN and (above_half or (tie and quotient % 2 == 1))
    ):
        quotient += 1
    if value.numerator < 0:
        quotient = -quotient
    return Number(quotient, scale)


def decimal_preflight(
    values: Sequence[Number], operation: str, *, precision: int, scale: int
) -> None:
    """Check operands cast to DECIMAL(precision, scale) and their expression.

    Bound the result by the same precision conservatively. Multiplication
    doubles the SQL scale, even when the normalized operands are integers.
    """
    _require(type(precision) is int and 1 <= precision <= 38, "decimal precision limit")
    _scale(scale, precision)
    _require(operation in {"add", "multiply", "sum"}, "unknown decimal operation")
    _require(bool(values), "empty decimal operands")
    _require(operation == "sum" or len(values) == 2, "decimal operand count")
    for value in values:
        _require(value.scale <= scale, "decimal cast would round")
        _require(
            value.coefficient == 0
            or _digits(value.coefficient) + scale - value.scale <= precision,
            "decimal operand limit",
        )
    if operation == "multiply":
        _require(2 * scale <= precision, "decimal product scale limit")
        bound = sum(
            _digits(value.coefficient) + scale - value.scale for value in values
        )
    else:
        bound = max(
            _digits(value.coefficient) + scale - value.scale for value in values
        )
        bound += len(str(len(values) - 1)) if len(values) > 1 else 0
    _require(bound <= precision, "decimal expression limit")


def register_arithmetic(connection: DuckDBPyConnection) -> None:
    """Register typed UDFs with SQL guards against literal coercion.

    Call the public ``exact_*`` macros, not their ``_integer_*`` kernels.
    DuckDB otherwise silently rounds quoted fractional scale literals
    before invoking a native UDF with an INTEGER parameter.
    """
    from duckdb import PythonExceptionHandling, struct_type  # noqa: PLC0415
    from duckdb.func import FunctionNullHandling  # noqa: PLC0415
    from duckdb.sqltypes import INTEGER, VARCHAR  # noqa: PLC0415

    numeric_type = struct_type({"coefficient": VARCHAR, "scale": INTEGER})
    rational_type = struct_type({"numerator": VARCHAR, "denominator": VARCHAR})

    def exact_multiply(
        left: str, left_scale: int, right: str, right_scale: int
    ) -> dict[str, str | int]:
        return multiply(
            _read_work(left, left_scale), _read_work(right, right_scale)
        ).wire()

    def exact_add(
        left: str, left_scale: int, right: str, right_scale: int
    ) -> dict[str, str | int]:
        return add(
            [_read_work(left, left_scale), _read_work(right, right_scale)]
        ).wire()

    def exact_divide(
        left: str, left_scale: int, right: str, right_scale: int
    ) -> dict[str, str]:
        return divide(
            _read_work(left, left_scale), _read_work(right, right_scale)
        ).wire()

    def exact_round(
        numerator: str, denominator: str, scale: int, mode: str
    ) -> dict[str, str | int]:
        ratio = Ratio(
            _read_work(numerator, 0).coefficient,
            _read_work(denominator, 0).coefficient,
        )
        return round_ratio(ratio, scale, Rounding(mode)).wire()

    for name, implementation, result_type in (
        ("exact_add", exact_add, numeric_type),
        ("exact_multiply", exact_multiply, numeric_type),
        ("exact_divide", exact_divide, rational_type),
    ):
        connection.create_function(
            "_integer_" + name,
            implementation,
            [VARCHAR, INTEGER, VARCHAR, INTEGER],
            result_type,
            null_handling=FunctionNullHandling.DEFAULT,
            exception_handling=PythonExceptionHandling.DEFAULT,
            side_effects=False,
        )
        # Names and SQL are fixed implementation constants, never caller input.
        connection.execute(
            f"""CREATE MACRO {name}(a, s, b, t) AS
            CASE WHEN (a IS NOT NULL AND typeof(a) <> 'VARCHAR')
                   OR (s IS NOT NULL AND typeof(s) <> 'INTEGER')
                   OR (b IS NOT NULL AND typeof(b) <> 'VARCHAR')
                   OR (t IS NOT NULL AND typeof(t) <> 'INTEGER')
            THEN error('exact arithmetic argument type')
            ELSE _integer_{name}(a, s, b, t) END"""
        )
    connection.create_function(
        "_integer_exact_round",
        exact_round,
        [VARCHAR, VARCHAR, INTEGER, VARCHAR],
        numeric_type,
        null_handling=FunctionNullHandling.DEFAULT,
        exception_handling=PythonExceptionHandling.DEFAULT,
        side_effects=False,
    )
    connection.execute(
        """CREATE MACRO exact_round(n, d, s, m) AS
        CASE WHEN (n IS NOT NULL AND typeof(n) <> 'VARCHAR')
               OR (d IS NOT NULL AND typeof(d) <> 'VARCHAR')
               OR (s IS NOT NULL AND typeof(s) <> 'INTEGER')
               OR (m IS NOT NULL AND typeof(m) <> 'VARCHAR')
        THEN error('exact arithmetic argument type')
        ELSE _integer_exact_round(n, d, s, m) END"""
    )
