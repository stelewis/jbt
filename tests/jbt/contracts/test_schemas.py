from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, SchemaError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from jbt.contracts.schemas import (
    broker_match_schema,
    checks_artifact_schema,
    configuration_schema,
    declaration_schema,
    derivation_definitions_schema,
    descriptor_schema,
    envelope_schema,
    execution_record_schema,
    extraction_schema,
    manifest_schema,
    model_artifact_schema,
    registry_schema,
    summary_artifact_schema,
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
        execution_record_schema,
        derivation_definitions_schema,
        checks_artifact_schema,
        model_artifact_schema,
        summary_artifact_schema,
    ],
)
def test_documents_are_valid_draft_202012_schemas(factory: Callable[[], dict]) -> None:
    schema = factory()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.fixture
def execution_record() -> dict:
    return {
        "schema_version": 1,
        "producer": {
            "kind": "release",
            "name": "jbt",
            "version": "0.1.0a1",
            "source_commit": "synthetic-commit",
        },
        "runtime": {
            "implementation": "cpython",
            "version": "3.14",
            "platform": "synthetic",
            "architecture": "synthetic",
        },
        "distributions": [{"name": "jbt", "version": "0.1.0a1"}],
    }


def test_execution_record_distinguishes_release_and_development(
    execution_record: dict,
) -> None:
    schema = execution_record_schema()
    validate_document(execution_record, schema)
    execution_record["producer"]["source_commit"] = None
    validate_document(execution_record, schema)
    execution_record["producer"].update(kind="development", source_commit=None)
    validate_document(execution_record, schema)
    execution_record["producer"].update(source_commit="synthetic-commit")
    validate_document(execution_record, schema)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(schema_version="1"),
        lambda r: r.update(schema_version=True),
        lambda r: r.update(is_dirty=False),
        lambda r: r["producer"].update(build_id=None),
        lambda r: r["producer"].update(name="other"),
        lambda r: r["producer"].update(kind="local"),
        lambda r: r["producer"].update(version=1),
        lambda r: r["producer"].update(is_dirty=None),
        lambda r: r["producer"].update(content_digest="a" * 64),
        lambda r: r["producer"].pop("source_commit"),
        lambda r: r["runtime"].update(path="python.tar"),
        lambda r: r["runtime"].pop("architecture"),
        lambda r: r["distributions"][0].update(path="jbt.whl"),
        lambda r: r["distributions"][0].update(version=1),
    ],
)
def test_execution_record_rejects_old_or_open_shapes(
    execution_record: dict, mutation: Callable[[dict], object]
) -> None:
    mutation(execution_record)
    with pytest.raises(ValidationError):
        validate_document(execution_record, execution_record_schema())


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


def test_subschemas_resolve_against_the_explicit_enclosing_scope() -> None:
    scope = {"$defs": {"value": {"type": "integer", "minimum": 2}}}
    branch = {"$ref": "#/$defs/value"}
    validate_document(2, branch, reference_scope=scope)
    with pytest.raises(ValidationError):
        validate_document(1, branch, reference_scope=scope)
    with pytest.raises(ValidationError):
        validate_document("2", branch, reference_scope=scope)
    other = {"$defs": {"value": {"type": "string"}}}
    validate_document("2", branch, reference_scope=other)
    with pytest.raises(ValidationError):
        validate_document(2, branch, reference_scope=other)


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize("remote_scope", [False, True])
def test_scoped_validation_refuses_remote_references_at_both_boundaries(
    keyword: str, *, remote_scope: bool
) -> None:
    remote = {keyword: "https://invalid.example/schema.json"}
    schema, scope = ({}, remote) if remote_scope else (remote, {})
    with pytest.raises(ValueError, match="Only local schema references"):
        validate_document({}, schema, reference_scope=scope)


def test_scoped_validation_preserves_schema_and_primitive_checks() -> None:
    with pytest.raises(ValidationError):
        validate_document(
            "2026-02-30", {"type": "string", "format": "date"}, reference_scope={}
        )
    with pytest.raises(ValueError, match="json_float"):
        validate_document(1.0, {"type": "integer"}, reference_scope={})
    with pytest.raises(ValueError, match="text_nfc"):
        validate_document("e\u0301", {"type": "string"}, reference_scope={})
    with pytest.raises(SchemaError):
        validate_document(1, {"type": "not-a-type"}, reference_scope={})


def test_scoped_schema_mutations_do_not_change_cached_validation() -> None:
    scope = {"$defs": {"value": {"type": "integer"}}}
    branch = {"$ref": "#/$defs/value"}
    validate_document(1, branch, reference_scope=scope)
    scope["$defs"]["value"]["type"] = "string"
    validate_document("1", branch, reference_scope=scope)
    with pytest.raises(ValidationError):
        validate_document(
            "1", branch, reference_scope={"$defs": {"value": {"type": "integer"}}}
        )


def test_integral_floats_are_not_json_contract_integers() -> None:
    with pytest.raises(ValueError, match="json_float"):
        validate_document(1.0, {"type": "integer"})
