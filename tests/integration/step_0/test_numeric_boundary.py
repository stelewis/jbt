from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from tests.integration.step_0.consumer_arithmetic import (
    Number,
    NumericError,
    Ratio,
    Rounding,
    add,
    decimal_preflight,
    divide,
    multiply,
    read_number,
    register_arithmetic,
    round_ratio,
)

pytestmark = pytest.mark.integration


def _connection(*, allow_local_files: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        config={
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "allow_community_extensions": "false",
            "enable_external_access": "true" if allow_local_files else "false",
        }
    )


def test_parquet_round_trip_preserves_physical_and_empty_types(tmp_path: Path) -> None:
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("day", pa.date32()),
            ("known", pa.bool_()),
            ("amount_coefficient", pa.string()),
            ("amount_scale", pa.int32()),
            ("amount_source_scale", pa.int32()),
            ("links", pa.list_(pa.string())),
        ]
    )
    rows = [
        {
            "id": "synthetic-a",
            "day": date(2025, 1, 2),
            "known": True,
            "amount_coefficient": "-842",
            "amount_scale": 1,
            "amount_source_scale": 2,
            "links": ["b", "a"],
        },
        {
            "id": "synthetic-b",
            "day": None,
            "known": False,
            "amount_coefficient": None,
            "amount_scale": None,
            "amount_source_scale": None,
            "links": [],
        },
        {
            "id": "synthetic-c",
            "day": date(2025, 1, 3),
            "known": True,
            "amount_coefficient": "9" * 96,
            "amount_scale": 38,
            "amount_source_scale": 38,
            "links": None,
        },
    ]
    populated, empty = tmp_path / "values.parquet", tmp_path / "empty.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), populated)
    pq.write_table(pa.Table.from_pylist([], schema=schema), empty)
    with _connection(allow_local_files=True) as connection:
        connection.execute("SET allowed_paths = ?", [[str(populated), str(empty)]])
        connection.execute("SET enable_external_access = false")
        result = connection.execute(
            "SELECT * FROM read_parquet(?) ORDER BY id", [str(populated)]
        )
        expected_types = [
            "VARCHAR",
            "DATE",
            "BOOLEAN",
            "VARCHAR",
            "INTEGER",
            "INTEGER",
            "VARCHAR[]",
        ]
        assert [str(column[1]) for column in result.description] == expected_types
        loaded = result.fetchall()
        assert loaded == [tuple(row.values()) for row in rows]
        for row in loaded:
            read_number(row[3], row[4], row[5])
        result = connection.execute("SELECT * FROM read_parquet(?)", [str(empty)])
        assert [str(column[1]) for column in result.description] == expected_types
        assert result.fetchall() == []
        with pytest.raises(duckdb.PermissionException, match="file system operations"):
            connection.execute("SELECT * FROM read_parquet('unlisted.parquet')")


@pytest.mark.parametrize(
    ("coefficient", "scale", "source_scale", "expected"),
    [
        ("842", 1, 2, Number(842, 1)),
        ("0", 0, 2, Number(0, 0)),
        ("-" + "9" * 96, 38, 38, Number(-int("9" * 96), 38)),
        (str(2**256 - 1), 0, None, Number(2**256 - 1, 0)),
        ("1", 38, None, Number(1, 38)),
        (None, None, None, None),
    ],
)
def test_stored_number_admission(
    coefficient: str | None,
    scale: int | None,
    source_scale: int | None,
    expected: Number | None,
) -> None:
    assert read_number(coefficient, scale, source_scale) == expected


@pytest.mark.parametrize(
    ("coefficient", "scale", "source_scale"),
    [
        ("9" * 97, 0, None),
        ("1", 39, None),
        ("1", 0, 39),
        ("-0", 0, None),
        ("01", 0, None),
        ("+1", 0, None),
        ("\u0661", 0, None),
        ("1e3", 0, None),
        ("1", -1, None),
        ("1", True, None),
        ("10", 1, None),
        ("0", 1, None),
        (None, 0, None),
        ("1", None, None),
        (None, None, 2),
    ],
)
def test_invalid_numeric_groups_fail(
    coefficient: str | None, scale: int | None, source_scale: int | None
) -> None:
    with pytest.raises(NumericError):
        read_number(coefficient, scale, source_scale)


