from dataclasses import replace
from datetime import date

import pytest

from jbt.domain.cash import (
    CashError,
    CashObservation,
    CashStatement,
    CategoryRule,
    CheckStatus,
    MatchField,
    Opening,
    Predicate,
    model_checks,
    resolve_cash,
    source_checks,
    total,
)
from jbt.domain.identity import AcquiredAnchor
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.numbers import ExactDecimal


@pytest.fixture
def opening() -> Opening:
    return Opening(
        "initial-cash",
        "opening-declaration",
        "opening",
        date(2025, 12, 31),
        ExactDecimal.parse("100.00"),
        "equity",
        "prior-balance",
        date(2026, 1, 1),
        ExactDecimal.parse("100.00"),
    )


@pytest.fixture
def statement() -> CashStatement:
    movements = tuple(
        CashObservation(
            AcquiredAnchor(
                "a" * 64,
                SemanticKey("bank"),
                SemanticKey("account"),
                SemanticKey(f"ofx:{identifier}"),
            ),
            identifier,
            date(2026, 1, day),
            ExactDecimal.parse(amount),
            kind,
            "USD",
            name,
            None,
        )
        for identifier, day, amount, kind, name in (
            ("credit", 12, "20.00", "CREDIT", "Invented income"),
            ("debit", 20, "-5.00", "DEBIT", "Invented expense"),
        )
    )
    return CashStatement(
        "january",
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 2, 1),
        ExactDecimal.parse("115.00"),
        complete=True,
        movements=movements,
    )


@pytest.fixture
def rules() -> tuple[CategoryRule, ...]:
    return (
        CategoryRule(
            "credit-rule", (Predicate(MatchField.NAME, "Invented income"),), "income"
        ),
        CategoryRule(
            "debit-rule", (Predicate(MatchField.NAME, "Invented expense"),), "expense"
        ),
    )


def test_source_and_model_reconcile_independently(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
) -> None:
    events = resolve_cash(EntityId("owner"), opening, (statement,), rules)
    assert total(tuple(e.amount for e in events)) == ExactDecimal("115", 0, None)
    assert all(
        c.status == CheckStatus.PASS for c in source_checks(opening, (statement,))
    )
    checks = model_checks(
        opening,
        (statement,),
        events,
        expected_periods=((statement.start, date(2026, 1, 31)),),
        as_of=statement.end,
    )
    assert all(c.status == CheckStatus.PASS for c in checks)
    dropped = tuple(e for e in events if e.amount.coefficient != "-5")
    checks = model_checks(
        opening,
        (statement,),
        dropped,
        expected_periods=((statement.start, date(2026, 1, 31)),),
        as_of=statement.end,
    )
    assert checks[1].observed == ExactDecimal("120", 0, None)
    assert checks[1].status == CheckStatus.FAIL
    assert checks[2].status == CheckStatus.FAIL
    assert source_checks(opening, (statement,))[0].status == CheckStatus.PASS


def test_compensating_omissions_fail_occurrence_check(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
) -> None:
    credit, debit = statement.movements
    balanced = replace(
        statement,
        balance=opening.amount,
        movements=(replace(credit, amount=ExactDecimal.parse("5.00")), debit),
    )
    events = resolve_cash(EntityId("owner"), opening, (balanced,), rules)
    checks = model_checks(
        opening,
        (balanced,),
        events[:1],
        expected_periods=((balanced.start, balanced.end),),
        as_of=balanced.end,
    )
    assert checks[1].status == CheckStatus.PASS
    assert checks[2].status == CheckStatus.FAIL


def test_reconciliation_does_not_claim_complete_coverage(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
) -> None:
    point = replace(statement, complete=False)
    assert source_checks(opening, (point,))[0].status == CheckStatus.PASS
    events = resolve_cash(EntityId("owner"), opening, (point,), rules)
    checks = model_checks(
        opening,
        (point,),
        events,
        expected_periods=((point.start, date(2026, 1, 31)),),
        as_of=date(2026, 1, 31),
    )
    assert checks[-1].status == CheckStatus.NOT_EVALUABLE


def test_category_requires_explicit_matching_policy(
    opening: Opening,
    statement: CashStatement,
) -> None:
    with pytest.raises(CashError, match="external_treatment_required"):
        resolve_cash(EntityId("owner"), opening, (statement,), ())


def test_overlapping_sources_are_not_merged(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
) -> None:
    with pytest.raises(CashError, match="overlapping_sources_unsupported"):
        resolve_cash(EntityId("owner"), opening, (statement, statement), rules)


def test_changed_authored_opening_is_not_repaired(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
) -> None:
    changed = replace(opening, amount=ExactDecimal.parse("101.00"))
    events = resolve_cash(EntityId("owner"), changed, (statement,), rules)
    checks = model_checks(
        changed,
        (statement,),
        events,
        expected_periods=((statement.start, statement.end),),
        as_of=statement.end,
    )
    assert checks[0].status == checks[1].status == CheckStatus.FAIL
    assert source_checks(changed, (statement,))[0].status == CheckStatus.PASS


@pytest.mark.parametrize("posted", [date(2026, 1, 1), date(2026, 2, 1)])
def test_boundary_day_movements_cannot_guess_point_order(
    opening: Opening,
    statement: CashStatement,
    rules: tuple[CategoryRule, ...],
    posted: date,
) -> None:
    ambiguous = replace(
        statement,
        movements=(
            replace(statement.movements[0], posted=posted),
            statement.movements[1],
        ),
    )
    assert source_checks(opening, (ambiguous,))[0].status == CheckStatus.NOT_EVALUABLE
    with pytest.raises(CashError, match="movement_outside_interval"):
        resolve_cash(EntityId("owner"), opening, (ambiguous,), rules)


def test_source_gap_does_not_invent_continuity(
    opening: Opening,
    statement: CashStatement,
) -> None:
    gap = replace(statement, start=date(2026, 1, 2))
    assert source_checks(opening, (gap,))[0].status == CheckStatus.NOT_EVALUABLE


def test_each_source_check_names_its_actual_boundary_evidence(
    opening: Opening,
    statement: CashStatement,
) -> None:
    following = replace(
        statement,
        source_id="february",
        start=date(2026, 2, 1),
        end=date(2026, 3, 1),
        balance_date=date(2026, 3, 1),
        movements=(),
    )
    checks = source_checks(opening, (statement, following))
    assert checks[1].status == CheckStatus.PASS
    assert checks[1].evidence_ids == ("january", "february")
