from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, SchemaError, ValidationError

from jbt.contracts import schemas

if TYPE_CHECKING:
    from collections.abc import Callable

from jbt.contracts.schemas import (
    _isolated_schema,
    broker_match_schema,
    checks_artifact_schema,
    configuration_schema,
    decision_body_schema,
    declaration_schema,
    derivation_definitions_schema,
    descriptor_schema,
    envelope_schema,
    execution_record_schema,
    extraction_schema,
    journal_entry_schema,
    journal_head_schema,
    manifest_schema,
    model_artifact_schema,
    registry_schema,
    review_report_schema,
    summary_artifact_schema,
    validate_document,
)


@pytest.mark.parametrize(
    "factory",
    [
        declaration_schema,
        decision_body_schema,
        journal_entry_schema,
        journal_head_schema,
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
        review_report_schema,
        summary_artifact_schema,
    ],
)
def test_documents_are_valid_draft_202012_schemas(factory: Callable[[], dict]) -> None:
    schema = factory()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    fresh = factory()
    assert fresh is not schema
    if "$defs" in schema:
        schema["$defs"].clear()
    schema["properties"] = {}
    assert factory() == fresh


def test_schema_construction_is_cached_without_sharing_mutable_documents() -> None:
    calls = []

    @_isolated_schema
    def factory() -> dict:
        calls.append(True)
        return {"properties": {"value": {"enum": ["original"]}}}

    caller = factory()
    caller["properties"]["value"]["enum"].append("caller mutation")
    assert factory() == {"properties": {"value": {"enum": ["original"]}}}
    assert calls == [True]


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
        "importers": [],
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
        "statement_revision",
        "ordering",
        "rule",
        "assign",
        "split",
        "link",
        "transfer",
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
        "source_scope": {"sections": []},
        "status_mappings": [],
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
    validate_document("e\u0301", {"type": "string"}, reference_scope={})
    with pytest.raises(ValueError, match="text_utf8"):
        validate_document("\ud800", {"type": "string"}, reference_scope={})
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


def test_scoped_validation_caches_fragment_checks_without_sharing_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas._validator.cache_clear()  # noqa: SLF001
    schemas._check_schema.cache_clear()  # noqa: SLF001
    original = Draft202012Validator.check_schema
    checked = []

    def count_check(schema: dict) -> None:
        checked.append(schema)
        original(schema)

    monkeypatch.setattr(Draft202012Validator, "check_schema", count_check)
    scope = {"$defs": {"value": {"type": "integer"}}}
    fragment = {"$ref": "#/$defs/value"}
    validate_document(1, fragment, reference_scope=scope)
    validate_document(2, fragment, reference_scope=scope)
    assert len(checked) == 2
    fragment["$ref"] = "https://invalid.example/schema.json"
    with pytest.raises(ValueError, match="Only local schema references"):
        validate_document(1, fragment, reference_scope=scope)
    fragment = {"type": "not-a-type"}
    with pytest.raises(SchemaError):
        validate_document(1, fragment, reference_scope=scope)
    validate_document(3, {"$ref": "#/$defs/value"}, reference_scope=scope)
    assert len(checked) == 3


@pytest.mark.parametrize("identifier", ["", "e\u0301", "\n", "\x00", "\ud800"])
def test_common_identifier_schema_rejects_invalid_keys(identifier: str) -> None:
    schema = declaration_schema()
    with pytest.raises(ValidationError):
        validate_document(
            {
                "schema_version": 1,
                "kind": "counterparty",
                "counterparty_id": identifier,
                "name": " e\u0301\n\x00 ",
            },
            schema,
        )


def test_table_derived_schemas_classify_descriptive_values_and_identifiers() -> None:
    schema = declaration_schema()
    fields = schema["$defs"]["category"]["properties"]
    assert fields["name"] == {"$ref": "#/$defs/S"}
    assert fields["category_id"] == {"$ref": "#/$defs/ID"}
    transaction = next(
        branch["properties"]["record"]
        for branch in schema["$defs"]["authored_fact"]["oneOf"]
        if branch["properties"]["fact_kind"] == {"const": "transaction"}
    )
    assert transaction["properties"]["tags"] == {"$ref": "#/$defs/TL"}