def test_work_profile_admits_four_factors_not_five() -> None:
    factor = Number(int("9" * 96), 38)
    product = multiply(multiply(multiply(factor, factor), factor), factor)
    assert product == Number((10**96 - 1) ** 4, 152)
    assert len(str(product.coefficient)) == 384
    with pytest.raises(NumericError, match="scale limit"):
        multiply(product, factor)
    with pytest.raises(NumericError, match="product limit"):
        multiply(Number(product.coefficient, 0), Number(factor.coefficient, 0))
    with pytest.raises(NumericError, match="storage coefficient limit"):
        product.stored()
    with pytest.raises(NumericError, match="scale limit"):
        Number(1, 39).stored()
    with pytest.raises(NumericError, match="coefficient limit"):
        Number(10**384, 0)
    with pytest.raises(NumericError, match="scale limit"):
        Number(1, 153)
    assert multiply(Number(int("9" * 384), 0), Number(1, 0)) == Number(
        int("9" * 384), 0
    )


def test_addition_checks_alignment_and_group_size_before_accumulating() -> None:
    assert add([Number(842, 1), Number(-2, 0), Number(1, 2)]) == Number(8221, 2)
    assert add([]) == Number(0, 0)
    assert add([Number(0, 0), Number(0, 0)]) == Number(0, 0)
    assert add([Number(10**382, 0)] * 10) == Number(10**383, 0)
    with pytest.raises(NumericError, match="accumulation limit"):
        add([Number(10**382, 0)] * 11)
    with pytest.raises(NumericError, match="accumulation limit"):
        add([Number(10**383, 0), Number(-(10**383), 0)])
    with pytest.raises(NumericError, match="accumulation limit"):
        add([Number(10**383, 0), Number(1, 152)])
    assert multiply(Number(-12, 1), Number(25, 1)) == Number(-3, 0)


@pytest.mark.parametrize(
    ("mode", "input_coefficient", "expected"),
    [
        (Rounding.HALF_EVEN, 1005, 100),
        (Rounding.HALF_EVEN, 1015, 102),
        (Rounding.HALF_EVEN, -1005, -100),
        (Rounding.HALF_EVEN, -1015, -102),
        (Rounding.HALF_UP, 1005, 101),
        (Rounding.HALF_UP, -1005, -101),
        (Rounding.TOWARD_ZERO, 1019, 101),
        (Rounding.TOWARD_ZERO, -1019, -101),
    ],
)
def test_rounding_is_explicit_and_sign_symmetric(
    mode: Rounding, input_coefficient: int, expected: int
) -> None:
    assert round_ratio(Ratio(input_coefficient, 1000), 2, mode) == Number(expected, 2)


@pytest.mark.parametrize("sign", [1, -1])
def test_rational_reductions_conserve_authoritative_total(sign: int) -> None:
    total = Number(sign * 10, 0)
    assert divide(total, Number(3, 0)) == Ratio(sign * 10, 3)
    slices = []
    for units in (3, 2):
        allocated = round_ratio(divide(total, Number(units, 0)), 2, Rounding.HALF_EVEN)
        slices.append(allocated)
        total = add([total, Number(-allocated.coefficient, allocated.scale)])
    slices.append(total)  # The policy explicitly selects the final slice.
    assert slices == [
        Number(sign * 333, 2),
        Number(sign * 334, 2),
        Number(sign * 333, 2),
    ]
    assert add(slices) == Number(sign * 10, 0)
    assert divide(Number(-10, 0), Number(-3, 0)) == Ratio(10, 3)
    assert round_ratio(Ratio(1, 3), 2, Rounding.HALF_EVEN) == Number(33, 2)


