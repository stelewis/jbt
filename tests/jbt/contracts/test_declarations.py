import json
from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

from jbt.contracts.declarations import (
    decode_declaration,
    field_registry,
    validate_declaration,
)


@pytest.mark.parametrize(
    "kind",
    ["assign", "split", "identity", "statement_revision", "transfer", "link"],
)
def test_journal_declarations_decode_through_the_same_header_boundary(
    kind: str,
) -> None:
    payload = {
        "schema_version": 1,
        "sequence": 1,
        "previous_entry_digest": None,
        "entity_id": "synthetic",
        "declaration_key": "reviewed",
        "kind": kind,
        "operation": "record",
        "prior_entry_digest": None,
        "reviewed_attempt_digest": "a" * 64,
        "reason": " e\u0301\n\x00 ",
        "payload_digest": "b" * 64,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert decode_declaration(encoded) == payload
    with pytest.raises(ValueError, match="journal_declaration_schema"):
        validate_declaration({**payload, "reason": ""})
    with pytest.raises(ValueError, match="journal_declaration_schema"):
        validate_declaration({**payload, "link_kind": "document"})


@pytest.fixture
def terms() -> dict:
    return {
        "schema_version": 1,
        "kind": "contract_terms",
        "commodity_id": "Q",
        "payoff": {
            "kind": "linear_difference",
            "reference_commodity_id": "USD",
            "settlement_commodity_id": "USD",
            "multiplier": {"coefficient": "10", "scale": 0, "source_scale": None},
        },
        "settlement": {
            "mode": "cash",
            "variation": "final",
            "reference_reset": "on_final_variation",
            "rounding": {"scale": 2, "mode": "half_even", "residual": "final_slice"},
        },
        "collateral": {"mode": "none"},
        "expiry": None,
        "deliverables": [],
        "evidence": [{"kind": "source_record", "id": "source"}],
    }


def test_terms_roundtrip_preserves_exact_multiplier(terms: dict) -> None:
    validate_declaration(terms)
    encoded = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    assert decode_declaration(encoded) == terms


def test_retained_declaration_validation_isolated_from_caller_mutation(
    terms: dict,
) -> None:
    encoded = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    decoded = decode_declaration(encoded)
    decoded["payoff"]["multiplier"]["coefficient"] = "-1"
    assert decode_declaration(encoded) == terms
    with pytest.raises(ValueError, match="positive_multiplier"):
        decode_declaration(json.dumps(decoded, sort_keys=True, separators=(",", ":")))


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda t: t.update(schema_version=2), "schema_version"),
        (lambda t: t.update(extras={}), "declaration_schema"),
        (lambda t: t.pop("expiry"), "declaration_schema"),
        (lambda t: t["payoff"].update(multiplier=1.5), "declaration_schema"),
        (
            lambda t: t["payoff"]["multiplier"].update(coefficient="-1"),
            "positive_multiplier",
        ),
        (
            lambda t: t["payoff"]["multiplier"].update(coefficient="00"),
            "declaration_schema",
        ),
        (lambda t: t["settlement"].update(reference_reset="none"), "variation_reset"),
        (lambda t: t["settlement"].update(mode="physical"), "physical_deliverables"),
        (lambda t: t.update(commodity_id=None), "terms_commodity"),
    ],
)
def test_terms_reject_invalid_contract_combinations(
    terms: dict,
    mutation: Callable[[dict], object],
    code: str,
) -> None:
    mutation(terms)
    with pytest.raises(ValueError, match=code):
        validate_declaration(terms)


def test_canonical_payload_rejects_duplicates_and_whitespace(terms: dict) -> None:
    text = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="canonical_payload_json"):
        decode_declaration(text + " ")
    with pytest.raises(ValueError, match="json_duplicate_key"):
        decode_declaration('{"kind":"counterparty","kind":"account"}')


def test_registry_explicitly_prohibits_identity_and_inventory_assignments() -> None:
    registry = {row["field_name"]: row for row in field_registry()}
    assert registry["transactions.tags"]["rule_mode"] == "union"
    assert registry["transactions.payee"]["correction_allowed"]
    assert registry["transactions.event_state"]["rule_mode"] == "prohibited"
    assert not registry["postings.amount"]["correction_allowed"]
    assert not registry["inventory_changes.units_delta"]["correction_allowed"]
    assert not registry["transactions.txn_id"]["correction_allowed"]


def test_account_cycle_policy_is_closed_and_lifecycle_is_independent(
    project_document: dict,
) -> None:
    account = deepcopy(project_document["declarations"][0]["payload"])
    account.update(
        statement_cadence="monthly_cycle",
        period_rule="monthly_cycle",
        expected_periods=[],
        cycle_end_day=31,
        closing_tolerance_days=2,
    )
    validate_declaration(account)
    for changes, code in (
        ({"cycle_end_day": 32}, "declaration_schema"),
        ({"closing_tolerance_days": 14}, "declaration_schema"),
        ({"period_rule": "calendar"}, "declaration_schema"),
        ({"statement_cadence": "monthly"}, "calendar_period_rule"),
        ({"coverage_basis_ids": []}, "coverage_basis_required"),
        ({"opened_date": "2026-02-01"}, "account_coverage_lifecycle"),
        ({"closed_date": "2025-01-01"}, "account_lifecycle"),
    ):
        with pytest.raises(ValueError, match=code):
            validate_declaration({**account, **changes})