@pytest.fixture
def journal_entry() -> dict:
    return {
        "schema_version": 1,
        "sequence": 1,
        "previous_entry_digest": None,
        "entity_id": "household",
        "declaration_key": "reviewed-split",
        "kind": "split",
        "operation": "record",
        "prior_entry_digest": None,
        "reviewed_attempt_digest": "a" * 64,
        "reason": " e\u0301\n\x00 ",
        "payload_digest": "b" * 64,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "assign",
        "split",
    ],
)
def test_journal_entry_variants_share_closed_custody_metadata(
    journal_entry: dict, kind: str
) -> None:
    journal_entry["kind"] = kind
    validate_document(journal_entry, journal_entry_schema())
    journal_entry.update(
        sequence=2,
        previous_entry_digest="c" * 64,
        operation="supersede",
        prior_entry_digest="d" * 64,
    )
    validate_document(journal_entry, journal_entry_schema())
    journal_entry.update(operation="retract", payload_digest=None)
    validate_document(journal_entry, journal_entry_schema())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entry: entry.update(schema_version=True),
        lambda entry: entry.update(sequence=True),
        lambda entry: entry.update(sequence=0),
        lambda entry: entry.update(sequence=2**63),
        lambda entry: entry.update(sequence=2),
        lambda entry: entry.update(previous_entry_digest="c" * 64),
        lambda entry: entry.update(prior_entry_digest="c" * 64),
        lambda entry: entry.update(payload_digest=None),
        lambda entry: entry.update(operation="supersede"),
        lambda entry: entry.update(operation="retract"),
        lambda entry: entry.update(operation="replace"),
        lambda entry: entry.update(kind="correction"),
        lambda entry: entry.update(reviewed_attempt_digest="A" * 64),
        lambda entry: entry.update(reason=""),
        lambda entry: entry.update(declaration_key="e\u0301"),
        lambda entry: entry.update(entity_id="\n"),
        lambda entry: entry.update(entry_digest="c" * 64),
        lambda entry: entry.pop("reason"),
        lambda entry: entry.pop("prior_entry_digest"),
    ],
)
def test_journal_entry_rejects_incoherent_or_open_metadata(
    journal_entry: dict, mutation: Callable[[dict], object]
) -> None:
    mutation(journal_entry)
    with pytest.raises(ValidationError):
        validate_document(journal_entry, journal_entry_schema())


@pytest.mark.parametrize("sequence", [0, 1, 2**63 - 1])
def test_journal_head_schema_names_exact_commit(sequence: int) -> None:
    validate_document(
        {
            "schema_version": 1,
            "sequence": sequence,
            "entry_digest": None if sequence == 0 else "a" * 64,
        },
        journal_head_schema(),
    )


@pytest.mark.parametrize(
    ("sequence", "digest"),
    [(True, "a" * 64), (-1, None), (2**63, "a" * 64), (0, "a" * 64), (1, None)],
)
def test_journal_head_schema_rejects_incoherent_commit(
    sequence: int, digest: str | None
) -> None:
    with pytest.raises(ValidationError):
        validate_document(
            {"schema_version": 1, "sequence": sequence, "entry_digest": digest},
            journal_head_schema(),
        )


def test_assignment_schema_rejects_amount_edits_and_wrong_target_vocabulary() -> None:
    document = {
        "schema_version": 1,
        "kind": "assign",
        "entity_id": "household",
        "review": {"attempt_digest": "a" * 64, "basis_digest": "b" * 64},
        "target": {"entity_id": "household", "event_id": "c" * 64},
        "assignments": [{"kind": "leg_category", "category_id": None}],
    }
    with pytest.raises(ValidationError):
        validate_document(document, decision_body_schema())
    document["target"] = {"entity_id": "household", "leg_id": "d" * 64}
    document["assignments"] = [
        {
            "kind": "leg_amount",
            "amount": {"coefficient": "100", "scale": 0, "source_scale": None},
        }
    ]
    with pytest.raises(ValidationError):
        validate_document(document, decision_body_schema())
    document["assignments"] = [
        {
            "kind": "leg_date",
            "role": "posted",
            "date": {"mode": "inherit", "value": "2026-01-01"},
        }
    ]
    with pytest.raises(ValidationError):
        validate_document(document, decision_body_schema())


@pytest.mark.parametrize(
    "fields",
    [
        {
            "kind": "transfer",
            "outgoing": ["c" * 64],
            "incoming": [],
            "commodity": "NZD",
        },
        {
            "kind": "transfer",
            "outgoing": [],
            "incoming": ["c" * 64],
            "commodity": "NZD",
        },
        {
            "kind": "link",
            "link_kind": "document",
            "target": {"entity_id": "household", "event_id": "c" * 64},
            "documents": ["d" * 64],
        },
        {
            "kind": "link",
            "link_kind": "lifecycle",
            "attempt": {"entity_id": "household", "event_id": "c" * 64},
            "fee": {"entity_id": "household", "event_id": "d" * 64},
        },
    ],
)
def test_transfer_and_link_schema_is_closed(fields: dict) -> None:
    document = {
        "schema_version": 1,
        "entity_id": "household",
        "review": {"attempt_digest": "a" * 64, "basis_digest": "b" * 64},
        **fields,
    }
    validate_document(document, decision_body_schema())
    with pytest.raises(ValidationError):
        validate_document({**document, "inferred": True}, decision_body_schema())
