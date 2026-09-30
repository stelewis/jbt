from datetime import date
from typing import cast

import pytest

from jbt.domain.canonical import (
    CanonicalValue,
    digest_record,
    encode,
    encode_json,
    encode_record,
)
from jbt.domain.errors import CanonicalError
from jbt.domain.numbers import ExactDecimal


@pytest.mark.golden
@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (None, b"n0:"),
        (False, b"b1:0"),
        (True, b"b1:1"),
        (0, b"i1:0"),
        (-12, b"i3:-12"),
        ("", b"s0:"),
        ("e\u0301", b"s2:\xc3\xa9"),
        ("a\nb", b"s3:a\nb"),
        ((1, 1, None), b"l11:i1:1i1:1n0:"),
        (date(2026, 9, 29), b"d10:2026-09-29"),
        (ExactDecimal("842", 1, 2), b"x18:l14:s3:842i1:1i1:2"),
    ],
)
def test_literal_typed_value_vectors(value: CanonicalValue, literal: bytes) -> None:
    assert encode(value) == literal


@pytest.mark.golden
def test_literal_record_and_digest_vector() -> None:
    fields = (("x", 7), ("y", None))
    literal = b"JBT\x00l36:s4:testi1:1l21:l8:s1:xi1:7l7:s1:yn0:"
    assert encode_record("test", fields) == literal
    assert digest_record("test", fields) == (
        "820678b9c1307ca5dcab559dcf3ea07dd338296b33a9b6860b099c998ec49d87"
    )


def test_domain_order_multiplicity_and_source_scale_are_significant() -> None:
    assert digest_record("a", (("x", 1),)) != digest_record("b", (("x", 1),))
    assert encode((1, 2)) != encode((2, 1))
    assert encode((1,)) != encode((1, 1))
    assert encode(ExactDecimal("1", 0, None)) != encode(ExactDecimal("1", 0, 0))
    assert encode_record("a", (("x", 1), ("y", 2))) != encode_record(
        "a", (("y", 2), ("x", 1))
    )


def test_duplicate_normalized_fields_and_invalid_unicode_fail() -> None:
    with pytest.raises(CanonicalError, match="unique_field_names"):
        encode_record("a", (("é", 1), ("e\u0301", 2)))
    with pytest.raises(CanonicalError, match="utf8_required"):
        encode("\ud800")
    with pytest.raises(CanonicalError, match="int64_required"):
        encode(2**63)
    assert encode(2**63 - 1) == b"i19:9223372036854775807"
    assert encode(-(2**63)) == b"i20:-9223372036854775808"


@pytest.mark.parametrize("value", [1.0, {"x": 1}, [1, 2]])
def test_coercions_and_mutable_containers_have_no_canonical_encoding(
    value: object,
) -> None:
    with pytest.raises(CanonicalError, match="unsupported_value_type"):
        encode(cast("CanonicalValue", value))


@pytest.mark.golden
def test_json_literal_vector_preserves_types_and_normalizes_text() -> None:
    value = {
        "z": [None, True, False, -12, "12", "e\u0301", "\n"],
        "a": {"source_scale": None, "scale": 1, "coefficient": "842"},
    }
    expected = (
        b'{"a":{"coefficient":"842","scale":1,"source_scale":null},'
        b'"z":[null,true,false,-12,"12","\xc3\xa9","\\n"]}\n'
    )
    assert encode_json(value, integer_strings=False) == expected
    assert (
        encode_json(dict(reversed(tuple(value.items()))), integer_strings=False)
        == expected
    )
    assert value["z"] == [None, True, False, -12, "12", "e\u0301", "\n"]


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        float("nan"),
        float("inf"),
        {"key": [1.5]},
        {1: "numeric-key"},
        {"é": 1, "e\u0301": 2},
        {"key": "\ud800"},
        {"\ud800": "value"},
        (1, 2),
        date(2026, 1, 1),
        ExactDecimal("1", 0, None),
    ],
)
def test_json_rejects_lossy_or_nonjson_values(value: object) -> None:
    with pytest.raises(CanonicalError):
        encode_json(value, integer_strings=False)


def test_json_rejects_cycles_but_preserves_shared_list_multiplicity() -> None:
    shared = [1, 2]
    assert encode_json([shared, shared], integer_strings=False) == b"[[1,2],[1,2]]\n"
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(CanonicalError, match="cyclic_json_container"):
        encode_json(cycle, integer_strings=False)
    cyclic_mapping: dict[str, object] = {}
    cyclic_mapping["cycle"] = cyclic_mapping
    with pytest.raises(CanonicalError, match="cyclic_json_container"):
        encode_json(cyclic_mapping, integer_strings=False)


def test_json_integer_and_nesting_bounds() -> None:
    assert encode_json([-(2**63), 2**63 - 1], integer_strings=False) == (
        b"[-9223372036854775808,9223372036854775807]\n"
    )
    with pytest.raises(CanonicalError, match="int64_required"):
        encode_json(2**63, integer_strings=False)
    with pytest.raises(CanonicalError, match="int64_required"):
        encode_json(-(2**63) - 1, integer_strings=False)
    nested: object = None
    for _ in range(64):
        nested = [nested]
    assert (
        encode_json(nested, integer_strings=False)
        == b"[" * 64 + b"null" + b"]" * 64 + b"\n"
    )
    with pytest.raises(CanonicalError, match="json_nesting_limit"):
        encode_json([nested], integer_strings=False)


def test_json_row_integer_strings_do_not_coerce_booleans_or_nulls() -> None:
    assert encode_json([0, -1, True, False, None, "2"], integer_strings=True) == (
        b'["0","-1",true,false,null,"2"]\n'
    )
