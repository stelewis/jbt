import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from beancount import loader
from beancount.core import convert, data
from beancount.core.position import Cost
from beancount.ops.validation import ValidationError
from tests.integration.conformance.corpus import FixtureCase, fixture_cases, load_case

from jbt.artifacts.beancount import (
    BeancountPolicy,
    BeancountProjectionError,
    DirectiveLifetime,
    render_beancount,
)
from jbt.domain.numbers import ExactDecimal

pytestmark = pytest.mark.integration
type MutableTables = dict[str, list[dict[str, object]]]


def _numeric(name: str, text: str) -> dict[str, object]:
    value = ExactDecimal.parse(text)
    return {
        f"{name}_coefficient": value.coefficient,
        f"{name}_scale": value.scale,
        f"{name}_source_scale": value.source_scale,
    }


def _row(**values: object) -> dict[str, object]:
    return {
        "entity_id": "synthetic",
        **{
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in values.items()
        },
    }


def _corpus_policy(case_id: str) -> BeancountPolicy:
    """Supply an explicit synthetic lifecycle to the corpus projection."""
    case = load_case(case_id)
    tables = case.tables
    opened = date(2026, 1, 1)
    declaration = str(tables["declarations"][0]["declaration_id"])
    return BeancountPolicy(
        entity_id=str(tables["transactions"][0]["entity_id"]),
        date_role="posted",
        position_lifetimes={
            str(row["position_id"]): DirectiveLifetime(
                str(row["declaration_id"]), opened
            )
            for row in tables["positions"]
        },
        category_lifetimes={
            str(row["category_id"]): DirectiveLifetime(
                str(row["declaration_id"]), opened
            )
            for row in tables["categories"]
        },
        commodity_lifetimes={
            str(row["commodity_id"]): DirectiveLifetime(declaration, opened)
            for row in tables["commodities"]
        },
        position_accounts={
            str(row["position_id"]): (
                "Liabilities" if row["position_kind"] == "liability" else "Assets"
            )
            + f":Fixture:P{row['position_id']}"
            for row in tables["positions"]
        },
        category_accounts={
            str(row["category_id"]): {
                "equity": "Equity",
                "income": "Income",
                "expense": "Expenses",
            }[str(row["category_type"])]
            + f":Fixture:C{row['category_id']}"
            for row in tables["categories"]
        },
        commodity_symbols={},
        note_accounts=(
            {
                str(row["txn_id"]): f"Assets:Fixture:P{case.aliases['cash-A']}"
                for row in tables["transactions"]
                if row["event_state"] == "recognized"
            }
            if case_id == "identity-history"
            else (
                {case.aliases["rename"]: f"Assets:Fixture:P{case.aliases['AQ']}"}
                if case_id == "corporate-variants"
                else {}
            )
        ),
    )


def _corpus_decimal(row: dict[str, object], field: str) -> Decimal:
    return Decimal(str(row[f"{field}_coefficient"])).scaleb(
        -int(str(row[f"{field}_scale"]))
    )


_CORPUS_LIMITS = {
    "brokerage": "unsupported_assertion_scope",
    "compound-expensed": "split_posting_dates_requires_clearing",
    "compound-capitalized": "split_posting_dates_requires_clearing",
    "compound-backfill": "split_posting_dates_requires_clearing",
    "pools": "unsupported_inventory",
    "reference": "unsupported_inventory",
    "reference-variants": "unsupported_inventory",
    "rounding": "nonterminating_unit_cost",
    "outcomes": "unbalanced_weights",
    "breadth/perpetual": "unsupported_inventory",
    "breadth/onchain": "unbalanced_weights",
    "empty-scope": "unsupported_assertion_scope",
    "ordering-split-buy": "split_posting_dates_requires_clearing",
}


def _loaded_number(posting: data.Posting) -> Decimal:
    assert posting.units is not None
    assert posting.units.number is not None
    return posting.units.number


def _loaded_cost(posting: data.Posting) -> Cost:
    assert isinstance(posting.cost, Cost)
    return posting.cost


def _assert_corpus_totals(
    case: FixtureCase,
    policy: BeancountPolicy,
    actual: dict[str, list[data.Posting]],
) -> None:
    quantities = {
        **case.expected_answers.get("positions", {}),
        **case.expected_answers.get("quantities", {}),
    }
    for alias, value in quantities.items():
        account = policy.position_accounts[case.aliases[alias]]
        assert sum(
            (
                _loaded_number(leg)
                for legs in actual.values()
                for leg in legs
                if leg.account == account
            ),
            Decimal(0),
        ) == Decimal(value)
    for alias, values in case.expected_answers.get("inventory", {}).items():
        account = policy.position_accounts[case.aliases[alias]]
        legs = [
            leg
            for postings in actual.values()
            for leg in postings
            if leg.account == account
        ]
        assert sum(
            (_loaded_number(leg) for leg in legs),
            Decimal(0),
        ) == Decimal(values["units"])
        assert sum(
            (
                _loaded_number(leg) * leg.cost.number
                for leg in legs
                if isinstance(leg.cost, Cost)
            ),
            Decimal(0),
        ) == Decimal(values["principal"])