def test_rational_bounds_and_cancellation() -> None:
    assert divide(Number(10**383, 0), Number(10**383, 152)) == Ratio(10**152, 1)
    assert divide(Number(1, 152), Number(1, 0)) == Ratio(1, 10**152)
    assert round_ratio(Ratio(1, 10**152), 152, Rounding.HALF_EVEN) == Number(1, 152)
    with pytest.raises(NumericError, match="division by zero"):
        divide(Number(1, 0), Number(0, 0))
    for denominator in (0, -1):
        with pytest.raises(NumericError, match="denominator must be positive"):
            Ratio(1, denominator)
    with pytest.raises(NumericError, match="division alignment limit"):
        divide(Number(10**383, 0), Number(1, 152))
    with pytest.raises(NumericError, match="product limit"):
        round_ratio(Ratio(10**383, 3), 2, Rounding.HALF_EVEN)
    assert Ratio(int("9" * 384), int("8" * 384)) == Ratio(9, 8)
    assert round_ratio(Ratio(int("9" * 384), 1), 0, Rounding.HALF_EVEN) == Number(
        int("9" * 384), 0
    )
    with pytest.raises(NumericError, match="coefficient limit"):
        Ratio(1, 10**384)


def test_sql_udfs_have_explicit_types_nulls_errors_and_connection_scope() -> None:
    with _connection() as connection:
        register_arithmetic(connection)
        signatures = connection.execute(
            """SELECT function_name, parameter_types, return_type
            FROM duckdb_functions()
            WHERE starts_with(function_name, '_integer_exact_')
            ORDER BY function_name"""
        ).fetchall()
        numeric_type = "STRUCT(coefficient VARCHAR, scale INTEGER)"
        rational_type = "STRUCT(numerator VARCHAR, denominator VARCHAR)"
        arguments = ["VARCHAR", "INTEGER", "VARCHAR", "INTEGER"]
        assert signatures == [
            ("_integer_exact_add", arguments, numeric_type),
            ("_integer_exact_divide", arguments, rational_type),
            ("_integer_exact_multiply", arguments, numeric_type),
            (
                "_integer_exact_round",
                ["VARCHAR", "VARCHAR", "INTEGER", "VARCHAR"],
                numeric_type,
            ),
        ]
        result = connection.execute(
            """SELECT exact_add('842', 1, '-2', 0),
            exact_multiply('100000000000', 0, '1000000000', 0),
            exact_divide('10', 0, '3', 0),
            exact_round('-201', '200', 2, 'half_up')"""
        )
        assert [str(column[1]) for column in result.description] == [
            numeric_type,
            numeric_type,
            rational_type,
            numeric_type,
        ]
        assert result.fetchone() == (
            {"coefficient": "822", "scale": 1},
            {"coefficient": "100000000000000000000", "scale": 0},
            {"numerator": "10", "denominator": "3"},
            {"coefficient": "-101", "scale": 2},
        )
        assert connection.execute(
            """SELECT exact_add(NULL, 0, '1', 0),
            exact_multiply('1', NULL, '2', 0),
            exact_divide('1', 0, NULL, 0),
            exact_round('1', '3', 2, NULL)"""
        ).fetchone() == (None, None, None, None)
        with pytest.raises(duckdb.InvalidInputException, match="division by zero"):
            connection.execute("SELECT exact_divide('1', 0, '0', 0)").fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="noncanonical"):
            connection.execute("SELECT exact_add('-0', 0, '1', 0)").fetchall()
        with pytest.raises(duckdb.BinderException):
            connection.execute("SELECT exact_add(1.5, 0, '1', 0)").fetchall()
        with pytest.raises(duckdb.NotImplementedException, match="already created"):
            register_arithmetic(connection)
        with _connection() as other:
            with pytest.raises(duckdb.CatalogException, match="does not exist"):
                other.execute("SELECT exact_divide('1', 0, '3', 0)")
            register_arithmetic(other)
            assert other.execute("SELECT exact_divide('1', 0, '3', 0)").fetchone() == (
                {"numerator": "1", "denominator": "3"},
            )


