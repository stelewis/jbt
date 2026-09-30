from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from jbt.contracts.schemas import (
    broker_match_schema,
    configuration_schema,
    declaration_schema,
    descriptor_schema,
    envelope_schema,
    extraction_schema,
    manifest_schema,
    registry_schema,
    validate_document,
)


@pytest.mark.parametrize(
    "factory",
    [
        declaration_schema,
        registry_schema,
        configuration_schema,
        broker_match_schema,
        extraction_schema,
        manifest_schema,
        descriptor_schema,
        envelope_schema,
    ],
)
def test_documents_are_valid_draft_202012_schemas(factory: Callable[[], dict]) -> None:
    schema = factory()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_declaration_variants_are_exhaustive_and_closed() -> None:
    schema = declaration_schema()
    assert {variant["$ref"].split("/")[-1] for variant in schema["oneOf"]} == {
        "account",
        "category",
        "commodity",
        "counterparty",
        "position",
        "assertion_scope",
        "action_scope",
        "opening_lot",
        "booking",
        "pool",
        "fee_attribution",
        "contract_terms",
        "corporate_action",
        "identity",
        "ordering",
        "rule",
        "correction",
        "link",
        "authored_fact",
    }
    payload = {
        "schema_version": 1,
        "kind": "counterparty",
        "counterparty_id": "c",
        "name": "Synthetic",
    }
    validate_document(payload, schema)
    for extra in ("extras", "python_import", "url"):
        with pytest.raises(ValidationError):
            validate_document({**payload, extra: {}}, schema)


def test_manifest_checks_describe_only_financial_model_findings() -> None:
    schema = manifest_schema()
    assert schema["properties"]["checks"]["items"] == {"$ref": "#/$defs/Check"}
    description = schema["$defs"]["Check"]["description"]
    assert "financial/model finding" in description
    assert "byte checks" in description
    assert "file inventory checks" in description
    assert "reader integrity verification" in description


def test_manifest_schema_and_cached_validator_are_mutation_isolated() -> None:
    caller_schema = manifest_schema()
    original_description = caller_schema["$defs"]["Check"]["description"]
    with pytest.raises(ValidationError):
        validate_document({}, caller_schema)

    caller_schema["required"].clear()
    caller_schema["properties"].clear()
    caller_schema["$defs"]["Check"]["description"] = "caller-owned mutation"
    validate_document({}, caller_schema)

    fresh = manifest_schema()
    assert "checks" in fresh["required"]
    assert "checks" in fresh["properties"]
    assert fresh["$defs"]["Check"]["description"] == original_description
    with pytest.raises(ValidationError):
        validate_document({}, fresh)


def test_redenomination_is_admitted_by_closed_action_payloads() -> None:
    declaration = declaration_schema()["$defs"]["corporate_action"]
    extraction = next(
        record["properties"]["payload"]
        for record in extraction_schema()["properties"]["records"]["items"]["oneOf"]
        if record["properties"]["kind"] == {"const": "corporate_action"}
    )
    for action in (declaration, extraction):
        assert "redenomination" in action["properties"]["action_kind"]["enum"]
        assert "effects" in action["required"]


@pytest.mark.parametrize(
    "target",
    [
        {"table": "postings", "key": []},
        {"table": "postings", "key": ["p", "other"]},
        {"table": "postings", "key": [1]},
        {"table": "unknown", "key": ["p"]},
    ],
)
def test_record_refs_reject_unknown_tables_arity_and_types(target: dict) -> None:
    schema = declaration_schema()
    reference_schema = {**schema, "oneOf": [{"$ref": "#/$defs/RecordRef"}]}
    with pytest.raises(ValidationError):
        validate_document(target, reference_schema)


def test_broker_match_preserves_exact_quantity_and_required_null() -> None:
    match = {
        "schema_version": 1,
        "acquisition_source_record_id": "a",
        "disposal_source_record_id": "d",
        "quantity": {
            "coefficient": "842",
            "scale": 1,
            "source_scale": 2,
        },
        "commodity_id": "Q",
        "broker_lot_id": None,
    }
    validate_document(match, broker_match_schema())
    del match["broker_lot_id"]
    with pytest.raises(ValidationError):
        validate_document(match, broker_match_schema())


def test_extraction_preserves_unsupported_content_without_fake_financial_records() -> (
    None
):
    extract = {
        "schema_version": 1,
        "source_scope_id": "scope",
        "source_blob_digest": "sha256:synthetic",
        "importer_id": "synthetic-v1",
        "source_scope": {
            "source_class": "document",
            "institution": None,
            "period_start": None,
            "period_end": None,
            "completeness": "unstated",
            "source_accounts": [],
            "revisions": [],
        },
        "records": [],
        "unsupported_content": [
            {
                "source_section": "account",
                "record_locator": "page:1",
                "event_family": "unimplemented",
                "reason": "unsupported_contract",
            }
        ],
    }
    validate_document(extract, extraction_schema())
    extract["records"] = [{"kind": "invented"}]
    with pytest.raises(ValidationError):
        validate_document(extract, extraction_schema())


def test_remote_schema_resolution_is_refused() -> None:
    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        with pytest.raises(ValueError, match="Only local schema references"):
            validate_document({}, {keyword: "https://invalid.example/schema.json"})


def test_integral_floats_are_not_json_contract_integers() -> None:
    with pytest.raises(ValueError, match="json_float"):
        validate_document(1.0, {"type": "integer"})
