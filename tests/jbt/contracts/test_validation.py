import json
from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

from jbt.contracts.primitives import ContractError
from jbt.contracts.validation import (
    _action_applications,
    _action_effects,
    _economic_identities,
    _intervals,
    _positions_and_lineage,
    validate_link_members,
    validate_tables,
)
from jbt.domain.identity import AuthoredAnchor, Occurrence, event_id
from jbt.domain.ids import EntityId, SemanticKey

TABLE_NAMES = [
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
]


@pytest.fixture
def snapshot() -> dict:
    tables = {name: [] for name in TABLE_NAMES}
    tables["build"] = [
        {
            "entity_id": "e",
            "manifest_digest": "0" * 64,
            "producer_version": "test",
            "schema_version": 1,
            "schema_digest": "1" * 64,
            "as_of": "2026-01-01",
            "execution_fingerprint": "2" * 64,
        }
    ]
    tables["transactions"] = [
        {
            "entity_id": "e",
            "txn_id": "t",
            "event_kind": "transaction",
            "event_state": "pending",
            "payee": None,
            "narration": None,
            "description_as_stated": None,
            "review_state": "unreviewed",
            "tags": [],
            "links": [],
            "date_authorized": None,
            "date_traded": None,
            "date_posted": "2026-01-01",
            "date_settled": None,
            "date_value": None,
        }
    ]
    return tables


def test_complete_empty_tables_and_required_nulls_are_valid(snapshot: dict) -> None:
    validate_tables(snapshot)
    validate_tables({name: [] for name in TABLE_NAMES})


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda t: t.pop("lots"), "complete_table_set"),
        (lambda t: t.update(unknown=[]), "complete_table_set"),
        (lambda t: t["transactions"][0].pop("payee"), "closed_row"),
        (lambda t: t["transactions"][0].update(extra=None), "closed_row"),
        (lambda t: t["transactions"][0].update(event_state="cleared"), "enum_value"),
        (lambda t: t["transactions"][0].update(tags=["z", "a"]), "list_order_unique"),
        (lambda t: t["transactions"][0].update(tags=["a", "a"]), "list_order_unique"),
        (lambda t: t["transactions"][0].update(date_posted="2026-02-30"), "date_value"),
        (
            lambda t: t["transactions"][0].update(date_posted="20260101"),
            "date_encoding",
        ),
        (lambda t: t["transactions"][0].update(entity_id="other"), "entity_build"),
        (lambda t: t["build"][0].update(schema_version=2), "schema_version"),
        (lambda t: t["build"][0].update(schema_version=True), "physical_type"),
        (lambda t: t["build"][0].update(is_dirty=None), "closed_row"),
        (lambda t: t["build"][0].update(is_dirty=False), "closed_row"),
        (lambda t: t["build"][0].update(is_dirty=True), "closed_row"),
        (lambda t: t["transactions"][0].update(narration="e\u0301"), "text_nfc"),
    ],
)
def test_strict_boundary_mutations(
    snapshot: dict,
    mutation: Callable[[dict], object],
    code: str,
) -> None:
    mutation(snapshot)
    with pytest.raises(ContractError, match=code):
        validate_tables(snapshot)


def test_duplicate_and_misordered_rows_fail(snapshot: dict) -> None:
    snapshot["transactions"].append(deepcopy(snapshot["transactions"][0]))
    with pytest.raises(ContractError, match="primary_key_unique"):
        validate_tables(snapshot)
    snapshot["transactions"][1].update(txn_id="a")
    with pytest.raises(ContractError, match="row_order"):
        validate_tables(snapshot)


def test_foreign_keys_remain_entity_qualified(snapshot: dict) -> None:
    snapshot["event_times"] = [
        {
            "entity_id": "e",
            "record_kind": "transactions",
            "record_id": "absent",
            "date_role": "posted",
            "time_mode": "unknown",
            "timestamp_date": None,
            "timestamp_local": None,
            "timestamp_offset_minutes": None,
            "timestamp_zone": None,
            "timestamp_precision": None,
            "timestamp_fraction_digits": None,
        }
    ]
    with pytest.raises(ContractError, match="record_reference"):
        validate_tables(snapshot)


def test_posted_date_and_time_cannot_disagree(snapshot: dict) -> None:
    timestamp: dict = {
        "entity_id": "e",
        "record_kind": "transactions",
        "record_id": "t",
        "date_role": "posted",
        "time_mode": "value",
        "timestamp_date": "2026-01-02",
        "timestamp_local": None,
        "timestamp_offset_minutes": None,
        "timestamp_zone": "Pacific/Auckland",
        "timestamp_precision": "date",
        "timestamp_fraction_digits": None,
    }
    snapshot["event_times"] = [timestamp]
    with pytest.raises(ContractError, match="event_time_date"):
        validate_tables(snapshot)
    timestamp["timestamp_date"] = "2026-01-01"
    validate_tables(snapshot)
    timestamp["timestamp_offset_minutes"] = 0
    with pytest.raises(ContractError, match="date_only_time"):
        validate_tables(snapshot)


def test_authored_economic_identity_uses_stable_key_not_revision_id() -> None:
    entity = EntityId("e")
    canonical = event_id(
        entity,
        Occurrence(
            AuthoredAnchor(SemanticKey("reviewed-key")),
            SemanticKey("economic-component"),
        ),
    )
    row = {
        "entity_id": "e",
        "record_kind": "transaction",
        "record_id": canonical.value,
        "anchor_kind": "declaration",
        "anchor_id": "reviewed-key",
        "event_key": "economic-component",
        "identity_state": "active",
    }
    tables = {
        "economic_identities": [row],
        "declarations": [{"entity_id": "e", "declaration_key": "reviewed-key"}],
    }
    indexes = {"transactions": {("e", canonical.value): {"event_state": "recognized"}}}
    _economic_identities(tables, indexes)
    wrong = {**row, "record_id": "f" * 64}
    indexes["transactions"][("e", wrong["record_id"])] = {"event_state": "recognized"}
    with pytest.raises(ContractError, match="economic_identity_canonical"):
        _economic_identities({**tables, "economic_identities": [wrong]}, indexes)


