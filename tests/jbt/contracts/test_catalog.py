import json
from importlib.resources import files
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, ValidationError

from jbt.contracts.catalog import Column, catalog, tabular_schema
from jbt.contracts.primitives import ContractError

if TYPE_CHECKING:
    from collections.abc import Callable

    from jsonschema.protocols import Validator


@pytest.fixture(scope="module")
def catalog_validator() -> Validator:
    schema = json.loads(
        files("jbt.contracts").joinpath("resources/catalog.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def catalog_document() -> dict:
    return json.loads(
        files("jbt.contracts").joinpath("resources/catalog.json").read_text()
    )


def test_packaged_catalog_matches_its_schema(
    catalog_document: dict, catalog_validator: Validator
) -> None:
    catalog_validator.validate(catalog_document)


def test_catalog_shape_does_not_duplicate_the_required_table_inventory(
    catalog_document: dict, catalog_validator: Validator
) -> None:
    catalog_document["tables"] = catalog_document["tables"][:1]
    catalog_validator.validate(catalog_document)


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda d: d.update(unexpected=True), "$"),
        (lambda d: d.pop("text_semantics"), "$"),
        (lambda d: d.pop("foreign_keys"), "$"),
        (lambda d: d.update(tables=[]), "$.tables"),
        (
            lambda d: d["tables"][0].update(primary_key=["txn_id", "txn_id"]),
            "$.tables[0].primary_key",
        ),
        (
            lambda d: d["tables"][0]["fields"].update(txn_id="float"),
            "$.tables[0].fields.txn_id",
        ),
        (
            lambda d: d["text_semantics"].update(entity_id="descriptive"),
            "$.text_semantics.entity_id",
        ),
        (
            lambda d: d["text_semantics"]["tables"]["transactions"].update(
                unknown=["txn_id"]
            ),
            "$.text_semantics.tables.transactions",
        ),
        (
            lambda d: d["text_semantics"]["tables"]["transactions"].update(
                identifier="txn_id"
            ),
            "$.text_semantics.tables.transactions.identifier",
        ),
        (
            lambda d: d["text_semantics"]["bundles"]["time"].update(
                identifier=["timestamp_local", "timestamp_local"]
            ),
            "$.text_semantics.bundles.time.identifier",
        ),
        (
            lambda d: d["foreign_keys"]["postings"].update(txn_id=["transactions"]),
            "$.foreign_keys.postings.txn_id",
        ),
        (
            lambda d: d["foreign_keys"]["postings"].update(txn_id="bad name"),
            "$.foreign_keys.postings.txn_id",
        ),
    ],
)
def test_catalog_schema_rejects_malformed_shapes_with_located_errors(
    catalog_document: dict,
    catalog_validator: Validator,
    mutation: Callable[[dict], object],
    path: str,
) -> None:
    mutation(catalog_document)
    with pytest.raises(ValidationError) as error:
        catalog_validator.validate(catalog_document)
    assert error.value.json_path == path


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
        "statement_coverage",
        "source_records",
        "declarations",
        "economic_identities",
        "event_support",
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
    document["numeric_limits"]["coefficient_digits"] = 1
    position["foreign_keys"][0]["columns"].clear()
    document["tables"].clear()
    fresh = tabular_schema()
    assert len(fresh["tables"]) == 40
    assert fresh["numeric_limits"]["coefficient_digits"] == 96
    assert all(
        reference["columns"]
        for table in fresh["tables"]
        for reference in table["foreign_keys"]
    )


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


def test_every_physical_text_column_has_explicit_semantics() -> None:
    for table in tabular_schema()["tables"]:
        for column in table["columns"]:
            if column["kind"] in {"string", "list"}:
                assert column["text_semantics"] in {"identifier", "descriptive"}
            else:
                assert "text_semantics" not in column
    for table in catalog():
        for column in table.columns:
            assert (column.text_semantics is not None) == (
                column.kind in {"string", "list"}
            )


def test_exact_descriptive_classification() -> None:
    expected = {
        "transactions": {"payee", "narration", "description_as_stated", "tags"},
        "postings": {"symbol_as_stated", "broker_lot_id"},
        "opening_lots": {"broker_lot_id"},
        "prices": {"basis", "market"},
        "accounts": {"institution"},
        "categories": {"name"},
        "commodities": {"name"},
        "counterparties": {"name"},
        "statements": {"institution"},
        "source_records": {"external_record_id", "ignored_reason"},
        "declarations": {"payload_json"},
        "observations": {"text_value"},
    }
    for table in tabular_schema()["tables"]:
        actual = {
            column["name"]
            for column in table["columns"]
            if column.get("text_semantics") == "descriptive"
        }
        assert actual == expected.get(table["name"], set())


def test_column_semantics_have_no_missing_metadata_default() -> None:
    with pytest.raises(ContractError, match="column_text_semantics"):
        Column("name", "string", nullable=False, text_semantics=None)
    with pytest.raises(ContractError, match="column_text_semantics"):
        Column("date", "date", nullable=False, text_semantics="descriptive")
