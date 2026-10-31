from dataclasses import replace
from datetime import date

import pytest

from jbt.artifacts.attempt import review_boundary, write_target
from jbt.domain.decisions import (
    AssignBody,
    EventReviewRef,
    EventTextAssignment,
    EventTextField,
    IdentityBody,
    IdentityMode,
    ReviewBasisRef,
    StatementRevisionBody,
    decision_kind,
)
from jbt.domain.identity import (
    AcquiredAnchor,
    Occurrence,
    SourceKeyAnchor,
    event_id,
    source_record_digest,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.journal import JournalEntry, JournalOperation
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind
from jbt.domain.review import compound_target
from jbt.domain.time import TimeRole
from jbt.model.corrections import Decision, targets
from jbt.model.corrections import fold as enrich_fold
from jbt.model.decision_merge import fold, native_targets
from jbt.model.quantity import (
    Binding,
    FindingCode,
    Inputs,
    Movement,
    Position,
    SourceLeg,
    Statement,
    StatementPart,
    Symbol,
    Validity,
    enrich,
    event_dates,
    merge,
)

ENTITY = EntityId("synthetic")
FEED = SemanticKey("feed")
SECTION = SemanticKey("checking")
ACCOUNT = SemanticKey("bank")
USD = SemanticKey("USD")
DECLARATION = RecordId(ENTITY, RecordKind.DECLARATION_REVISION, "a" * 64)
REVIEW = ReviewBasisRef("a" * 64, "b" * 64)


def movement(blob: str, key: str, amount: str = "-5") -> Movement:
    anchor = AcquiredAnchor(blob * 64, FEED, SECTION, SemanticKey("synthetic:1"))
    return Movement(
        source_record=RecordId(
            ENTITY, RecordKind.SOURCE_RECORD, source_record_digest(anchor)
        ),
        anchor=anchor,
        source_key=SourceKeyAnchor(
            FEED, SECTION, SemanticKey("fitid"), SemanticKey(key)
        ),
        event_key=SemanticKey("main"),
        when=date(2026, 1, 10),
        state=EventState.RECOGNIZED,
        payee=None,
        narration=None,
        legs=(
            SourceLeg(
                SemanticKey("principal"), "USD", ExactDecimal.parse(amount), None
            ),
        ),
        is_transfer=False,
        dates=(),
        times=(),
        transaction_type=None,
        movement_interval=None,
        effective_role=TimeRole.POSTED,
    )


def inputs(*movements: Movement) -> Inputs:
    return Inputs(
        ENTITY,
        date(2026, 2, 1),
        (Binding(FEED, SECTION, ACCOUNT, Validity(None, None), DECLARATION),),
        (),
        (Symbol("USD", USD, None, Validity(None, None), DECLARATION),),
        (
            Position(
                SemanticKey("cash"),
                ACCOUNT,
                USD,
                None,
                None,
                None,
                Validity(None, None),
                DECLARATION,
            ),
        ),
        (),
        tuple(movements),
        (),
        (),
        (),
    )


def identity(
    context: Inputs,
    canonical: Movement,
    *members: Movement,
    mode: IdentityMode = IdentityMode.KEY_FAMILY,
    key: str = "identity",
    sequence: int = 1,
) -> Decision:
    records = tuple(
        sorted(
            {canonical.source_record, *(member.source_record for member in members)},
            key=lambda value: value.value,
        )
    )
    body = IdentityBody(ENTITY, REVIEW, mode, canonical.source_record, records)
    selected = tuple(
        target
        for target in native_targets(context)
        if target.reference.table.value == "review_sources"
        and target.reference.key[0] in {record.value for record in records}
    )
    entry = JournalEntry(
        sequence,
        "d" * 64 if sequence > 1 else None,
        ENTITY,
        SemanticKey(key),
        decision_kind(body),
        JournalOperation.RECORD,
        None,
        REVIEW.attempt_digest,
        "Reviewed synthetic correspondence",
        "c" * 64,
    )
    return Decision(entry, body, compound_target(selected))


def test_family_merge_persists_without_reviving_retired_id() -> None:
    first, second = movement("1", "A"), movement("2", "B")
    context = inputs(first, second)
    decision = identity(context, first, second)
    result = fold(context, (decision,))
    retired = event_id(
        ENTITY, Occurrence(second.source_key or second.anchor, second.event_key)
    )
    canonical = event_id(
        ENTITY, Occurrence(first.source_key or first.anchor, first.event_key)
    )
    assert result.resolution.retired == (retired,)
    assert [
        (event.identity, event.state) for event in result.resolution.events
    ] == sorted(
        ((canonical, EventState.RECOGNIZED), (retired, EventState.RETRACTED)),
        key=lambda item: item[0].value,
    )
    assert all(
        event_dates(event) == (None, None, event.when, None, None)
        for event in result.resolution.events
    )
    later = movement("3", "B", "-5.00")
    repeated = fold(
        replace(context, movements=(*context.movements, later)), (decision,)
    )
    assert repeated.receipts[0].evaluation.outcome.value == "applied"
    live = next(
        event
        for event in repeated.resolution.events
        if event.state == EventState.RECOGNIZED
    )
    assert live.identity == canonical
    assert len(live.support) == 3
    assert repeated.resolution.retired == (retired,)
    assert len(repeated.receipts[0].restore) <= 3
    separate = identity(
        repeated.inputs,
        later,
        mode=IdentityMode.OCCURRENCE,
        key="separate",
        sequence=2,
    )
    split = fold(
        replace(context, movements=(*context.movements, later)),
        (decision, separate),
    )
    acquired = event_id(ENTITY, Occurrence(later.anchor, later.event_key))
    assert split.resolution.retired == (retired,)
    assert {
        event.identity
        for event in split.resolution.events
        if event.state == EventState.RECOGNIZED
    } == {canonical, acquired}
    assert (
        next(
            event for event in split.resolution.events if event.identity == retired
        ).identity_decision
        == decision.entry.revision_id
    )


def test_enrichment_correction_does_not_follow_merge_retirement_or_later_split() -> (
    None
):
    canonical, duplicate = movement("1", "A"), movement("2", "B")
    context = inputs(canonical, duplicate)
    before = merge(context)
    original = next(
        target
        for target in targets(before)
        if target.reference
        == EventReviewRef(
            event_id(
                ENTITY,
                Occurrence(
                    duplicate.source_key or duplicate.anchor, duplicate.event_key
                ),
            )
        ).guard_reference
    )
    body = AssignBody(
        ENTITY,
        REVIEW,
        EventReviewRef(
            event_id(
                ENTITY,
                Occurrence(
                    duplicate.source_key or duplicate.anchor, duplicate.event_key
                ),
            )
        ),
        (EventTextAssignment(EventTextField.PAYEE, "Reviewed duplicate"),),
    )
    entry = JournalEntry(
        1,
        None,
        ENTITY,
        SemanticKey("payee"),
        decision_kind(body),
        JournalOperation.RECORD,
        None,
        REVIEW.attempt_digest,
        "Reviewed the duplicate before correspondence",
        "c" * 64,
    )
    correction = Decision(entry, body, original)
    correspondence = identity(context, canonical, duplicate, sequence=2)
    merged = fold(context, (correspondence,))
    separated = identity(
        merged.inputs,
        duplicate,
        mode=IdentityMode.OCCURRENCE,
        key="separate",
        sequence=3,
    )
    for decisions in ((correspondence,), (correspondence, separated)):
        result = fold(context, decisions)
        corrected = enrich_fold(
            result.inputs,
            enrich(result.inputs, result.resolution),
            (correction,),
        )
        assert corrected.receipts[0].evaluation.outcome.value == "inactive"
        assert corrected.receipts[0].candidates[0].reference == original.reference
        assert any(finding.blocking for finding in corrected.resolution.findings)
        assert all(
            event.payee != "Reviewed duplicate" for event in corrected.resolution.events
        )


def test_occurrence_split_invalidates_dependent_membership() -> None:
    first, reused, other = (
        movement("1", "A"),
        movement("2", "A", "-6"),
        movement("3", "B", "-6"),
    )
    context = inputs(first, reused, other)
    separate = identity(context, reused, mode=IdentityMode.OCCURRENCE, key="separate")
    separated = fold(context, (separate,))
    assert not any(finding.blocking for finding in separated.resolution.findings)
    new_id = event_id(ENTITY, Occurrence(reused.anchor, reused.event_key))
    assert new_id in {event.identity for event in separated.resolution.events}
    dependent = identity(separated.inputs, other, reused, key="dependent", sequence=2)
    complete = fold(context, (separate, dependent))
    assert [receipt.evaluation.outcome.value for receipt in complete.receipts] == [
        "applied",
        "applied",
    ]
    without_split = fold(context, (dependent,))
    assert without_split.receipts[0].evaluation.outcome.value == "changed"
    assert any(finding.blocking for finding in without_split.resolution.findings)
    receipt = complete.receipts[0]
    review = {
        "tail": [write_target(target) for target in complete.tail],
        "receipts": [
            {
                "decision_key": item.entry.key.value,
                "expected": write_target(item.expected),
                "candidates": [
                    write_target(candidate) for candidate in item.candidates
                ],
                "restore": [
                    {
                        "entity_id": reference.entity.value,
                        "table": reference.table.value,
                        "key": list(reference.key),
                        "candidates": [write_target(value) for value in values],
                    }
                    for reference, values in item.restore
                ],
            }
            for item in complete.receipts
        ],
    }
    before = review_boundary(review, receipt.entry.key.value)
    assert (
        compound_target(
            tuple(
                target
                for target in before
                if target.reference
                in {candidate.reference for candidate in native_targets(context)}
                and target.reference.key == (reused.source_record.value,)
            )
        )
        == separate.expected
    )


@pytest.mark.parametrize(
    "scope", ["single", "collective", "account", "basis", "date_role"]
)
def test_statement_revision_enforces_scope_before_retracting_covered_omissions(
    scope: str,
) -> None:
    omitted = movement("1", "omitted")
    replacement = movement("2", "retained", "-6")
    reversal = replace(movement("3", "reversal", "5"), when=date(2026, 1, 25))
    context = inputs(omitted, replacement, reversal)
    period = Validity(date(2026, 1, 1), date(2026, 1, 20))
    statements = tuple(
        Statement(
            SemanticKey(blob * 64),
            FEED,
            SECTION,
            blob * 64,
            ACCOUNT,
            (
                StatementPart(
                    SemanticKey("posted"),
                    FactKind.MOVEMENT,
                    SemanticKey("posted"),
                    TimeRole.POSTED,
                    period,
                    complete=True,
                    source_records=(member.source_record,),
                ),
            ),
        )
        for blob, member in (("1", omitted), ("2", replacement))
    )
    movements = (
        replace(omitted, statement=statements[0].identity),
        replace(replacement, statement=statements[1].identity),
        reversal,
    )
    if scope == "collective":
        boundary = date(2026, 1, 15)
        later = replace(
            movement("4", "retained-later", "-8"),
            when=boundary,
            statement=SemanticKey("4" * 64),
        )
        first = replace(
            statements[1].parts[0],
            validity=Validity(period.start, boundary),
        )
        last = replace(
            first,
            validity=Validity(boundary, period.end),
            source_records=(later.source_record,),
        )
        statements = (
            statements[0],
            replace(statements[1], parts=(first,)),
            replace(
                statements[1],
                identity=later.statement,
                blob_digest="4" * 64,
                parts=(last,),
            ),
        )
        movements = (*movements, later)
    context = replace(context, statements=statements, movements=movements)
    body = StatementRevisionBody(
        ENTITY,
        REVIEW,
        ("1" * 64,),
        tuple(sorted(statement.identity.value for statement in statements[1:])),
        ACCOUNT,
        FactKind.MOVEMENT,
        SemanticKey("posted"),
        date(2026, 1, 1),
        date(2026, 1, 20),
    )
    if scope == "account":
        body = replace(body, account=SemanticKey("other-bank"))
    elif scope == "basis":
        body = replace(body, basis=SemanticKey("other-basis"))
    elif scope == "date_role":
        replacement_statement = context.statements[1]
        context = replace(
            context,
            statements=(
                context.statements[0],
                replace(
                    replacement_statement,
                    parts=(
                        replace(
                            replacement_statement.parts[0], date_role=TimeRole.VALUE
                        ),
                    ),
                ),
            ),
        )
    selected = tuple(
        target
        for target in native_targets(context)
        if target.reference.table.value == "review_statements"
    )
    entry = JournalEntry(
        1,
        None,
        ENTITY,
        SemanticKey("reissue"),
        decision_kind(body),
        JournalOperation.RECORD,
        None,
        REVIEW.attempt_digest,
        "Reviewed complete synthetic reissue",
        "c" * 64,
    )
    result = fold(context, (Decision(entry, body, compound_target(selected)),))
    assert result.receipts[0].evaluation.outcome.value == "applied"
    if scope not in {"single", "collective"}:
        assert result.inputs == context
        assert any(
            finding.code == FindingCode.DECISION_INVALID
            and finding.scope == entry.revision_id
            and finding.blocking
            for finding in result.resolution.findings
        )
        return
    assert not any(finding.blocking for finding in result.resolution.findings)
    by_id = {event.identity: event for event in result.resolution.events}
    omitted_id = event_id(
        ENTITY, Occurrence(omitted.source_key or omitted.anchor, omitted.event_key)
    )
    reversal_id = event_id(
        ENTITY, Occurrence(reversal.source_key or reversal.anchor, reversal.event_key)
    )
    assert by_id[omitted_id].state == EventState.RETRACTED
    assert by_id[reversal_id].state == EventState.RECOGNIZED
    assert sorted(
        event.legs[0].amount.coefficient
        for event in by_id.values()
        if event.state == EventState.RECOGNIZED
    ) == (["-6", "-8", "5"] if scope == "collective" else ["-6", "5"])
