import pytest

from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.runtime.project import objects, read_project


def test_decode_existing_declarations_without_cash_input_language(
    project_document: dict,
) -> None:
    project = read_project(encode_json(project_document, integer_strings=False))
    assert project.entity.value == "invented-owner"
    assert project.sources[0].accession_id == project_document["sources"][0]
    assert (
        next(d for d in project.declarations if d.kind == "authored_fact").key
        == "initial-cash"
    )


@pytest.mark.parametrize(
    ("field", "value"), [("schema_version", 2), ("unexpected", None)]
)
def test_reject_unknown_project_contract(
    project_document: dict, field: str, value: object
) -> None:
    project_document[field] = value
    with pytest.raises(ContractError, match="project_schema"):
        read_project(encode_json(project_document, integer_strings=False))


def test_decode_literal_contains_rule(
    project_document: dict,
) -> None:
    rule = next(
        d["payload"]
        for d in project_document["declarations"]
        if d["payload"]["kind"] == "rule"
    )
    rule["matches"][0]["operator"] = "contains"
    project = read_project(encode_json(project_document, integer_strings=False))
    rule_document = next(d for d in project.declarations if d.kind == "rule").decode()
    assert objects(rule_document["matches"], "matches")[0]["operator"] == "contains"


@pytest.mark.parametrize(
    ("field", "operator", "value", "code"),
    [
        ("source.unregistered", "equals", "text", "rule_match_field"),
        ("source.synthetic", "equals", "5", "rule_match_value"),
        ("source.synthetic", "contains", "5", "contains_text"),
    ],
)
def test_source_rule_types_come_from_manifest_registration(
    project_document: dict, field: str, operator: str, value: object, code: str
) -> None:
    project_document["registries"]["observation_fields"].append(
        {
            "field_name": "source.synthetic",
            "value_type": "decimal",
            "units": "commodity",
            "payload_schema": None,
        }
    )
    rule = next(
        item["payload"]
        for item in project_document["declarations"]
        if item["payload"]["kind"] == "rule"
    )
    rule["matches"] = [{"field": field, "operator": operator, "value": value}]
    with pytest.raises(ContractError, match=code):
        read_project(encode_json(project_document, integer_strings=False))


def test_declaration_revisions_change_with_values(project_document: dict) -> None:
    first = read_project(encode_json(project_document, integer_strings=False))
    next(
        d["payload"]
        for d in project_document["declarations"]
        if d["payload"]["kind"] == "account"
    )["institution"] = "Renamed invented bank"
    second = read_project(encode_json(project_document, integer_strings=False))
    before = next(d for d in first.declarations if d.kind == "account")
    after = next(d for d in second.declarations if d.kind == "account")
    assert before.key == after.key
    assert before.revision != after.revision


def test_opening_precision_fails_at_project_boundary(project_document: dict) -> None:
    authored = next(
        item["payload"]
        for item in project_document["declarations"]
        if item["payload"]["kind"] == "authored_fact"
    )
    authored["record"]["legs"][0]["amount"] = {
        "coefficient": "10001",
        "scale": 2,
        "source_scale": 1,
    }
    with pytest.raises(ContractError, match="decimal_source_scale"):
        read_project(encode_json(project_document, integer_strings=False))
