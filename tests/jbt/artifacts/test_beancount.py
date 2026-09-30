from dataclasses import replace
from datetime import date

import pytest

from jbt.artifacts.beancount import (
    BeancountPolicy,
    BeancountProjectionError,
    DirectiveLifetime,
    render_beancount,
)

type MutableTables = dict[str, list[dict[str, object]]]


@pytest.fixture
def policy() -> BeancountPolicy:
    return BeancountPolicy(
        entity_id="entity",
        date_role="posted",
        position_accounts={"cash": "Assets:Bank:Cash"},
        category_accounts={"opening": "Equity:Opening"},
        commodity_symbols={"usd": "USD"},
        position_lifetimes={"cash": DirectiveLifetime("decl", date(2024, 1, 1))},
        category_lifetimes={"opening": DirectiveLifetime("decl", date(2024, 2, 1))},
        commodity_lifetimes={"usd": DirectiveLifetime("decl", date(2023, 1, 1))},
    )


@pytest.fixture
def tables() -> MutableTables:
    return {
        "declarations": [{"entity_id": "entity", "declaration_id": "decl"}],
        "commodities": [{"entity_id": "entity", "commodity_id": "usd"}],
        "categories": [
            {
                "entity_id": "entity",
                "category_id": "opening",
                "category_type": "equity",
            },
        ],
        "positions": [
            {
                "entity_id": "entity",
                "position_id": "cash",
                "measurement_kind": "quantity",
                "inventory_method": "quantity",
            },
        ],
        "transactions": [
            {
                "entity_id": "entity",
                "txn_id": "initial",
                "event_state": "recognized",
                "event_kind": "opening",
                "date_posted": "2025-01-01",
                "payee": None,
                "narration": 'A "quoted" opening',
            },
        ],
        "book_steps": [
            {
                "entity_id": "entity",
                "step_id": "initial-step",
                "txn_id": "initial",
                "sequence": 0,
            },
        ],
        "postings": [
            {
                "entity_id": "entity",
                "posting_id": identity,
                "txn_id": "initial",
                "step_id": "initial-step",
                "commodity_id": "usd",
                "leg_kind": kind,
                "position_id": position,
                "category_id": category,
                "posting_role": "principal",
                "amount_coefficient": amount,
                "amount_scale": 0,
                "amount_source_scale": None,
                "date_posted_mode": "inherit",
            }
            for identity, kind, position, category, amount in (
                ("cash-leg", "position", "cash", None, "100"),
                ("equity-leg", "boundary", None, "opening", "-100"),
            )
        ],
        "posting_weights": [
            {
                "entity_id": "entity",
                "posting_id": identity,
                "commodity_id": "usd",
                "amount_coefficient": amount,
                "amount_scale": 0,
                "amount_source_scale": None,
            }
            for identity, amount in (("cash-leg", "100"), ("equity-leg", "-100"))
        ],
    }


