"""Bounded scalar checks shared by table and declaration boundaries."""

import re
import unicodedata
from fractions import Fraction

SCALE_LIMIT = 38


class ContractError(ValueError):
    """A named boundary failure without financial payloads in diagnostics."""

    def __init__(self, code: str, location: str) -> None:
        """Retain machine-readable failure identity and safe schema location."""
        self.code = code
        self.location = location
        super().__init__(f"{code}: {location}")


def require(condition: bool, code: str, location: str) -> None:  # noqa: FBT001
    """Reject an invariant without coercion or repair."""
    if not condition:
        raise ContractError(code, location)


def number(row: dict, field: str) -> Fraction | None:
    """Read a validated exact group; preserve unknown rather than zero."""
    coefficient = row[f"{field}_coefficient"]
    if coefficient is None:
        return None
    return Fraction(int(coefficient), 10 ** row[f"{field}_scale"])


def validate_number(value: dict, location: str) -> None:
    """Validate bounded canonical coefficient/scale and independent precision."""
    coefficient, scale, source = (
        value["coefficient"],
        value["scale"],
        value["source_scale"],
    )
    if coefficient is None:
        require(scale is None and source is None, "decimal_null_group", location)
        return
    require(
        isinstance(coefficient, str)
        and re.fullmatch(r"(0|-?[1-9][0-9]{0,95})", coefficient) is not None,
        "decimal_coefficient",
        location,
    )
    require(type(scale) is int and 0 <= scale <= SCALE_LIMIT, "decimal_scale", location)
    require(
        source is None or (type(source) is int and 0 <= source <= SCALE_LIMIT),
        "decimal_source_scale",
        location,
    )
    require(
        not (scale > 0 and coefficient.endswith("0")), "decimal_normalization", location
    )


def validate_json_values(value: object, location: str) -> None:
    """Reject floats, non-NFC text, and noncanonical embedded decimal groups."""
    require(type(value) is not float, "json_float", location)
    if isinstance(value, str):
        require(unicodedata.normalize("NFC", value) == value, "text_nfc", location)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            code = "text_utf8"
            raise ContractError(code, location) from error
    elif type(value) is int:
        require(-(2**63) <= value < 2**63, "json_integer_range", location)
    elif isinstance(value, list):
        for item in value:
            validate_json_values(item, location)
    elif isinstance(value, dict):
        if set(value) == {"coefficient", "scale", "source_scale"}:
            validate_number(value, location)
        for key, item in value.items():
            require(isinstance(key, str), "json_object_key", location)
            validate_json_values(key, location)
            validate_json_values(item, f"{location}.{key}")
