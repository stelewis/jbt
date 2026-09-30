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
    assert registry["postings.amount"]["correction_allowed"]
    assert not registry["inventory_changes.units_delta"]["correction_allowed"]
    assert not registry["transactions.txn_id"]["correction_allowed"]


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


def test_correction_retraction_has_no_replacement_or_target() -> None:
    payload: dict = {
        "schema_version": 1,
        "kind": "correction",
        "operation": "retract",
        "target": None,
        "prior_declaration_id": "prior",
        "reason": "Reviewed replacement",
        "journal_sequence": 1,
        "guards": [],
        "assignments": [],
        "selection": [],
    }
    validate_declaration(payload)
    mutated = deepcopy(payload)
    mutated["target"] = {"table": "transactions", "key": ["t"]}
    with pytest.raises(ValueError, match="correction_retract"):
        validate_declaration(mutated)


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
