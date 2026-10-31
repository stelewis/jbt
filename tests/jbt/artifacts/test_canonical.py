from jbt.artifacts.canonical import table_digest


def test_table_digest_distinguishes_type_order_null_and_multiplicity() -> None:
    columns = [{"name": "amount", "kind": "integer", "nullable": True}]
    rows = [{"amount": 0}, {"amount": None}]
    expected = table_digest(columns, rows, schema_version=1)
    assert expected != table_digest(columns, rows[::-1], schema_version=1)
    assert expected != table_digest(columns, rows + rows, schema_version=1)
    assert expected != table_digest(columns, [{"amount": 0}], schema_version=1)
    assert expected != table_digest(columns, rows, schema_version=2)
    assert expected != table_digest(
        [{"name": "amount", "kind": "string", "nullable": True}],
        rows,
        schema_version=1,
    )


def test_table_digest_preserves_exact_descriptive_text() -> None:
    columns = [
        {
            "name": "narration",
            "kind": "string",
            "nullable": False,
            "text_semantics": "descriptive",
        }
    ]
    assert table_digest(
        columns, [{"narration": "é"}], schema_version=1
    ) != table_digest(columns, [{"narration": "e\u0301"}], schema_version=1)