def test_explicit_amounts_metadata_and_deterministic_enumeration(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    rendered = render_beancount(tables, policy=policy)
    reversed_tables = {
        name: list(reversed(rows)) for name, rows in reversed(list(tables.items()))
    }
    assert render_beancount(reversed_tables, policy=policy) == rendered
    assert "Assets:Bank:Cash 100 USD" in rendered
    assert "Equity:Opening -100 USD" in rendered
    assert 'jbt_txn_id: "initial"' in rendered
    assert 'jbt_posting_id: "cash-leg"' in rendered
    assert r"A \"quoted\" opening" in rendered
    assert "pad " not in rendered


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"commodity_symbols": {"usd": "usd"}}, "invalid_symbol"),
        (
            {"category_accounts": {"opening": "Assets:Opening"}},
            "category_root_mismatch",
        ),
        (
            {"category_accounts": {"opening": "Assets:Bank:Cash"}},
            "account_path_collision",
        ),
        ({"position_accounts": {}}, "unmapped_posting"),
        ({"date_role": "tomorrow"}, "date_role"),
    ],
)
def test_invalid_projection_policy_fails(
    tables: MutableTables,
    policy: BeancountPolicy,
    changes: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(BeancountProjectionError, match=code):
        render_beancount(tables, policy=replace(policy, **changes))


def test_symbol_collisions_do_not_merge_commodity_identities(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    tables["commodities"].append({"entity_id": "entity", "commodity_id": "other"})
    policy = replace(policy, commodity_symbols={"usd": "USD", "other": "USD"})
    with pytest.raises(BeancountProjectionError, match="symbol_collision"):
        render_beancount(tables, policy=policy)


def test_generated_symbols_ignore_source_display_names(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    policy = replace(policy, commodity_symbols={})
    original = render_beancount(tables, policy=policy)
    tables["commodities"][0]["name"] = "Completely different display name"
    assert render_beancount(tables, policy=policy) == original
    assert "commodity J" in original


@pytest.mark.parametrize(
    ("mode", "override", "code"),
    [
        ("unknown", None, "unknown_posting_date"),
        ("value", date(2025, 1, 2), "split_posting_dates_requires_clearing"),
    ],
)
def test_leg_date_differences_never_create_clearing_bookings(
    tables: MutableTables,
    policy: BeancountPolicy,
    mode: str,
    override: date | None,
    code: str,
) -> None:
    tables["postings"][0]["date_posted_mode"] = mode
    tables["postings"][0]["date_posted"] = (
        override.isoformat() if override is not None else None
    )
    with pytest.raises(BeancountProjectionError, match=code):
        render_beancount(tables, policy=policy)


def test_unbalanced_weights_are_not_repaired(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    tables["posting_weights"][1]["amount_coefficient"] = "-99"
    tables["postings"][1]["amount_coefficient"] = "-99"
    with pytest.raises(BeancountProjectionError, match="unbalanced_weights"):
        render_beancount(tables, policy=policy)


def test_large_exact_upstream_amount_has_named_sink_limit(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    for name in ("postings", "posting_weights"):
        for row in tables[name]:
            row["amount_coefficient"] = (
                "-" if str(row["amount_coefficient"]).startswith("-") else ""
            ) + "12345678901234567890123456789"
    with pytest.raises(BeancountProjectionError, match="beancount_decimal_precision"):
        render_beancount(tables, policy=policy)


@pytest.mark.parametrize("measurement", ["reference", "pooled"])
def test_inventory_outside_supported_slice_fails(
    tables: MutableTables, policy: BeancountPolicy, measurement: str
) -> None:
    field = "inventory_method" if measurement == "pooled" else "measurement_kind"
    tables["positions"][0][field] = measurement
    with pytest.raises(BeancountProjectionError, match="unsupported_inventory"):
        render_beancount(tables, policy=policy)


def test_nonparticipating_event_does_not_create_financial_postings(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    tables["transactions"][0]["event_state"] = "pending"
    text = render_beancount(tables, policy=policy)
    assert " 100 USD" not in text
    assert 'jbt_txn_id: "initial"' not in text


def test_source_and_correction_references_are_retained(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    tables["provenance"] = [
        {
            "entity_id": "entity",
            "record_kind": "transactions",
            "record_id": '["entity","initial"]',
            "evidence_kind": "declaration",
            "evidence_id": "correction-1",
        },
        {
            "entity_id": "entity",
            "record_kind": "postings",
            "record_id": '["entity","cash-leg"]',
            "evidence_kind": "source_record",
            "evidence_id": "source-1",
        },
    ]
    text = render_beancount(tables, policy=policy)
    assert "declaration:correction-1" in text
    assert "source_record:source-1" in text


def test_declaration_lifetime_is_not_inferred_from_first_posting(
    tables: MutableTables, policy: BeancountPolicy
) -> None:
    with pytest.raises(BeancountProjectionError, match="directive_lifetime_required"):
        render_beancount(tables, policy=replace(policy, commodity_lifetimes={}))
    with pytest.raises(BeancountProjectionError, match="outside_directive_lifetime"):
        render_beancount(
            tables,
            policy=replace(
                policy,
                position_lifetimes={
                    "cash": DirectiveLifetime("decl", date(2025, 1, 2))
                },
            ),
        )