@pytest.mark.parametrize(
    ("case_id", "table", "kind", "field"),
    [
        ("cash", "positions", "position", "position_id"),
        ("pools", "inventory_pools", "pool", "pool_id"),
    ],
)
def test_authored_position_and_pool_ids_match_payload_not_declaration_key(
    case_id: str, table: str, kind: str, field: str
) -> None:
    from tests.integration.conformance.corpus import load_case  # noqa: PLC0415

    case = load_case(case_id)
    row = deepcopy(case.tables[table][0])
    declaration = deepcopy(
        next(
            item
            for item in case.tables["declarations"]
            if item["declaration_id"] == row["declaration_id"]
        )
    )
    declaration["declaration_key"] = "unrelated-authored-key"
    row[field] = f"explicit-{kind}-id"
    payload_json = declaration["payload_json"]
    assert isinstance(payload_json, str)
    payload = json.loads(payload_json)
    payload[field] = row[field]
    declaration["payload_json"] = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    positions = (
        [row]
        if table == "positions"
        else [
            deepcopy(
                next(
                    item
                    for item in case.tables["positions"]
                    if item["position_id"] == row["position_id"]
                )
            )
        ]
    )
    declarations = [declaration]
    if table == "inventory_pools":
        declarations.append(
            next(
                item
                for item in case.tables["declarations"]
                if item["declaration_id"] == positions[0]["declaration_id"]
            )
        )
    tables = {
        "positions": positions,
        "inventory_pools": [row] if table == "inventory_pools" else [],
        "lots": [],
        "opening_lots": [],
    }
    indexes = {
        "declarations": {
            (item["entity_id"], item["declaration_id"]): item for item in declarations
        },
        "positions": {
            (item["entity_id"], item["position_id"]): item for item in positions
        },
    }
    _positions_and_lineage(tables, indexes)
    row[field] = "different-authored-id"
    with pytest.raises(ContractError, match=f"{kind}_identity"):
        _positions_and_lineage(tables, indexes)


def test_declaration_interval_allows_unbounded_start_with_finite_end() -> None:
    row = {"valid_from": None, "valid_to": "2026-02-01"}
    tables = {"declarations": [row], "accounts": []}
    _intervals(tables)
    row["valid_from"] = "2026-01-01"
    _intervals(tables)
    row["valid_from"] = "2026-02-01"
    with pytest.raises(ContractError, match="validity_interval"):
        _intervals(tables)


def test_redenomination_requires_explicit_different_commodity_units_effect() -> None:
    action = {
        "entity_id": "e",
        "action_id": "conversion",
        "action_kind": "redenomination",
        "commodity_id": "old",
    }
    effect = {
        "entity_id": "e",
        "action_id": "conversion",
        "effect_kind": "units",
        "input_commodity_id": "old",
        "resulting_commodity_id": "new",
        "ratio_numerator_coefficient": "1",
        "ratio_numerator_scale": 0,
        "ratio_denominator_coefficient": "100",
        "ratio_denominator_scale": 0,
        "cash_per_unit_coefficient": None,
        "cash_total_coefficient": None,
        "cash_commodity_id": None,
        "basis_per_unit_coefficient": None,
        "basis_total_coefficient": None,
        "basis_commodity_id": None,
        "symbol_id": None,
    }
    tables = {"corporate_actions": [action], "corporate_action_effects": [effect]}
    _action_effects(tables)
    for changed in (
        {"resulting_commodity_id": "old"},
        {"input_commodity_id": "other"},
    ):
        with pytest.raises(ContractError, match="redenomination_units_effect"):
            _action_effects(
                {**tables, "corporate_action_effects": [{**effect, **changed}]}
            )
    with pytest.raises(ContractError, match="effect_ratio"):
        _action_effects(
            {
                **tables,
                "corporate_action_effects": [
                    {**effect, "ratio_denominator_coefficient": "0"}
                ],
            }
        )
    with pytest.raises(ContractError, match="redenomination_units_effect"):
        _action_effects({**tables, "corporate_action_effects": []})


def test_basis_application_cannot_target_a_whole_allocation() -> None:
    application = {
        "entity_id": "e",
        "effect_id": "basis",
        "target_kind": "inventory_allocation",
        "target_id": "multi-output",
    }
    indexes = {
        "corporate_action_effects": {
            ("e", "basis"): {"effect_kind": "basis"},
        }
    }
    with pytest.raises(ContractError, match="action_application_target"):
        _action_applications({"action_applications": [application]}, indexes)


@pytest.mark.parametrize(
    ("kind", "members", "terms"),
    [
        (
            "transfer",
            [
                {"role": "outgoing", "kind": "posting", "id": "a"},
                {"role": "incoming", "kind": "posting", "id": "b"},
            ],
            None,
        ),
        (
            "complete_set",
            [
                {"role": "outcome", "kind": "commodity", "id": "a"},
                {"role": "outcome", "kind": "commodity", "id": "b"},
            ],
            "terms",
        ),
        (
            "lifecycle",
            [
                {"role": "predecessor", "kind": "transaction", "id": "failed"},
                {"role": "fee", "kind": "posting", "id": "fee"},
            ],
            None,
        ),
    ],
)
def test_closed_link_roles(kind: str, members: list[dict], terms: str | None) -> None:
    validate_link_members(kind, members, terms)
    with pytest.raises(ContractError):
        validate_link_members(kind, members[:1], terms)