@pytest.mark.parametrize("case_id", [case.case_id for case in fixture_cases()])
def test_complete_corpus_beancount_handoff(case_id: str) -> None:
    case = load_case(case_id)
    policy = _corpus_policy(case_id)
    if case_id in _CORPUS_LIMITS:
        with pytest.raises(BeancountProjectionError) as failure:
            render_beancount(case.tables, policy=policy)
        assert failure.value.code == _CORPUS_LIMITS[case_id]
        return
    text = render_beancount(case.tables, policy=policy)
    entries, errors, _ = loader.load_string(text)
    assert errors == [], (case_id, errors)
    if case_id == "identity-history":
        notes = [entry for entry in entries if isinstance(entry, data.Note)]
        assert {note.meta["jbt_txn_id"] for note in notes} == {
            row["txn_id"]
            for row in case.tables["transactions"]
            if row["event_state"] == "recognized"
        }
        assert not any(isinstance(entry, data.Transaction) for entry in entries)
    actual: dict[str, list[data.Posting]] = {}
    transaction_dates = {}
    for entry in entries:
        if isinstance(entry, data.Transaction):
            assert entry.meta is not None
            transaction_dates[entry.meta["jbt_txn_id"]] = entry.date
            for posting in entry.postings:
                assert posting.meta is not None
                actual.setdefault(posting.meta["jbt_posting_id"], []).append(posting)
    expected = {
        str(row["posting_id"]): row
        for row in case.tables["postings"]
        if any(
            txn["txn_id"] == row["txn_id"]
            and txn["event_state"] == "recognized"
            and txn["event_kind"] != "note"
            for txn in case.tables["transactions"]
        )
    }
    assert set(actual) == set(expected)
    for identity, posting in expected.items():
        rendered = actual[identity]
        units = Decimal(0)
        for leg in rendered:
            assert leg.units is not None
            assert leg.units.number is not None
            units += leg.units.number
        assert units == _corpus_decimal(posting, "amount")
        target = str(posting["position_id"] or posting["category_id"])
        account = (
            policy.position_accounts
            if posting["position_id"]
            else policy.category_accounts
        )[target]
        assert all(leg.account == account for leg in rendered)
        transaction = next(
            txn
            for txn in case.tables["transactions"]
            if txn["txn_id"] == posting["txn_id"]
        )
        assert transaction_dates[posting["txn_id"]] == date.fromisoformat(
            str(transaction["date_posted"])
        )
        changes = [
            change
            for change in case.tables["inventory_changes"]
            if change["posting_id"] == identity
        ]
        if changes:
            assert {
                leg.cost.label for leg in rendered if isinstance(leg.cost, Cost)
            } == {change["lot_id"] for change in changes}
        weights = [
            weight
            for weight in case.tables["posting_weights"]
            if weight["posting_id"] == identity
        ]
        if weights:
            assert sum(
                (convert.get_weight(leg).number for leg in rendered), Decimal(0)
            ) == sum(
                (_corpus_decimal(weight, "amount") for weight in weights), Decimal(0)
            )
    _assert_corpus_totals(case, policy, actual)


def test_mixed_merger_retains_child_basis_cash_claim_and_book_result() -> None:
    case = load_case("mixed-merger")
    policy = _corpus_policy("mixed-merger")
    entries, errors, _ = loader.load_string(
        render_beancount(case.tables, policy=policy)
    )
    assert errors == []
    action = next(
        entry
        for entry in entries
        if isinstance(entry, data.Transaction)
        and entry.meta["jbt_txn_id"] == case.aliases["merger"]
    )
    assert action.meta["jbt_action_id"] == case.aliases["merger-action"]
    by_identity = {}
    for posting in action.postings:
        assert posting.meta is not None
        by_identity[posting.meta["jbt_posting_id"]] = posting
    source = by_identity[case.aliases["merger-out"]]
    target = by_identity[case.aliases["merger-in"]]
    assert (_loaded_number(source), _loaded_cost(source).number) == (
        Decimal(-10),
        Decimal(10),
    )
    assert (_loaded_number(target), _loaded_cost(target).number) == (
        Decimal(5),
        Decimal(14),
    )
    assert _loaded_cost(target).label == case.aliases["child"]
    assert _loaded_cost(target).date == date.fromisoformat(
        case.expected_answers["child_origin_date"]
    )
    assert _loaded_number(by_identity[case.aliases["merger-receivable"]]) == Decimal(
        case.expected_answers["proceeds"]
    )
    assert _loaded_number(by_identity[case.aliases["merger-result"]]) == -Decimal(
        case.expected_answers["gross_result"]
    )


def test_reference_variants_retains_named_unvalued_reference_limit() -> None:
    case = load_case("reference-variants")
    with pytest.raises(BeancountProjectionError) as failure:
        render_beancount(case.tables, policy=_corpus_policy(case.case_id))
    assert failure.value.code == "unsupported_inventory"
    assert failure.value.record_id == case.aliases["future-open"]


