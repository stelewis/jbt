import pytest

from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.runtime.project import SourceRole, read_project


def test_decode_existing_declarations_without_cash_input_language(
    project_document: dict,
) -> None:
    project = read_project(encode_json(project_document, integer_strings=False))
    assert project.entity.value == "invented-owner"
    assert project.sources[0].role == SourceRole.OPENING
    assert [rule.category_id for rule in project.category_rules()] == [
        "income",
        "expense",
    ]
    assert project.one("authored_fact").key == "initial-cash"


@pytest.mark.parametrize(
    ("field", "value"), [("schema_version", 2), ("unexpected", None)]
)
def test_reject_unknown_project_contract(
    project_document: dict, field: str, value: object
) -> None:
    project_document[field] = value
    with pytest.raises(ContractError, match="project_schema"):
        read_project(encode_json(project_document, integer_strings=False))


def test_reject_unsupported_rule_instead_of_skipping(project_document: dict) -> None:
    rule = next(
        d["payload"]
        for d in project_document["declarations"]
        if d["payload"]["kind"] == "rule"
    )
    rule["matches"][0]["operator"] = "contains"
    with pytest.raises(ContractError, match="rule_operator_unsupported"):
        read_project(encode_json(project_document, integer_strings=False))


def test_declaration_revisions_change_with_values(project_document: dict) -> None:
    first = read_project(encode_json(project_document, integer_strings=False))
    next(
        d["payload"]
        for d in project_document["declarations"]
        if d["payload"]["kind"] == "account"
    )["institution"] = "Renamed invented bank"
    second = read_project(encode_json(project_document, integer_strings=False))
    assert first.one("account").key == second.one("account").key
    assert first.one("account").revision != second.one("account").revision


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