def test_checked_sql_decimal_operations_remain_exact() -> None:
    decimal_preflight([Number(842, 1), Number(-2, 0)], "add", precision=18, scale=2)
    decimal_preflight(
        [Number(-125, 2), Number(2, 0)], "multiply", precision=18, scale=2
    )
    decimal_preflight([Number(125, 2)] * 3, "sum", precision=18, scale=2)
    decimal_preflight([Number(10**36, 0)] * 10, "sum", precision=38, scale=0)
    with _connection() as connection:
        result = connection.execute(
            """SELECT
            84.20::DECIMAL(18,2) + (-2)::DECIMAL(18,2),
            (-1.25)::DECIMAL(18,2) * 2::DECIMAL(18,2),
            (SELECT sum(v) FROM (VALUES (1.25::DECIMAL(18,2)),
            (1.25::DECIMAL(18,2)), (1.25::DECIMAL(18,2))) AS amounts(v))"""
        )
        assert [str(column[1]) for column in result.description] == [
            "DECIMAL(18,2)",
            "DECIMAL(18,4)",
            "DECIMAL(38,2)",
        ]
        assert result.fetchone() == (
            Decimal("82.20"),
            Decimal("-2.5000"),
            Decimal("3.75"),
        )
        result = connection.execute(
            "SELECT sum(?::DECIMAL(38,0)) FROM range(10)", [str(10**36)]
        )
        assert str(result.description[0][1]) == "DECIMAL(38,0)"
        assert result.fetchone() == (Decimal("1" + "0" * 37),)
        assert connection.execute(
            "SELECT sum(NULL::DECIMAL(38,2)), sum(0::DECIMAL(38,2)) WHERE false"
        ).fetchone() == (None, None)


def test_sql_wide_results_preserve_limits_and_reject_invalid_inputs() -> None:
    with _connection() as connection:
        register_arithmetic(connection)
        result = connection.execute(
            """WITH squared AS (
                SELECT exact_multiply(?, 38, ?, 38) AS n
            )
            SELECT exact_multiply(
                n.coefficient, n.scale, n.coefficient, n.scale
            ) FROM squared""",
            ["9" * 96, "9" * 96],
        ).fetchone()
        assert result == ({"coefficient": str((10**96 - 1) ** 4), "scale": 152},)
        assert connection.execute(
            "SELECT exact_round('1', ?, 152, 'half_even')", [str(10**152)]
        ).fetchone() == ({"coefficient": "1", "scale": 152},)
        with pytest.raises(duckdb.InvalidInputException, match="coefficient limit"):
            connection.execute("SELECT exact_add(?, 0, '1', 0)", ["9" * 385]).fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="scale limit"):
            connection.execute("SELECT exact_multiply('1', 152, '1', 1)").fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="product limit"):
            connection.execute(
                "SELECT exact_multiply(?, 0, '2', 0)", ["9" * 384]
            ).fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="Rounding"):
            connection.execute(
                "SELECT exact_round('1', '3', 2, 'approximate')"
            ).fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="positive"):
            connection.execute(
                "SELECT exact_round('1', '-3', 2, 'half_even')"
            ).fetchall()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT exact_add(1.5::DOUBLE, 0, '1', 0)",
        "SELECT exact_add('1', 1.5::DOUBLE, '1', 0)",
        "SELECT exact_multiply('1', true, '2', 0)",
        "SELECT exact_divide('1', 0, '3', 1.5::DECIMAL(2,1))",
        "SELECT exact_round('1', '3', 1.5::DOUBLE, 'half_even')",
    ],
)
def test_sql_rejects_noninteger_scales_and_approximate_coefficients(query: str) -> None:
    with _connection() as connection:
        register_arithmetic(connection)
        with pytest.raises(duckdb.BinderException):
            connection.execute(query).fetchall()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT exact_add('1', '1.5', '1', 0)",
        "SELECT exact_multiply('1', '1', '2', 0)",
        "SELECT exact_divide('1', 0, '3', '0.5')",
        "SELECT exact_round('1', '3', '2.5', 'half_even')",
    ],
)
def test_sql_rejects_literal_scale_coercion_before_udf(query: str) -> None:
    with _connection() as connection:
        register_arithmetic(connection)
        with pytest.raises(duckdb.InvalidInputException, match="argument type"):
            connection.execute(query).fetchall()