def test_reference_variants_balanced_cash_slice_loads() -> None:
    case = load_case("reference-variants")
    included = {
        case.aliases[alias]
        for alias in (
            "collateral-open",
            "daily",
            "withdraw",
            "variant-partial-reset",
            "collateral-only-variation",
        )
    }
    tables = {name: list(rows) for name, rows in case.tables.items()}
    tables["transactions"] = [
        row for row in tables["transactions"] if row["txn_id"] in included
    ]
    tables["book_steps"] = [
        row for row in tables["book_steps"] if row["txn_id"] in included
    ]
    tables["postings"] = [
        row for row in tables["postings"] if row["txn_id"] in included
    ]
    posting_ids = {row["posting_id"] for row in tables["postings"]}
    tables["posting_weights"] = [
        row for row in tables["posting_weights"] if row["posting_id"] in posting_ids
    ]
    tables["prices"] = []
    entries, errors, _ = loader.load_string(
        render_beancount(tables, policy=_corpus_policy(case.case_id))
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert {entry.meta["jbt_txn_id"] for entry in transactions} == included
    margin_account = _corpus_policy(case.case_id).position_accounts[
        case.aliases["margin"]
    ]
    assert sum(
        _loaded_number(posting)
        for transaction in transactions
        for posting in transaction.postings
        if posting.account == margin_account
    ) == Decimal(case.expected_answers["collateral"])
    reset = next(
        entry
        for entry in transactions
        if entry.meta["jbt_txn_id"] == case.aliases["variant-partial-reset"]
    )
    assert sorted(_loaded_number(posting) for posting in reset.postings) == [
        Decimal(-10),
        Decimal(10),
    ]


def test_corporate_variants_loader_preserves_action_targets_and_values() -> None:
    case = load_case("corporate-variants")
    policy = _corpus_policy(case.case_id)
    entries, errors, _ = loader.load_string(
        render_beancount(case.tables, policy=policy)
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    by_change = {
        posting.meta["jbt_change_id"]: posting
        for transaction in transactions
        for posting in transaction.postings
        if posting.meta is not None and "jbt_change_id" in posting.meta
    }
    for alias, units, cost, lot in (
        ("sale-source", "-2", "10", "root"),
        ("reverse-source", "-8", "10", "root"),
        ("reverse-target", "1.6", "50", "root"),
        ("spinoff-source", "-1.6", "50", "root"),
        ("spinoff-parent", "1.6", "37.5", "root"),
        ("spinoff-child", "0.8", "25", "child"),
        ("capital-source", "-1.6", "37.5", "root"),
        ("capital-target", "1.6", "25", "root"),
        ("reinvest-target", "0.5", "20", "reinvested"),
    ):
        posting = by_change[case.aliases[alias]]
        assert _loaded_number(posting) == Decimal(units)
        assert _loaded_cost(posting).number == Decimal(cost)
        assert _loaded_cost(posting).label == case.aliases[lot]
    parent_posting = by_change[case.aliases["spinoff-parent"]]
    child_posting = by_change[case.aliases["spinoff-child"]]
    assert parent_posting.meta is not None
    assert child_posting.meta is not None
    parent = json.loads(parent_posting.meta["jbt_action_effect_ids"])
    child = json.loads(child_posting.meta["jbt_action_effect_ids"])
    assert case.aliases["spinoff-parent-basis"] in parent
    assert case.aliases["spinoff-parent-basis"] not in child
    assert case.aliases["spinoff-child-basis"] in child
    assert case.aliases["spinoff-child-basis"] not in parent
    by_posting = {
        posting.meta["jbt_posting_id"]: posting
        for transaction in transactions
        for posting in transaction.postings
        if posting.meta is not None and "jbt_posting_id" in posting.meta
    }
    assert _loaded_number(by_posting[case.aliases["sell-result"]]) == -Decimal(
        case.expected_answers["pre_action_gain"]
    )
    assert _loaded_number(by_posting[case.aliases["capital-cash"]]) == Decimal(
        case.expected_answers["return_of_capital"]
    )
    assert _loaded_number(by_posting[case.aliases["new-in"]]) == Decimal(
        case.expected_answers["positions"]["new-cash"]
    )
    assert convert.get_weight(by_posting[case.aliases["new-in"]]).number == Decimal(20)
    new_units = by_posting[case.aliases["new-in"]].units
    old_units = by_posting[case.aliases["old-out"]].units
    assert new_units is not None
    assert old_units is not None
    assert new_units.currency != old_units.currency
    notes = [entry for entry in entries if isinstance(entry, data.Note)]
    assert [entry.meta["jbt_action_id"] for entry in notes] == [
        case.aliases["rename-action"]
    ]
    assert json.loads(notes[0].meta["jbt_action_effect_ids"]) == [
        case.aliases["rename-effect"]
    ]
    assert json.loads(notes[0].meta["jbt_symbol_ids"]) == [case.aliases["new-symbol"]]
    assert json.loads(notes[0].meta["jbt_symbols"]) == ["Q-NEW"]
    assert not any(
        entry.meta["jbt_txn_id"] == case.aliases["rename"] for entry in transactions
    )
    actual: dict[str, list[data.Posting]] = {}
    for transaction in transactions:
        for posting in transaction.postings:
            assert posting.meta is not None
            actual.setdefault(posting.meta["jbt_posting_id"], []).append(posting)
    _assert_corpus_totals(case, policy, actual)


def test_trade_chain_real_loader_preserves_both_roll_members_and_lot_costs() -> None:
    case = load_case("trade-chain")
    policy = _corpus_policy(case.case_id)
    entries, errors, _ = loader.load_string(
        render_beancount(case.tables, policy=policy)
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert [entry.meta["jbt_txn_id"] for entry in transactions] == [
        case.aliases[alias] for alias in ("open", "roll-one", "roll-two", "close")
    ]
    assert len(transactions) == int(case.expected_answers["chain_members"])
    members = [
        tuple(link)
        for transaction in transactions
        for link in json.loads(transaction.meta["jbt_link_members"])
    ]
    assert {link[0] for link in members} == {case.aliases["strategy"]}
    assert [link[2] for link in members] == ["open", "roll", "roll", "close"]
    assert sum(link[2] == "roll" for link in members) == int(
        case.expected_answers["roll_members"]
    )
    by_change = {
        posting.meta["jbt_change_id"]: posting
        for transaction in transactions
        for posting in transaction.postings
        if posting.meta is not None and "jbt_change_id" in posting.meta
    }
    assert sum(
        -convert.get_weight(by_change[case.aliases[alias]]).number
        for alias in ("first-source", "second-source", "third-source")
    ) == Decimal(case.expected_answers["realized_principal"])
    for alias, cost, lot in (
        ("first-source", "10", "first"),
        ("second-source", "20", "second"),
        ("third-source", "12.5", "third"),
    ):
        assert _loaded_cost(by_change[case.aliases[alias]]).number == Decimal(cost)
        assert _loaded_cost(by_change[case.aliases[alias]]).label == case.aliases[lot]
    actual: dict[str, list[data.Posting]] = {}
    for transaction in transactions:
        for posting in transaction.postings:
            assert posting.meta is not None
            actual.setdefault(posting.meta["jbt_posting_id"], []).append(posting)
    _assert_corpus_totals(case, policy, actual)


def test_time_and_loan_loader_books_only_recognized_components() -> None:
    case = load_case("time-and-loan")
    policy = _corpus_policy(case.case_id)
    entries, errors, _ = loader.load_string(
        render_beancount(case.tables, policy=policy)
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert {entry.meta["jbt_txn_id"] for entry in transactions} == {
        row["txn_id"]
        for row in case.tables["transactions"]
        if row["event_state"] == "recognized"
    }
    by_posting = {
        posting.meta["jbt_posting_id"]: posting
        for transaction in transactions
        for posting in transaction.postings
        if posting.meta is not None
    }
    assert case.aliases["unallocated-gross"] not in by_posting
    for alias, expected in (
        ("mortgage-cash", "-12"),
        ("mortgage-principal", "10"),
        ("mortgage-interest", "2"),
        ("fold-cash", "9"),
        ("late-local-cash", "1"),
    ):
        assert _loaded_number(by_posting[case.aliases[alias]]) == Decimal(expected)
    assert _loaded_number(by_posting[case.aliases["mortgage-interest"]]) == Decimal(
        case.expected_answers["interest_expense"]
    )
    actual = {identity: [posting] for identity, posting in by_posting.items()}
    _assert_corpus_totals(case, policy, actual)


def test_three_lot_disposal_loader_uses_three_selected_costs() -> None:
    case = load_case("three-lot-disposal")
    policy = _corpus_policy(case.case_id)
    entries, errors, _ = loader.load_string(
        render_beancount(case.tables, policy=policy)
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    sale = next(
        entry
        for entry in transactions
        if entry.meta["jbt_txn_id"] == case.aliases["three-way-sale"]
    )
    selected = {
        _loaded_cost(posting).label: posting
        for posting in sale.postings
        if posting.meta is not None
        and posting.meta["jbt_posting_id"] == case.aliases["sale-q"]
    }
    assert len(selected) == int(case.expected_answers["selected_origins"])
    for alias, units, cost in (
        ("L1", "-1", "10"),
        ("L2", "-1", "20"),
        ("L3", "-2", "30"),
    ):
        posting = selected[case.aliases[alias]]
        assert _loaded_number(posting) == Decimal(units)
        assert _loaded_cost(posting).number == Decimal(cost)
    assert -sum(
        convert.get_weight(posting).number for posting in selected.values()
    ) == Decimal(case.expected_answers["released_principal"])
    sale_legs = {
        posting.meta["jbt_posting_id"]: posting
        for posting in sale.postings
        if posting.meta is not None
    }
    assert _loaded_number(sale_legs[case.aliases["sale-cash"]]) == Decimal(
        case.expected_answers["proceeds"]
    )
    assert _loaded_number(sale_legs[case.aliases["sale-gain"]]) == -Decimal(
        case.expected_answers["gain"]
    )
    actual: dict[str, list[data.Posting]] = {}
    for transaction in transactions:
        for posting in transaction.postings:
            assert posting.meta is not None
            actual.setdefault(posting.meta["jbt_posting_id"], []).append(posting)
    _assert_corpus_totals(case, policy, actual)


@pytest.fixture
def policy() -> BeancountPolicy:
    return BeancountPolicy(
        entity_id="synthetic",
        date_role="traded",
        position_accounts={
            "cash": "Assets:Broker:Cash",
            "stock": "Assets:Broker:Stock",
            "loan": "Liabilities:Broker:Loan",
        },
        category_accounts={
            "opening": "Equity:Opening",
            "fees": "Expenses:Fees",
            "gain": "Income:Book-Result",
        },
        commodity_symbols={"dollars": "USD", "shares": "XYZ"},
        position_lifetimes={
            identity: DirectiveLifetime("decl", date(2024, 1, 1))
            for identity in ("cash", "stock", "loan")
        },
        category_lifetimes={
            identity: DirectiveLifetime("decl", date(2024, 1, 1))
            for identity in ("opening", "fees", "gain")
        },
        commodity_lifetimes={
            identity: DirectiveLifetime("decl", date(2024, 1, 1))
            for identity in ("dollars", "shares")
        },
    )


@pytest.fixture
def history() -> MutableTables:
    tables: MutableTables = {
        "declarations": [_row(declaration_id="decl")],
        "commodities": [
            _row(commodity_id="dollars"),
            _row(commodity_id="shares"),
        ],
        "positions": [
            _row(
                position_id=identity,
                account_id="broker",
                measurement_kind=measurement,
                inventory_method=method,
            )
            for identity, measurement, method in (
                ("cash", "quantity", "quantity"),
                ("stock", "cost", "individual"),
                ("loan", "quantity", "quantity"),
            )
        ],
        "categories": [
            _row(category_id=identity, category_type=kind)
            for identity, kind in (
                ("opening", "equity"),
                ("fees", "expense"),
                ("gain", "income"),
            )
        ],
        "lots": [
            _row(
                lot_id="selected-lot",
                acquisition_date=date(2025, 1, 2),
                book_commodity_id="dollars",
            ),
        ],
        "transactions": [],
        "postings": [],
        "posting_weights": [],
        "book_steps": [],
        "inventory_changes": [],
        "inventory_allocations": [],
    }
    events = [
        (
            "opening",
            "opening",
            [
                ("cash", "100", "dollars", "100", "cash"),
                ("loan", "-20", "dollars", "-20", "principal"),
                ("opening", "-80", "dollars", "-80", "principal"),
            ],
        ),
        (
            "purchase",
            "trade",
            [
                ("stock", "3", "shares", "9", "principal"),
                ("cash", "-10", "dollars", "-10", "cash"),
                ("fees", "1", "dollars", "1", "fee"),
            ],
        ),
        (
            "sale",
            "trade",
            [
                ("stock", "-1", "shares", "-3", "principal"),
                ("cash", "5", "dollars", "5", "cash"),
                ("fees", "1", "dollars", "1", "fee"),
                ("gain", "-3", "dollars", "-3", "book_result"),
            ],
        ),
    ]
    for sequence, (identity, kind, legs) in enumerate(events):
        when = date(2025, 1, sequence + 1)
        tables["transactions"].append(
            _row(
                txn_id=identity,
                event_kind=kind,
                event_state="recognized",
                date_traded=when,
                date_settled=date(2025, 1, sequence + 3),
                narration=identity,
                payee="Synthetic Broker",
            )
        )
        tables["book_steps"].append(
            _row(
                step_id=identity,
                txn_id=identity,
                sequence=sequence,
            )
        )
        for target, units, commodity, weight, role in legs:
            is_position = target in {"cash", "stock", "loan"}
            posting_id = f"{identity}-{target}"
            tables["postings"].append(
                _row(
                    posting_id=posting_id,
                    txn_id=identity,
                    step_id=identity,
                    position_id=target if is_position else None,
                    category_id=None if is_position else target,
                    leg_kind="position" if is_position else "boundary",
                    posting_role=role,
                    commodity_id=commodity,
                    date_traded_mode="inherit",
                    date_settled_mode="inherit",
                    **_numeric("amount", units),
                )
            )
            tables["posting_weights"].append(
                _row(
                    posting_id=posting_id,
                    commodity_id="dollars",
                    **_numeric("amount", weight),
                )
            )
    for event, units, principal, kind in (
        ("purchase", "3", "9", "acquisition"),
        ("sale", "-1", "-3", "reduction"),
    ):
        tables["inventory_allocations"].append(
            _row(
                allocation_id=event,
                allocation_kind=kind,
            )
        )
        tables["inventory_changes"].append(
            _row(
                change_id=event,
                allocation_id=event,
                posting_id=f"{event}-stock",
                position_id="stock",
                lot_id="selected-lot",
                pool_id=None,
                **_numeric("units_delta", units),
                **_numeric("principal_delta", principal),
                **_numeric("capitalized_fee_delta", "0"),
            )
        )
    tables["assertion_scopes"] = [
        _row(scope_id="holdings", scope_kind="positions", measurement="units"),
    ]
    tables["balance_assertions"] = [
        _row(
            assertion_set_id="start",
            scope_id="holdings",
            date=date(2025, 1, 1),
            assertion_kind="opening",
            is_complete=False,
        ),
        _row(
            assertion_set_id="end",
            scope_id="holdings",
            date=date(2025, 1, 3),
            assertion_kind="closing",
            is_complete=False,
        ),
    ]
    tables["balances"] = [
        _row(
            balance_id=identity,
            assertion_set_id=assertion,
            position_id=position,
            commodity_id=commodity,
            **_numeric("amount", value),
        )
        for identity, assertion, position, commodity, value in (
            ("start-cash", "start", "cash", "dollars", "0"),
            ("end-cash", "end", "cash", "dollars", "95"),
            ("end-stock", "end", "stock", "shares", "2"),
        )
    ]
    return tables


def test_real_loader_preserves_units_selected_lot_weights_fees_and_gain(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    text = render_beancount(history, policy=policy)
    entries, errors, _ = loader.load_string(text)
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert [entry.date for entry in transactions] == [
        date(2025, 1, 1),
        date(2025, 1, 2),
        date(2025, 1, 3),
    ]
    quantities: dict[tuple[str, str], Decimal] = {}
    stock_weights = []
    for transaction in transactions:
        assert all(posting.units is not None for posting in transaction.postings)
        for posting in transaction.postings:
            assert posting.units is not None
            assert isinstance(posting.units.number, Decimal)
            key = (posting.account, posting.units.currency)
            quantities[key] = quantities.get(key, Decimal(0)) + posting.units.number
            if posting.account == "Assets:Broker:Stock":
                assert isinstance(posting.cost, Cost)
                assert posting.cost.label == "selected-lot"
                assert posting.cost.date == date(2025, 1, 2)
                assert posting.cost.number == Decimal(3)
                stock_weights.append(convert.get_weight(posting).number)
    assert stock_weights == [Decimal(9), Decimal(-3)]
    assert quantities == {
        ("Assets:Broker:Cash", "USD"): Decimal(95),
        ("Assets:Broker:Stock", "XYZ"): Decimal(2),
        ("Liabilities:Broker:Loan", "USD"): Decimal(-20),
        ("Equity:Opening", "USD"): Decimal(-80),
        ("Expenses:Fees", "USD"): Decimal(2),
        ("Income:Book-Result", "USD"): Decimal(-3),
    }
    assert 'jbt_date_settled: "2025-01-05"' in text


def test_assertions_translate_closing_only_to_next_day(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    balances = [entry for entry in entries if isinstance(entry, data.Balance)]
    assert [(entry.date, entry.amount.number) for entry in balances] == [
        (date(2025, 1, 1), Decimal(0)),
        (date(2025, 1, 4), Decimal(95)),
        (date(2025, 1, 4), Decimal(2)),
    ]
    assert all(entry.tolerance == Decimal(0) for entry in balances)


def test_real_loader_cannot_preserve_ten_over_three_total_cost() -> None:
    text = """option "tolerance_multiplier" "0"
option "inferred_tolerance_default" "*:0"
2025-01-01 open Assets:Stock
2025-01-01 open Assets:Cash
2025-01-02 * "Authoritative total ten, three units"
  Assets:Stock 3 XYZ {{10 USD, 2025-01-02, "lot"}}
  Assets:Cash -10 USD
2025-01-03 * "One unit reduction"
  Assets:Stock -1 XYZ {2025-01-02, "lot"}
  Assets:Cash 3.333333333333333333333333333 USD
"""
    entries, errors, _ = loader.load_string(text)
    assert any(isinstance(error, ValidationError) for error in errors)
    purchase = next(entry for entry in entries if isinstance(entry, data.Transaction))
    stock = purchase.postings[0]
    assert isinstance(stock.cost, Cost)
    assert stock.cost.number == Decimal("3.333333333333333333333333333")
    assert convert.get_weight(stock).number != Decimal(10)


def test_nonterminating_book_cost_fails_before_output(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["inventory_changes"][0].update(_numeric("principal_delta", "10"))
    for row in history["posting_weights"]:
        if row["posting_id"] == "purchase-stock":
            row.update(_numeric("amount", "10"))
        elif row["posting_id"] == "purchase-cash":
            row.update(_numeric("amount", "-11"))
    for row in history["postings"]:
        if row["posting_id"] == "purchase-cash":
            row.update(_numeric("amount", "-11"))
    with pytest.raises(BeancountProjectionError, match="nonterminating_unit_cost"):
        render_beancount(history, policy=policy)


def test_selected_settlement_policy_does_not_invent_trade_date_clearing(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["balance_assertions"] = []
    history["balances"] = []
    entries, errors, _ = loader.load_string(
        render_beancount(history, policy=replace(policy, date_role="settled"))
    )
    assert errors == []
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    assert [entry.date for entry in transactions] == [
        date(2025, 1, 3),
        date(2025, 1, 4),
        date(2025, 1, 5),
    ]
    assert sum(len(entry.postings) for entry in transactions) == 10


def test_real_loader_posting_date_metadata_does_not_book_a_second_date() -> None:
    entries, errors, _ = loader.load_string(
        "2026-01-01 open Assets:Stock\n"
        "2026-01-01 open Assets:Cash\n"
        '2026-01-30 * "different posting date"\n'
        "  Assets:Stock 1 XYZ\n"
        "    date: 2026-02-02\n"
        "  Assets:Cash -1 XYZ\n"
    )
    assert errors == []
    transaction = next(
        entry for entry in entries if isinstance(entry, data.Transaction)
    )
    assert transaction.date == date(2026, 1, 30)
    assert transaction.postings[0].meta is not None
    assert transaction.postings[1].meta is not None
    assert transaction.postings[0].meta["date"] == date(2026, 2, 2)
    assert transaction.postings[1].meta.get("date") is None


@pytest.mark.parametrize(
    ("postings", "unbalanced"),
    [
        (
            "  Assets:Native -2 NATIVE\n  Assets:Wrapped 2 WRAPPED\n",
            "(-2 NATIVE, 2 WRAPPED)",
        ),
        ("  Assets:Perpetual 1 PERP\n", "(1 PERP)"),
    ],
)
def test_real_loader_cannot_balance_unvalued_cross_commodity_or_reference(
    postings: str, unbalanced: str
) -> None:
    _, errors, _ = loader.load_string(
        "2026-01-01 open Assets:Native\n"
        "2026-01-01 open Assets:Wrapped\n"
        "2026-01-01 open Assets:Perpetual\n"
        '2026-01-02 * "unvalued position change"\n' + postings
    )
    assert any(
        isinstance(error, ValidationError) and unbalanced in error.message
        for error in errors
    )


def test_recognized_note_is_a_note_directive_not_a_financial_event(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["transactions"].append(
        _row(
            txn_id="memo",
            event_kind="note",
            event_state="recognized",
            date_traded=date(2025, 1, 2),
            narration="Invented custody note",
        )
    )
    text = render_beancount(
        history,
        policy=replace(
            policy, note_accounts={"memo": policy.position_accounts["cash"]}
        ),
    )
    entries, errors, _ = loader.load_string(text)
    assert errors == []
    notes = [entry for entry in entries if isinstance(entry, data.Note)]
    assert len(notes) == 1
    assert notes[0].meta["jbt_txn_id"] == "memo"
    assert notes[0].account == policy.position_accounts["cash"]
    assert len([entry for entry in entries if isinstance(entry, data.Transaction)]) == 3


@pytest.mark.parametrize("kind", ["point", "complete", "value"])
def test_upstream_only_assertions_are_not_claimed_as_ledger_checks(
    history: MutableTables, policy: BeancountPolicy, kind: str
) -> None:
    if kind == "complete":
        history["balance_assertions"][0]["is_complete"] = True
    elif kind == "value":
        history["assertion_scopes"][0]["measurement"] = "value"
    else:
        history["balance_assertions"][0]["assertion_kind"] = kind
    with pytest.raises(BeancountProjectionError, match="unsupported_assertion_scope"):
        render_beancount(history, policy=policy)


def test_negative_market_price_loads_as_exact_price_directive(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["prices"] = [
        _row(
            price_id="negative",
            date=date(2025, 1, 3),
            commodity_id="shares",
            quote_commodity_id="dollars",
            **_numeric("rate", "-1"),
        ),
    ]
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    price = next(entry for entry in entries if isinstance(entry, data.Price))
    assert price.amount.number == Decimal(-1)


def test_real_parser_rejects_negative_posting_price_annotation() -> None:
    _, errors, _ = loader.load_string(
        "2025-01-01 open Assets:Stock\n"
        "2025-01-01 open Assets:Cash\n"
        '2025-01-02 * "negative posting price"\n'
        "  Assets:Stock 1 XYZ @ -1 USD\n"
        "  Assets:Cash 1 USD\n"
    )
    assert any("Negative prices are not allowed" in error.message for error in errors)


def test_same_day_projection_preserves_book_order_not_lexical_id(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["balance_assertions"] = []
    history["balances"] = []
    history["transactions"][1]["date_traded"] = "2025-01-03"
    history["transactions"][2]["txn_id"] = "a-sale"
    history["book_steps"][2]["txn_id"] = "a-sale"
    for posting in history["postings"]:
        if posting["txn_id"] == "sale":
            posting["txn_id"] = "a-sale"
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    assert [
        entry.meta["jbt_txn_id"]
        for entry in entries
        if isinstance(entry, data.Transaction)
    ] == ["opening", "purchase", "a-sale"]


def test_multiple_lots_follow_recorded_selection_not_fifo(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    original = history["inventory_changes"][0]
    original["lot_id"] = "earlier-lot"
    original.update(_numeric("units_delta", "1"))
    original.update(_numeric("principal_delta", "1"))
    history["lots"].append(
        {
            **history["lots"][0],
            "lot_id": "earlier-lot",
        }
    )
    history["inventory_changes"].append(
        {
            **original,
            "change_id": "second-purchase",
            "lot_id": "selected-lot",
            "allocation_id": "second-purchase",
            **_numeric("units_delta", "2"),
            **_numeric("principal_delta", "8"),
        }
    )
    history["inventory_allocations"].append(
        _row(
            allocation_id="second-purchase",
            allocation_kind="acquisition",
        )
    )
    history["inventory_changes"][1].update(_numeric("principal_delta", "-4"))
    for weight in history["posting_weights"]:
        if weight["posting_id"] == "sale-stock":
            weight.update(_numeric("amount", "-4"))
        elif weight["posting_id"] == "sale-gain":
            weight.update(_numeric("amount", "-2"))
    for posting in history["postings"]:
        if posting["posting_id"] == "sale-gain":
            posting.update(_numeric("amount", "-2"))
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    sale = next(
        entry
        for entry in entries
        if isinstance(entry, data.Transaction) and entry.meta["jbt_txn_id"] == "sale"
    )
    stock = next(
        posting for posting in sale.postings if posting.account == "Assets:Broker:Stock"
    )
    assert isinstance(stock.cost, Cost)
    assert stock.cost.number == Decimal(4)
    assert stock.cost.label == "selected-lot"


def test_capitalized_fee_components_are_not_expensed_again(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    # Of nine carried dollars, six are principal and three are booked fees.
    history["inventory_changes"][0].update(_numeric("principal_delta", "6"))
    history["inventory_changes"][0].update(_numeric("capitalized_fee_delta", "3"))
    history["inventory_changes"][1].update(_numeric("principal_delta", "-2"))
    history["inventory_changes"][1].update(_numeric("capitalized_fee_delta", "-1"))
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    postings = [
        posting
        for entry in entries
        if isinstance(entry, data.Transaction)
        for posting in entry.postings
    ]
    assert [
        convert.get_weight(posting).number
        for posting in postings
        if posting.account == "Assets:Broker:Stock"
    ] == [Decimal(9), Decimal(-3)]
    assert sum(
        convert.get_weight(posting).number
        for posting in postings
        if posting.account == "Expenses:Fees"
    ) == Decimal(2)


@pytest.mark.parametrize(
    ("table", "field", "code"),
    [
        ("lots", "acquisition_date", "known_date_required"),
        ("inventory_changes", "principal_delta_coefficient", "known_number_required"),
    ],
)
def test_unknown_book_components_are_not_invented(
    history: MutableTables,
    policy: BeancountPolicy,
    table: str,
    field: str,
    code: str,
) -> None:
    history[table][0][field] = None
    with pytest.raises(BeancountProjectionError, match=code):
        render_beancount(history, policy=policy)


def test_date_policy_cannot_reorder_booked_history(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["transactions"][1]["date_settled"] = "2025-01-06"
    with pytest.raises(BeancountProjectionError, match="date_policy_reorders_book"):
        render_beancount(history, policy=replace(policy, date_role="settled"))


def test_corporate_action_is_explicitly_outside_scoped_projection(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["corporate_actions"] = [
        _row(action_id="unsupported", action_kind="spin_off"),
    ]
    with pytest.raises(BeancountProjectionError, match="unsupported_action_kind"):
        render_beancount(history, policy=policy)


def test_positive_price_and_generated_commodity_symbols_load(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["prices"] = [
        _row(
            price_id="close",
            date=date(2025, 1, 3),
            commodity_id="shares",
            quote_commodity_id="dollars",
            **_numeric("rate", "4.25"),
        ),
    ]
    entries, errors, _ = loader.load_string(
        render_beancount(history, policy=replace(policy, commodity_symbols={}))
    )
    assert errors == []
    price = next(entry for entry in entries if isinstance(entry, data.Price))
    assert price.amount.number == Decimal("4.25")
    assert price.currency != price.amount.currency
    assert price.meta["jbt_price_id"] == "close"


def test_exact_reduction_with_different_unit_cost_is_not_rebooked(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["inventory_changes"][1].update(_numeric("principal_delta", "-4"))
    for weight in history["posting_weights"]:
        if weight["posting_id"] == "sale-stock":
            weight.update(_numeric("amount", "-4"))
        elif weight["posting_id"] == "sale-gain":
            weight.update(_numeric("amount", "-2"))
    for posting in history["postings"]:
        if posting["posting_id"] == "sale-gain":
            posting.update(_numeric("amount", "-2"))
    with pytest.raises(BeancountProjectionError, match="lot_unit_cost_changed"):
        render_beancount(history, policy=policy)


def test_explicit_split_transfer_and_disposal_keep_selected_lot_lineage(
    history: MutableTables, policy: BeancountPolicy
) -> None:
    history["positions"].append(
        _row(
            position_id="destination",
            account_id="broker",
            measurement_kind="cost",
            inventory_method="individual",
        )
    )
    policy = replace(
        policy,
        position_accounts={
            **policy.position_accounts,
            "destination": "Assets:Other:Stock",
        },
        position_lifetimes={
            **policy.position_lifetimes,
            "destination": DirectiveLifetime("decl", date(2024, 1, 1)),
        },
    )
    for sequence, (txn_id, day) in enumerate(
        (("split", 4), ("transfer", 5), ("final-sale", 6)), start=3
    ):
        history["transactions"].append(
            _row(
                txn_id=txn_id,
                event_kind="corporate_action" if txn_id == "split" else "trade",
                event_state="recognized",
                date_traded=date(2025, 1, day),
                narration=txn_id,
                payee="Invented",
            )
        )
        history["book_steps"].append(
            _row(step_id=txn_id, txn_id=txn_id, sequence=sequence)
        )
    history["corporate_actions"] = [
        _row(
            action_id="split-action",
            action_kind="split",
            txn_id="split",
            effective_date=date(2025, 1, 4),
        )
    ]
    for txn_id, posting_id, target, units, weight in (
        ("split", "split-stock", "stock", "2", None),
        ("transfer", "transfer-out", "stock", "-2", None),
        ("transfer", "transfer-in", "destination", "2", None),
        ("final-sale", "final-stock", "destination", "-2", "-3"),
        ("final-sale", "final-cash", "cash", "5", "5"),
        ("final-sale", "final-gain", "gain", "-2", "-2"),
    ):
        is_position = target != "gain"
        history["postings"].append(
            _row(
                posting_id=posting_id,
                txn_id=txn_id,
                step_id=txn_id,
                position_id=target if is_position else None,
                category_id=None if is_position else target,
                leg_kind="position" if is_position else "boundary",
                posting_role="book_result" if not is_position else "principal",
                commodity_id="shares"
                if target in {"stock", "destination"}
                else "dollars",
                date_traded_mode="inherit",
                **_numeric("amount", units),
            )
        )
        if weight is not None:
            history["posting_weights"].append(
                _row(
                    posting_id=posting_id,
                    commodity_id="dollars",
                    **_numeric("amount", weight),
                )
            )
    for allocation_id, kind, posting_id, position, units, cost in (
        ("split", "transformation", "split-stock", "stock", "-2", "-6"),
        ("split", "transformation", "split-stock", "stock", "4", "6"),
        ("move", "transfer", "transfer-out", "stock", "-2", "-3"),
        ("move", "transfer", "transfer-in", "destination", "2", "3"),
        ("last", "reduction", "final-stock", "destination", "-2", "-3"),
    ):
        if not any(
            row["allocation_id"] == allocation_id
            for row in history["inventory_allocations"]
        ):
            history["inventory_allocations"].append(
                _row(allocation_id=allocation_id, allocation_kind=kind)
            )
        history["inventory_changes"].append(
            _row(
                change_id=f"{allocation_id}-{posting_id}-{units}",
                allocation_id=allocation_id,
                posting_id=posting_id,
                position_id=position,
                lot_id="selected-lot",
                pool_id=None,
                **_numeric("units_delta", units),
                **_numeric("principal_delta", cost),
                **_numeric("capitalized_fee_delta", "0"),
            )
        )
    entries, errors, _ = loader.load_string(render_beancount(history, policy=policy))
    assert errors == []
    actions = {
        entry.meta["jbt_txn_id"]: entry
        for entry in entries
        if isinstance(entry, data.Transaction)
        and entry.meta["jbt_txn_id"] in {"split", "transfer", "final-sale"}
    }
    assert actions["split"].meta["jbt_action_id"] == "split-action"
    assert [
        (_loaded_number(leg), _loaded_cost(leg).number, _loaded_cost(leg).label)
        for leg in actions["split"].postings
    ] == [
        (Decimal(-2), Decimal(3), "selected-lot"),
        (Decimal(4), Decimal("1.5"), "selected-lot"),
    ]
    assert {
        (leg.account, _loaded_number(leg), _loaded_cost(leg).number)
        for leg in actions["transfer"].postings
    } == {
        ("Assets:Broker:Stock", Decimal(-2), Decimal("1.5")),
        ("Assets:Other:Stock", Decimal(2), Decimal("1.5")),
    }
    assert sum(
        convert.get_weight(leg).number
        for leg in actions["final-sale"].postings
        if leg.account == "Assets:Other:Stock"
    ) == Decimal(-3)
    final_lot = next(
        leg
        for leg in actions["final-sale"].postings
        if leg.account == "Assets:Other:Stock"
    )
    assert _loaded_cost(final_lot).label == "selected-lot"
    assert _loaded_cost(final_lot).number == Decimal("1.5")
    assert sum(
        _loaded_number(leg)
        for leg in actions["final-sale"].postings
        if leg.account == "Income:Book-Result"
    ) == Decimal(-2)