@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("transactions.payee", "Synthetic", True),
        ("transactions.review_state", "reviewed", True),
        ("transactions.review_state", "cleared", False),
        ("transactions.event_state", "recognized", False),
        (
            "postings.amount",
            {"coefficient": "1", "scale": 0, "source_scale": None},
            False,
        ),
        ("transactions.txn_id", "new", False),
    ],
)
def test_rule_assignment_allowlist(field: str, value: object, *, valid: bool) -> None:
    payload = {
        "schema_version": 1,
        "kind": "rule",
        "order": 0,
        "matches": [],
        "assignments": [{"field": field, "value": value}],
        "split": [],
    }
    if valid:
        validate_declaration(payload)
    else:
        with pytest.raises(ValueError, match="assignment"):
            validate_declaration(payload)


@pytest.mark.parametrize("text", ["e\u0301", " e\u0301\n\x00 ", ""])
def test_descriptive_declaration_roundtrip_is_exact(text: str) -> None:
    payload = {
        "schema_version": 1,
        "kind": "counterparty",
        "counterparty_id": "counterparty",
        "name": text,
    }
    validate_declaration(payload)
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert decode_declaration(encoded)["name"] == text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transactions.payee", " e\u0301\n\x00 "),
        ("transactions.narration", " e\u0301\n\x00 "),
        ("transactions.tags", ["", " e\u0301\n\x00 ", "e\u0301", "é"]),
    ],
)
def test_rule_descriptive_assignments_and_matches_are_exact(
    field: str, value: object
) -> None:
    payload = {
        "schema_version": 1,
        "kind": "rule",
        "order": 0,
        "matches": [{"field": field, "operator": "equals", "value": value}],
        "assignments": [{"field": field, "value": value}],
        "split": [],
    }
    validate_declaration(payload)
    assert payload["matches"][0]["value"] == value
    assert payload["assignments"][0]["value"] == value


@pytest.mark.parametrize("value", ["", "e\u0301", "\n", "\x00", "\ud800"])
@pytest.mark.parametrize("operation", ["assignment", "match"])
def test_rule_identifier_values_remain_strict(value: str, operation: str) -> None:
    payload = {
        "schema_version": 1,
        "kind": "rule",
        "order": 0,
        "matches": [],
        "assignments": [],
        "split": [],
    }
    if operation == "assignment":
        payload["assignments"] = [{"field": "postings.category_id", "value": value}]
    else:
        payload["matches"] = [
            {"field": "postings.category_id", "operator": "equals", "value": value}
        ]
    with pytest.raises(ValueError, match=r"canonical_key_required|text_utf8"):
        validate_declaration(payload)


@pytest.mark.parametrize(
    ("field", "operator", "value", "code"),
    [
        ("postings.amount", "equals", "5", "rule_match_value"),
        ("postings.amount", "contains", "5", "contains_text"),
        ("postings.account_id", "contains", "bank", "contains_text"),
        ("transactions.date_posted", "equals", "2026-02-30", "rule_match_value"),
        ("transactions.review_state", "equals", "cleared", "rule_match_value"),
        ("transactions.payee", "in", ["text", 1], "in_homogeneous"),
        (
            "postings.amount",
            "in",
            [
                {"coefficient": "5", "scale": 0, "source_scale": None},
                {"coefficient": 6, "scale": 0, "source_scale": None},
            ],
            "declaration_schema",
        ),
        ("transactions.tags", "in", [["b", "a"]], "rule_match_value"),
    ],
)
def test_rule_operators_validate_the_field_type(
    field: str, operator: str, value: object, code: str
) -> None:
    payload = {
        "schema_version": 1,
        "kind": "rule",
        "order": 0,
        "matches": [{"field": field, "operator": operator, "value": value}],
        "assignments": [],
        "split": [],
    }
    with pytest.raises(ValueError, match=code):
        validate_declaration(payload)


def test_membership_wraps_a_list_valued_field_without_coercion() -> None:
    validate_declaration(
        {
            "schema_version": 1,
            "kind": "rule",
            "order": 0,
            "matches": [
                {
                    "field": "transactions.tags",
                    "operator": "in",
                    "value": [["a"], ["a", "b"]],
                }
            ],
            "assignments": [],
            "split": [],
        }
    )


def test_pool_eligibility_never_invents_constituents() -> None:
    payload = {
        "schema_version": 1,
        "kind": "pool",
        "pool_id": "P",
        "position_id": "p",
        "book_commodity_id": "USD",
        "booking_declaration_id": "b",
        "fee_treatment": "expensed",
        "rounding": {"scale": 2, "mode": "half_even", "residual": "final_slice"},
        "eligibility": {"mode": "all_origins", "lot_ids": []},
        "conversions": [],
    }
    validate_declaration(payload)
    payload["eligibility"]["lot_ids"] = ["lot"]
    with pytest.raises(ValueError, match="pool_eligibility"):
        validate_declaration(payload)