@pytest.mark.parametrize(
    ("coefficient", "scale"),
    [(True, 0), (1.25, 0), (float("nan"), 0), (1, True), (1, 1.5)],
)
def test_python_numbers_reject_noninteger_inputs(coefficient: int, scale: int) -> None:
    with pytest.raises(NumericError):
        Number(coefficient, scale)


def test_sql_scale_alignment_never_normalizes_invalid_wire_values() -> None:
    with _connection() as connection:
        register_arithmetic(connection)
        with pytest.raises(duckdb.InvalidInputException, match="noncanonical numeric"):
            connection.execute("SELECT exact_add('10', 1, '1', 0)").fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="noncanonical numeric"):
            connection.execute("SELECT exact_multiply('0', 1, '1', 0)").fetchall()
        with pytest.raises(duckdb.InvalidInputException, match="argument type"):
            connection.execute("SELECT exact_add('1', ?, '1', 0)", ["1.5"]).fetchall()


def test_decimal_admission_checks_product_scale_and_whole_group_bound() -> None:
    decimal_preflight([Number(10**36, 0)] * 10, "sum", precision=38, scale=0)
    with pytest.raises(NumericError, match="decimal expression limit"):
        decimal_preflight([Number(10**36, 0)] * 11, "sum", precision=38, scale=0)
    decimal_preflight([Number(1, 2), Number(1, 2)], "multiply", precision=18, scale=2)
    with pytest.raises(NumericError, match="decimal product scale limit"):
        decimal_preflight(
            [Number(1, 20), Number(1, 20)], "multiply", precision=38, scale=20
        )
    with pytest.raises(NumericError, match="decimal operand limit"):
        decimal_preflight(
            [Number(10**38, 0), Number(1, 0)], "add", precision=38, scale=0
        )
    with pytest.raises(NumericError, match="decimal expression limit"):
        decimal_preflight(
            [Number(10**37, 0), Number(-1, 0)], "add", precision=38, scale=0
        )


def test_decimal_preflight_counts_native_multiplication_scale() -> None:
    # Both inputs and the normalized answer fit; the SQL scale-36 product does not.
    with pytest.raises(NumericError, match="decimal expression limit"):
        decimal_preflight(
            [Number(100, 0), Number(1, 0)], "multiply", precision=38, scale=18
        )
    with _connection() as connection, pytest.raises(duckdb.OutOfRangeException):
        connection.execute("SELECT 100::DECIMAL(38,18) * 1::DECIMAL(38,18)").fetchall()


def test_unsafe_decimal_expression_chooses_integer_path_before_execution() -> None:
    operands = [Number(10**11, 0), Number(10**9, 0)]
    with pytest.raises(NumericError, match="decimal expression limit"):
        decimal_preflight(operands, "multiply", precision=38, scale=18)
    with pytest.raises(NumericError, match="decimal cast would round"):
        decimal_preflight([Number(1, 3), Number(1, 0)], "add", precision=18, scale=2)
    with pytest.raises(NumericError, match="decimal expression limit"):
        decimal_preflight([Number(10**37, 0)] * 11, "sum", precision=38, scale=0)
    with _connection() as connection:
        register_arithmetic(connection)
        assert connection.execute(
            "SELECT exact_multiply('100000000000', 0, '1000000000', 0)"
        ).fetchone() == ({"coefficient": "100000000000000000000", "scale": 0},)
        # A deliberate negative probe, never an overflow-driven fallback.
        with pytest.raises(duckdb.OutOfRangeException):
            connection.execute(
                "SELECT 100000000000::DECIMAL(38,18) * 1000000000::DECIMAL(38,18)"
            ).fetchall()
