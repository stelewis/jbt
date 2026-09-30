import pytest

from jbt.contracts.primitives import (
    ContractError,
    number,
    require,
    validate_json_values,
    validate_number,
)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (
            {"coefficient": "-0", "scale": 0, "source_scale": None},
            "decimal_coefficient",
        ),
        (
            {"coefficient": "01", "scale": 0, "source_scale": None},
            "decimal_coefficient",
        ),
        (
            {"coefficient": "1" * 97, "scale": 0, "source_scale": None},
            "decimal_coefficient",
        ),
        ({"coefficient": "1", "scale": 39, "source_scale": None}, "decimal_scale"),
        ({"coefficient": "1", "scale": 0, "source_scale": 39}, "decimal_source_scale"),
        (
            {"coefficient": "0", "scale": 1, "source_scale": None},
            "decimal_normalization",
        ),
        (
            {"coefficient": "10", "scale": 1, "source_scale": None},
            "decimal_normalization",
        ),
        ({"coefficient": None, "scale": 0, "source_scale": None}, "decimal_null_group"),
        ({"coefficient": "1", "scale": True, "source_scale": None}, "decimal_scale"),
    ],
)
def test_numeric_encoding_failure_cases(value: dict, code: str) -> None:
    with pytest.raises(ContractError, match=code):
        validate_number(value, "test.amount")


@pytest.mark.parametrize(
    "value",
    [
        {"coefficient": "1" * 96, "scale": 38, "source_scale": 38},
        {"coefficient": "842", "scale": 1, "source_scale": 2},
        {"coefficient": "0", "scale": 0, "source_scale": 2},
        {"coefficient": None, "scale": None, "source_scale": None},
    ],
)
def test_numeric_encoding_boundary_success(value: dict) -> None:
    validate_number(value, "test.amount")
    row = {f"amount_{key}": item for key, item in value.items()}
    result = number(row, "amount")
    assert (result is None) == (value["coefficient"] is None)


def test_failure_has_safe_structured_location() -> None:
    with pytest.raises(ContractError) as caught:
        require(
            condition=False, code="missing_reference", location="postings[0].account_id"
        )
    assert caught.value.code == "missing_reference"
    assert caught.value.location == "postings[0].account_id"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1.0, "json_float"),
        (2**63, "json_integer_range"),
        ({"name": "e\u0301"}, "text_nfc"),
        ({1: "value"}, "json_object_key"),
        (
            {"amount": {"coefficient": "10", "scale": 1, "source_scale": None}},
            "decimal_normalization",
        ),
    ],
)
def test_json_values_are_strictly_typed(value: object, code: str) -> None:
    with pytest.raises(ContractError, match=code):
        validate_json_values(value, "test")
