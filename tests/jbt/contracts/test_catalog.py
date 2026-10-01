from jbt.contracts.catalog import catalog, tabular_schema


def test_catalog_has_every_required_table_with_entity_keys() -> None:
    expected = {
        "transactions",
        "postings",
        "event_times",
        "book_steps",
        "book_step_dependencies",
        "posting_weights",
        "positions",
        "lots",
        "opening_lots",
        "inventory_pools",
        "inventory_allocations",
        "inventory_changes",
        "component_conversions",
        "disposals",
        "fee_allocations",
        "inventory_fee_changes",
        "reference_changes",
        "reference_settlements",
        "assertion_scopes",
        "balance_assertions",
        "balances",
        "prices",
        "corporate_actions",
        "corporate_action_effects",
        "action_applications",
        "accounts",
        "categories",
        "commodities",
        "counterparties",
        "commodity_symbols",
        "statements",
        "source_records",
        "declarations",
        "economic_identities",
        "observations",
        "links",
        "provenance",
        "build",
    }
    tables = catalog()
    assert {table.name for table in tables} == expected
    assert len(tables) == len(expected)
    assert all(table.primary_key[0] == "entity_id" for table in tables)
    assert all(table.sort_key[0] == "entity_id" for table in tables)
    assert all(table.columns[0].name == "entity_id" for table in tables)


def test_decimal_expansion_and_physical_types() -> None:
    posting = next(table for table in catalog() if table.name == "postings")
    columns = {column.name: column for column in posting.columns}
    assert columns["amount_coefficient"].kind == "string"
    assert not columns["amount_coefficient"].nullable
    assert columns["amount_scale"].kind == "integer"
    assert columns["amount_source_scale"].nullable
    assert columns["original_amount_coefficient"].nullable
    assert columns["date_posted"].kind == "date"
    assert posting.primary_key == ("entity_id", "posting_id")
    assert posting.sort_key == ("entity_id", "txn_id", "posting_index", "posting_id")


def test_redenomination_is_a_distinct_corporate_action_kind() -> None:
    action = next(
        table
        for table in tabular_schema()["tables"]
        if table["name"] == "corporate_actions"
    )
    kind = next(
        column for column in action["columns"] if column["name"] == "action_kind"
    )
    assert "redenomination" in kind["enum"]
    assert "symbol_change" in kind["enum"]


def test_action_application_registry_names_exact_basis_dependencies() -> None:
    check = next(
        entry
        for entry in tabular_schema()["semantic_checks"]
        if entry["name"] == "action_application_identity"
    )
    assert {"action_applications", "corporate_action_effects", "inventory_changes"} <= (
        set(check["tables"])
    )
    precedence = next(
        entry
        for entry in tabular_schema()["semantic_checks"]
        if entry["name"] == "economic_precedence_graph"
    )
    assert {"book_step_dependencies", "observations", "source_records"} <= set(
        precedence["tables"]
    )


def test_catalog_metadata_includes_relationships_and_is_not_shared_mutable_state() -> (
    None
):
    document = tabular_schema()
    position = next(
        table for table in document["tables"] if table["name"] == "positions"
    )
    assert {
        "columns": ["entity_id", "account_id"],
        "table": "accounts",
        "target_columns": ["entity_id", "account_id"],
    } in position["foreign_keys"]
    assert document["numeric_limits"]["coefficient_digits"] == 96
    document["tables"].clear()
    assert len(tabular_schema()["tables"]) == 38


def test_external_identifiers_are_not_internal_foreign_keys() -> None:
    tables = {table["name"]: table for table in tabular_schema()["tables"]}
    for table, field in (
        ("source_records", "source_scope_id"),
        ("commodity_symbols", "source_scope_id"),
        ("postings", "broker_lot_id"),
        ("opening_lots", "broker_lot_id"),
    ):
        assert all(
            field not in reference["columns"]
            for reference in tables[table]["foreign_keys"]
        )
    assert {
        "columns": ["entity_id", "scope_id"],
        "table": "assertion_scopes",
        "target_columns": ["entity_id", "scope_id"],
    } in tables["balance_assertions"]["foreign_keys"]


def test_declared_references_resolve_to_complete_target_keys() -> None:
    tables = {table["name"]: table for table in tabular_schema()["tables"]}
    for table in tables.values():
        fields = {column["name"] for column in table["columns"]}
        for reference in table["foreign_keys"]:
            assert set(reference["columns"]) <= fields
            assert (
                reference["target_columns"] == tables[reference["table"]]["primary_key"]
            )
            assert len(reference["columns"]) == len(reference["target_columns"])
