from dataclasses import replace
from datetime import date

from jbt.domain.decisions import (
    AssignBody,
    EventReviewAssignment,
    EventReviewRef,
    EventTagsAssignment,
    EventTextAssignment,
    EventTextField,
    LegCategoryAssignment,
    LegReviewRef,
    ReviewBasisRef,
)
from jbt.domain.identity import AuthoredAnchor, Occurrence, event_id
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.journal import DecisionKind, JournalEntry, JournalOperation
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind, ReviewState
from jbt.domain.rules import MatchType, Operator, Predicate, RuleField
from jbt.domain.time import TimeRole
from jbt.model.corrections import Decision, fold, targets
from jbt.model.quantity import (
    Event,
    EventKind,
    FindingCode,
    Inputs,
    Leg,
    Validity,
    resolve,
)
from jbt.model.rules import AssignmentOutcome, Rule, RuleAssignment

ENTITY = EntityId("synthetic-owner")
PAYEE = " Cafe\u0301 "


def _event() -> Event:
    occurrence = Occurrence(
        AuthoredAnchor(SemanticKey("purchase")), SemanticKey("main")
    )
    return Event(
        event_id(ENTITY, occurrence),
        occurrence,
        date(2026, 1, 20),
        EventKind.TRANSACTION,
        EventState.RECOGNIZED,
        ReviewState.UNREVIEWED,
        PAYEE,
        " exact memo ",
        (
            Leg(
                SemanticKey("cash"),
                LegKind.POSITION,
                SemanticKey("USD"),
                ExactDecimal.parse("-5.00"),
                SemanticKey("bank"),
                SemanticKey("cash"),
                None,
                None,
                None,
            ),
            Leg(
                SemanticKey("boundary"),
                LegKind.BOUNDARY,
                SemanticKey("USD"),
                ExactDecimal.parse("5"),
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        (),
        None,
        effective_role=TimeRole.POSTED,
        description=" source description ",
    )


def _rule(
    key: str,
    order: int,
    assignments: tuple[RuleAssignment, ...],
    predicates: tuple[Predicate, ...] | None = None,
) -> Rule:
    return Rule(
        SemanticKey(key),
        RecordId(ENTITY, RecordKind.DECLARATION_REVISION, f"{order + 1:064x}"),
        order,
        Validity(None, None),
        predicates
        if predicates is not None
        else (
            Predicate(
                SemanticKey("transactions.payee"),
                MatchType.TEXT,
                Operator.EQUALS,
                (PAYEE,),
            ),
        ),
        assignments,
    )


def _inputs(event: Event, rules: tuple[Rule, ...]) -> Inputs:
    return Inputs(
        ENTITY,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        rules,
        (),
        (event,),
        (),
        (),
        categories=(SemanticKey("expense"), SemanticKey("income")),
    )


def _policies() -> tuple[Rule, ...]:
    return (
        _rule(
            "first",
            0,
            (
                EventTextAssignment(EventTextField.PAYEE, "Enriched"),
                EventTagsAssignment(("a",)),
                LegCategoryAssignment(SemanticKey("expense")),
            ),
        ),
        _rule(
            "second",
            1,
            (
                EventTextAssignment(EventTextField.PAYEE, "Loses"),
                EventTextAssignment(EventTextField.NARRATION, None),
                EventReviewAssignment(ReviewState.REVIEWED),
                EventTagsAssignment(("a", "b")),
                LegCategoryAssignment(SemanticKey("income")),
            ),
        ),
        _rule(
            "enriched-is-not-source",
            2,
            (EventTagsAssignment(("wrong",)),),
            (
                Predicate(
                    SemanticKey("transactions.payee"),
                    MatchType.TEXT,
                    Operator.EQUALS,
                    ("Enriched",),
                ),
            ),
        ),
        _rule(
            "redundant",
            3,
            (
                EventTextAssignment(EventTextField.PAYEE, "Enriched"),
                EventTagsAssignment(("a",)),
                LegCategoryAssignment(SemanticKey("expense")),
            ),
        ),
    )


def test_complete_fold_first_scalars_union_tags_and_pre_enrichment_view() -> None:
    inputs = _inputs(_event(), _policies())
    resolution = resolve(inputs)
    (event,) = resolution.events
    assert (event.payee, event.narration, event.review, event.tags) == (
        "Enriched",
        None,
        ReviewState.REVIEWED,
        ("a", "b"),
    )
    assert event.legs[1].category == SemanticKey("expense")
    policies = dict(event.rules)
    assert policies[RuleField.PAYEE] == (inputs.rules[0].declaration,)
    assert policies[RuleField.TAGS] == (
        inputs.rules[0].declaration,
        inputs.rules[1].declaration,
    )
    assert [(result.rule.key.value, result.matched) for result in resolution.rules] == [
        ("first", True),
        ("second", True),
        ("enriched-is-not-source", False),
        ("redundant", True),
    ]
    assert [result.outcome for result in resolution.rules[1].assignments] == [
        AssignmentOutcome.SHADOWED,
        AssignmentOutcome.APPLIED,
        AssignmentOutcome.APPLIED,
        AssignmentOutcome.APPLIED,
        AssignmentOutcome.SHADOWED,
    ]
    assert all(
        result.observed == PAYEE
        for evaluation in resolution.rules
        for result in evaluation.predicates
    )
    assert {
        (finding.code, finding.scope, finding.severity)
        for finding in resolution.findings
    } == {
        (FindingCode.RULE_UNUSED, inputs.rules[2].declaration, "warning"),
        (FindingCode.RULE_SHADOWED, inputs.rules[3].declaration, "warning"),
        (FindingCode.RULE_REDUNDANT, inputs.rules[3].declaration, "info"),
    }
    assert inputs.authored[0] == _event()


def test_rules_remain_declared_and_become_unused_when_source_text_changes() -> None:
    rule = _policies()[0]
    inputs = _inputs(replace(_event(), payee=" Café "), (rule,))
    result = resolve(inputs)
    assert inputs.rules == (rule,)
    assert result.events[0].legs[1].category is None
    assert result.rules[0].predicates[0].observed == " Café "
    assert not result.rules[0].matched
    assert any(
        finding.code == FindingCode.RULE_UNUSED and finding.scope == rule.declaration
        for finding in result.findings
    )


def test_nullable_first_assignment_is_not_a_missing_winner() -> None:
    rules = (
        _rule(
            "explicit-null",
            0,
            (
                LegCategoryAssignment(None),
                EventTextAssignment(EventTextField.PAYEE, PAYEE),
            ),
        ),
        _rule("later-category", 1, (LegCategoryAssignment(SemanticKey("expense")),)),
    )
    resolution = resolve(_inputs(_event(), rules))
    assert resolution.events[0].legs[1].category is None
    assert resolution.events[0].legs[1].rules == (rules[0].declaration,)
    assert dict(resolution.events[0].rules)[RuleField.PAYEE] == (rules[0].declaration,)
    assert all(
        result.outcome == AssignmentOutcome.REDUNDANT and result.selected
        for result in resolution.rules[0].assignments
    )
    assert resolution.rules[1].assignments[0].outcome == AssignmentOutcome.SHADOWED
    assert not resolution.rules[1].assignments[0].selected
    assert any(
        finding.code == FindingCode.RULE_REDUNDANT
        and finding.scope == rules[0].declaration
        for finding in resolution.findings
    )
    assert not any(
        finding.code == FindingCode.RULE_SHADOWED
        and finding.scope == rules[0].declaration
        for finding in resolution.findings
    )
    assert any(
        finding.code == FindingCode.UNCLASSIFIED for finding in resolution.findings
    )


def test_event_assignments_follow_rule_order_not_balancing_leg_order() -> None:
    original = _event()
    event = replace(
        original,
        legs=(
            replace(original.legs[0], amount=ExactDecimal.parse("-12")),
            original.legs[1],
            replace(
                original.legs[1],
                key=SemanticKey("second-boundary"),
                amount=ExactDecimal.parse("7"),
            ),
        ),
    )
    rules = tuple(
        _rule(
            f"amount-{amount}",
            index,
            (EventTextAssignment(EventTextField.PAYEE, str(amount)),),
            (
                Predicate(
                    SemanticKey("postings.amount"),
                    MatchType.DECIMAL,
                    Operator.EQUALS,
                    (ExactDecimal.parse(str(amount)),),
                ),
            ),
        )
        for index, amount in enumerate((7, 5))
    )
    result = resolve(_inputs(event, rules))
    assert result.events[0].payee == "7"
    assert [
        evaluation.assignments[0].outcome
        for evaluation in result.rules
        if evaluation.matched
    ] == [
        AssignmentOutcome.APPLIED,
        AssignmentOutcome.SHADOWED,
    ]


def test_corrections_follow_rule_only_values_without_recomputing_explanation() -> None:
    inputs = _inputs(_event(), _policies())
    resolution = resolve(inputs)
    target = next(
        target
        for target in targets(resolution)
        if target.reference.table.value == "review_events"
    )
    entry = JournalEntry(
        1,
        None,
        ENTITY,
        SemanticKey("reviewed"),
        DecisionKind.ASSIGN,
        JournalOperation.RECORD,
        None,
        "a" * 64,
        "Synthetic review",
        "b" * 64,
    )
    body = AssignBody(
        ENTITY,
        ReviewBasisRef("a" * 64, "c" * 64),
        EventReviewRef(resolution.events[0].identity),
        (
            EventTextAssignment(EventTextField.PAYEE, "Enriched"),
            EventTagsAssignment(("a", "b")),
        ),
    )
    redundant = fold(inputs, resolution, (Decision(entry, body, target),))
    assert any(
        finding.code == FindingCode.REDUNDANT and finding.severity == "info"
        for finding in redundant.resolution.findings
    )
    changed = fold(
        inputs,
        resolution,
        (
            Decision(
                entry,
                replace(
                    body,
                    assignments=(
                        EventTextAssignment(EventTextField.PAYEE, "Reviewed"),
                        EventTagsAssignment(("review-only",)),
                    ),
                ),
                target,
            ),
        ),
    )
    assert changed.resolution.events[0].payee == "Reviewed"
    assert changed.resolution.events[0].tags == ("review-only",)
    assert changed.resolution.rules == resolution.rules
    assert not any(
        finding.code == FindingCode.REDUNDANT for finding in changed.resolution.findings
    )
    revised_inputs = _inputs(replace(_event(), payee="No longer matches"), _policies())
    revised_rules = resolve(revised_inputs)
    assert all(not evaluation.matched for evaluation in revised_rules.rules)
    retained = fold(revised_inputs, revised_rules, (Decision(entry, body, target),))
    assert retained.receipts[0].evaluation.outcome.value == "applied"
    assert retained.resolution.events[0].payee == "Enriched"
    assert retained.resolution.events[0].tags == ("a", "b")
    assert retained.resolution.rules == revised_rules.rules
    category_target = targets(resolution)[2]
    category = Decision(
        replace(entry, key=SemanticKey("category")),
        AssignBody(
            ENTITY,
            body.review,
            LegReviewRef(
                RecordId(ENTITY, RecordKind.LEG, str(category_target.reference.key[0]))
            ),
            (LegCategoryAssignment(SemanticKey("expense")),),
        ),
        category_target,
    )
    assert any(
        finding.code == FindingCode.REDUNDANT
        for finding in fold(inputs, resolution, (category,)).resolution.findings
    )
    retained_category = fold(revised_inputs, revised_rules, (category,))
    assert retained_category.receipts[0].evaluation.outcome.value == "applied"
    assert retained_category.resolution.events[0].legs[1].category == SemanticKey(
        "expense"
    )
    assert retained_category.resolution.rules == revised_rules.rules
