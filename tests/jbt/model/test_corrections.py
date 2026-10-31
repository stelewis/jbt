from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from tests.integration.conformance.attempt_reader import (
    _boundary as independent_boundary,
)
from tests.integration.conformance.reader import ReaderError

from jbt.artifacts.attempt import review_boundary, write_target
from jbt.contracts.primitives import ContractError
from jbt.domain.decisions import (
    AssignBody,
    Assignment,
    AssignmentDateRole,
    EventDateAssignment,
    EventReviewRef,
    EventStateAssignment,
    LegCategoryAssignment,
    LegReviewRef,
    ReviewBasisRef,
    SplitBody,
    SplitChild,
)
from jbt.domain.errors import GuardError
from jbt.domain.guards import GuardReference, GuardTarget, projection_digest
from jbt.domain.identity import AuthoredAnchor, Occurrence, event_id
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalOperation,
    entry_digest,
)
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, LegKind, ReviewState
from jbt.domain.time import TimeRole
from jbt.model import corrections
from jbt.model.corrections import Decision, fold, profile, targets
from jbt.model.quantity import (
    Determination,
    Event,
    EventKind,
    FindingCode,
    Inputs,
    Leg,
    Resolution,
    ResolutionStatus,
    event_dates,
)


def _event() -> Event:
    entity = EntityId("invented-owner")
    occurrence = Occurrence(
        AuthoredAnchor(SemanticKey("purchase")), SemanticKey("main")
    )
    return Event(
        event_id(entity, occurrence),
        occurrence,
        date(2026, 1, 20),
        EventKind.TRANSACTION,
        EventState.RECOGNIZED,
        ReviewState.UNREVIEWED,
        "Invented expense",
        None,
        (
            Leg(
                SemanticKey("position"),
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
                ExactDecimal.parse("5.00"),
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
    )


def _targets(event: Event) -> tuple:
    return targets(
        Resolution(
            (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
        )
    )


def test_owned_profiles_ignore_descriptive_rule_and_lexical_changes() -> None:
    original = _event()
    revised = replace(
        original,
        payee="Different match text",
        narration="Corrected description",
        review=ReviewState.REVIEWED,
        tags=(" exact ",),
        legs=tuple(
            replace(
                leg,
                category=SemanticKey("expense")
                if leg.kind == LegKind.BOUNDARY
                else None,
                amount=ExactDecimal.parse(
                    "-5" if leg.kind == LegKind.POSITION else "5"
                ),
            )
            for leg in original.legs
        ),
    )
    expected, observed = _targets(original), _targets(revised)
    assert len(expected) == len(observed)
    for left, right in zip(expected, observed, strict=True):
        assert left.reference == right.reference
        assert projection_digest(left, profile(left)) == projection_digest(
            right, profile(right)
        )


@pytest.mark.parametrize("change", ["quantity", "original_currency"])
def test_changed_economics_keep_addresses_and_invalidate_guards(change: str) -> None:
    event = _event()
    original = replace(
        event,
        legs=tuple(
            replace(leg, original_commodity=SemanticKey("EUR")) for leg in event.legs
        ),
    )
    revised = replace(
        original,
        legs=tuple(
            replace(
                leg,
                amount=ExactDecimal.parse("-7" if leg.kind == LegKind.POSITION else "7")
                if change == "quantity"
                else leg.amount,
                original_commodity=SemanticKey("GBP")
                if change == "original_currency"
                else leg.original_commodity,
            )
            for leg in original.legs
        ),
    )
    for left, right in zip(_targets(original), _targets(revised), strict=True):
        assert left.reference == right.reference
        assert projection_digest(left, profile(left)) != projection_digest(
            right, profile(right)
        )


def test_caller_cannot_choose_a_weaker_owned_profile() -> None:
    target = _targets(_event())[1]
    weaker = replace(
        target,
        fields=tuple(field for field in target.fields if field.name != "accounts"),
    )
    with pytest.raises(GuardError, match="native_guard_profile_required"):
        profile(weaker)


@pytest.mark.parametrize(
    ("target_index", "categories", "cause"),
    [
        (1, (SemanticKey("expense"),), FindingCode.CATEGORY_TARGET),
        (2, (), FindingCode.CATEGORY_UNKNOWN),
    ],
)
def test_invalid_category_assignments_keep_their_specific_cause(
    target_index: int,
    categories: tuple[SemanticKey, ...],
    cause: FindingCode,
) -> None:
    event = _event()
    target = _targets(event)[target_index]
    entity = event.identity.entity
    body = AssignBody(
        entity,
        ReviewBasisRef("a" * 64, "b" * 64),
        LegReviewRef(RecordId(entity, RecordKind.LEG, str(target.reference.key[0]))),
        (LegCategoryAssignment(SemanticKey("expense")),),
    )
    entry = JournalEntry(
        1,
        None,
        entity,
        SemanticKey("category"),
        DecisionKind.ASSIGN,
        JournalOperation.RECORD,
        None,
        "a" * 64,
        "classify",
        "c" * 64,
    )
    inputs = Inputs(
        entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (event,),
        (),
        (),
        categories=categories,
    )
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    result = fold(inputs, resolution, (Decision(entry, body, target),))
    assert any(
        finding.code == cause and finding.blocking
        for finding in result.resolution.findings
    )
    assert not any(
        finding.code == FindingCode.DECISION for finding in result.resolution.findings
    )
    assert result.resolution.determinations[0].status == ResolutionStatus.BLOCKED


def test_split_preserves_currency_and_frames_keys_without_collisions() -> None:
    original = _event()
    event = replace(
        original,
        legs=tuple(
            replace(leg, original_commodity=SemanticKey("EUR")) for leg in original.legs
        ),
    )
    expected = _targets(event)[2]
    entity = event.identity.entity
    reference = LegReviewRef(
        RecordId(entity, RecordKind.LEG, str(expected.reference.key[0]))
    )
    inputs = Inputs(entity, date(2026, 2, 1), (), (), (), (), (), (), (event,), (), ())
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    keys = []
    for decision_key, child_key in (("A:B", "C"), ("A", "B:C")):
        body = SplitBody(
            entity,
            ReviewBasisRef("a" * 64, "b" * 64),
            reference,
            (
                SplitChild(SemanticKey(child_key), ExactDecimal.parse("2")),
                SplitChild(SemanticKey("rest"), ExactDecimal.parse("3")),
            ),
        )
        entry = JournalEntry(
            1,
            None,
            entity,
            SemanticKey(decision_key),
            DecisionKind.SPLIT,
            JournalOperation.RECORD,
            None,
            "a" * 64,
            "allocate",
            "c" * 64,
        )
        result = fold(inputs, resolution, (Decision(entry, body, expected),))
        assert result.receipts[0].evaluation.outcome.value == "applied"
        assert sorted(
            leg.amount.coefficient for leg in result.resolution.events[0].legs
        ) == ["-5", "2", "3"]
        assert all(
            leg.original_commodity == SemanticKey("EUR")
            for leg in result.resolution.events[0].legs
        )
        keys.append(
            next(
                leg.key
                for leg in result.resolution.events[0].legs
                if leg.amount.coefficient == "2"
            )
        )
    assert keys[0] != keys[1]


def _named_event(key: str) -> Event:
    event = _event()
    occurrence = Occurrence(AuthoredAnchor(SemanticKey(key)), SemanticKey("main"))
    return replace(
        event,
        identity=event_id(event.identity.entity, occurrence),
        occurrence=occurrence,
    )


def _assignment(
    expected: GuardTarget,
    key: str,
    sequence: int,
    assignment: Assignment,
    *,
    prior: JournalEntry | None = None,
) -> Decision:
    entity = expected.reference.entity
    kind = (
        RecordKind.EVENT
        if expected.reference.table.value == "review_events"
        else RecordKind.LEG
    )
    identifier = RecordId(entity, kind, str(expected.reference.key[0]))
    body = AssignBody(
        entity,
        ReviewBasisRef("a" * 64, "b" * 64),
        EventReviewRef(identifier)
        if kind == RecordKind.EVENT
        else LegReviewRef(identifier),
        (assignment,),
    )
    entry = JournalEntry(
        sequence,
        "d" * 64 if sequence > 1 else None,
        entity,
        SemanticKey(key),
        DecisionKind.ASSIGN,
        JournalOperation.SUPERSEDE if prior else JournalOperation.RECORD,
        entry_digest(prior) if prior else None,
        "a" * 64,
        "review synthetic purchase",
        "c" * 64,
    )
    return Decision(entry, body, expected)


@pytest.mark.parametrize("state", [EventState.RECOGNIZED, EventState.PENDING])
def test_cancellation_preserves_source_facts_and_retraction_restores(
    state: EventState,
) -> None:
    event = replace(
        _event(),
        state=state,
        effective_role=TimeRole.AUTHORIZED
        if state == EventState.PENDING
        else TimeRole.POSTED,
    )
    inputs = Inputs(
        event.identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (event,),
        (),
        (),
    )
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    cancellation = _assignment(
        _targets(event)[0], "cancel", 1, EventStateAssignment(EventState.CANCELLED)
    )
    cancelled = fold(inputs, resolution, (cancellation,))
    updated = cancelled.resolution.events[0]
    assert updated.state == EventState.CANCELLED
    assert updated.legs == event.legs
    assert event_dates(updated) == event_dates(event)
    assert not any(
        finding.code == FindingCode.UNCLASSIFIED
        for finding in cancelled.resolution.findings
    )
    assert fold(inputs, resolution, ()).resolution.events[0].state == state
    assert cancelled.receipts[0].evaluation.outcome.value == "applied"


@pytest.mark.parametrize("state", [EventState.CANCELLED, EventState.RETRACTED])
@pytest.mark.parametrize(
    "effective_role", [TimeRole.POSTED, TimeRole.AUTHORIZED, TimeRole.INITIATED]
)
def test_inactive_date_edits_retain_source_effective_role(
    state: EventState, effective_role: TimeRole
) -> None:
    event = replace(
        _event(),
        state=EventState.RECOGNIZED
        if effective_role == TimeRole.POSTED
        else EventState.PENDING,
        effective_role=effective_role,
        dates=(date(2026, 1, 20), None, date(2026, 1, 20), None, None),
    )
    inputs = Inputs(
        event.identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (event,),
        (),
        (),
    )
    if state == EventState.RETRACTED:
        event = replace(event, state=state)
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    decisions = (
        (_assignment(_targets(event)[0], "cancel", 1, EventStateAssignment(state)),)
        if state == EventState.CANCELLED
        else ()
    )
    before = fold(inputs, resolution, decisions)
    role = (
        AssignmentDateRole.POSTED
        if effective_role == TimeRole.POSTED
        else AssignmentDateRole.AUTHORIZED
    )
    edit = _assignment(
        targets(before.resolution)[0],
        "date",
        len(decisions) + 1,
        EventDateAssignment(role, date(2026, 1, 21)),
    )
    result = fold(inputs, resolution, (*decisions, edit))
    (determination,) = result.resolution.determinations
    assert determination.status == ResolutionStatus.RESOLVED
    updated = determination.event
    assert updated is not None
    assert updated.state == state
    assert updated.effective_role == effective_role
    assert updated.when == date(
        2026, 1, 20 if effective_role == TimeRole.INITIATED else 21
    )
    assert event_dates(updated)[2 if role == AssignmentDateRole.POSTED else 0] == date(
        2026, 1, 21
    )
    assert updated.legs == event.legs
    assert not any(finding.blocking for finding in result.resolution.findings)


@pytest.mark.parametrize("effective_role", [TimeRole.POSTED, TimeRole.AUTHORIZED])
@pytest.mark.parametrize("clear_effective", [True, False])
def test_inactive_required_date_remains_required(
    effective_role: TimeRole, *, clear_effective: bool
) -> None:
    event = replace(
        _event(),
        state=EventState.CANCELLED,
        effective_role=effective_role,
        dates=(date(2026, 1, 20), None, date(2026, 1, 20), None, None),
    )
    role = (
        AssignmentDateRole.POSTED
        if (effective_role == TimeRole.POSTED) == clear_effective
        else AssignmentDateRole.AUTHORIZED
    )
    inputs = Inputs(
        event.identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (event,),
        (),
        (),
    )
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    edit = _assignment(_targets(event)[0], "date", 1, EventDateAssignment(role, None))
    result = fold(inputs, resolution, (edit,))
    (determination,) = result.resolution.determinations
    assert determination.status == (
        ResolutionStatus.BLOCKED if clear_effective else ResolutionStatus.RESOLVED
    )
    assert (
        any(
            finding.code == FindingCode.CORRECTION_DATE
            for finding in result.resolution.findings
        )
        == clear_effective
    )
    assert determination.event is not None
    assert determination.event.when == event.when


@pytest.mark.parametrize("change", ["role", "date"])
def test_guards_distinguish_effective_context_from_civil_dates_and_participation(
    change: str,
) -> None:
    event = replace(
        _event(),
        state=EventState.CANCELLED,
        dates=(date(2026, 1, 20), None, date(2026, 1, 20), None, None),
    )
    revised = (
        replace(event, effective_role=TimeRole.AUTHORIZED)
        if change == "role"
        else replace(event, when=date(2026, 1, 21))
    )
    for expected, observed in zip(_targets(event), _targets(revised), strict=True):
        assert expected.reference == observed.reference
        assert projection_digest(expected, profile(expected)) != projection_digest(
            observed, profile(observed)
        )


def test_earlier_date_supersession_invalidates_dependent_guard() -> None:
    event = _event()
    inputs = Inputs(
        event.identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (event,),
        (),
        (),
        categories=(SemanticKey("expense"),),
    )
    resolution = Resolution(
        (Determination(event.identity, ResolutionStatus.RESOLVED, event),), ()
    )
    earlier = _assignment(
        _targets(event)[0],
        "date",
        1,
        EventDateAssignment(AssignmentDateRole.POSTED, date(2026, 1, 21)),
    )
    dated = fold(inputs, resolution, (earlier,))
    dependent = _assignment(
        targets(dated.resolution)[2],
        "category",
        2,
        LegCategoryAssignment(SemanticKey("expense")),
    )
    classified = fold(inputs, resolution, (earlier, dependent))
    assert [item.evaluation.outcome.value for item in classified.receipts] == [
        "applied",
        "applied",
    ]
    assert classified.resolution.events[0].legs[1].category == SemanticKey("expense")
    superseded = _assignment(
        earlier.expected,
        "date",
        3,
        EventDateAssignment(AssignmentDateRole.POSTED, date(2026, 1, 22)),
        prior=earlier.entry,
    )
    revised = fold(inputs, resolution, (superseded, dependent))
    assert [item.entry.key.value for item in revised.receipts] == ["date", "category"]
    assert [item.evaluation.outcome.value for item in revised.receipts] == [
        "applied",
        "changed",
    ]
    determination = revised.resolution.determinations[0]
    assert determination.status == ResolutionStatus.BLOCKED
    assert determination.event is not None
    assert event_dates(determination.event)[2] == date(2026, 1, 22)
    assert determination.event.legs[1].category is None


def test_leg_retargeting_uses_reconstructed_original_boundary() -> None:
    original, destination = _named_event("original"), _named_event("destination")
    inputs = Inputs(
        original.identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (original, destination),
        (),
        (),
        categories=(SemanticKey("expense"),),
    )
    resolution = Resolution(
        tuple(
            Determination(event.identity, ResolutionStatus.RESOLVED, event)
            for event in (original, destination)
        ),
        (),
    )
    recorded = _assignment(
        _targets(original)[2],
        "retarget",
        1,
        LegCategoryAssignment(SemanticKey("expense")),
    )
    later = _assignment(
        _targets(destination)[0],
        "later-date",
        2,
        EventDateAssignment(AssignmentDateRole.POSTED, date(2026, 1, 21)),
    )
    reviewed = fold(inputs, resolution, (recorded, later))
    review = {
        "tail": [write_target(target) for target in reviewed.tail],
        "receipts": [
            {
                "decision_key": receipt.entry.key.value,
                "expected": write_target(receipt.expected),
                "candidates": [write_target(target) for target in receipt.candidates],
                "restore": [
                    {
                        "entity_id": reference.entity.value,
                        "table": reference.table.value,
                        "key": list(reference.key),
                        "candidates": [write_target(target) for target in candidates],
                    }
                    for reference, candidates in receipt.restore
                ],
            }
            for receipt in reviewed.receipts
        ],
    }
    boundary = review_boundary(review, "retarget")
    assert independent_boundary({"model": {"review": review}}, "retarget") == [
        write_target(target) for target in boundary
    ]
    expected = next(
        target
        for target in boundary
        if target.reference == _targets(destination)[2].reference
    )
    assert expected == _targets(destination)[2]
    assert expected != targets(reviewed.resolution)[5]
    retargeted = _assignment(
        expected,
        "retarget",
        3,
        LegCategoryAssignment(SemanticKey("expense")),
        prior=recorded.entry,
    )
    result = fold(inputs, resolution, (retargeted, later))
    assert [item.entry.key.value for item in result.receipts] == [
        "retarget",
        "later-date",
    ]
    assert [item.evaluation.outcome.value for item in result.receipts] == [
        "applied",
        "applied",
    ]
    first, second = result.resolution.events
    assert first.legs[1].category is None
    assert second.legs[1].category == SemanticKey("expense")
    assert event_dates(second)[2] == date(2026, 1, 21)
    damaged = deepcopy(review)
    damaged["receipts"][-1]["restore"] = []
    with pytest.raises(ContractError, match="review_boundary_receipt"):
        review_boundary(damaged, "retarget")
    with pytest.raises(ReaderError, match="reconstructed pre-entry boundary"):
        independent_boundary({"model": {"review": damaged}}, "retarget")


@pytest.mark.parametrize("unrelated_count", [0, 32])
@pytest.mark.parametrize(
    ("outcome", "candidate_count"),
    [("applied", 1), ("changed", 1), ("inactive", 1), ("missing", 0), ("ambiguous", 2)],
)
def test_review_projection_and_retained_candidates_are_target_scoped(
    monkeypatch: pytest.MonkeyPatch,
    unrelated_count: int,
    outcome: str,
    candidate_count: int,
) -> None:
    addressed = tuple(_named_event(f"addressed-{index}") for index in range(6))
    unrelated = tuple(
        _named_event(f"unrelated-{index}") for index in range(unrelated_count)
    )
    unrelated_refs = {
        target.reference for event in unrelated for target in _targets(event)
    }
    determinations = [
        Determination(event.identity, ResolutionStatus.RESOLVED, event)
        for event in unrelated
    ]
    decisions = []
    for index, event in enumerate(addressed):
        expected = _targets(event)[2]
        entry = JournalEntry(
            index + 1,
            "d" * 64 if index else None,
            event.identity.entity,
            SemanticKey(f"classify-{index}"),
            DecisionKind.ASSIGN,
            JournalOperation.RECORD,
            None,
            "a" * 64,
            "classify synthetic expense",
            "c" * 64,
        )
        decisions.append(
            Decision(
                entry,
                AssignBody(
                    event.identity.entity,
                    ReviewBasisRef("a" * 64, "b" * 64),
                    LegReviewRef(
                        RecordId(
                            event.identity.entity,
                            RecordKind.LEG,
                            str(expected.reference.key[0]),
                        )
                    ),
                    (LegCategoryAssignment(SemanticKey("expense")),),
                ),
                expected,
            )
        )
        revised = replace(
            event,
            legs=tuple(
                replace(
                    leg,
                    amount=ExactDecimal.parse(
                        "-7" if leg.kind == LegKind.POSITION else "7"
                    ),
                )
                for leg in event.legs
            ),
        )
        if outcome != "missing":
            determinations.append(
                Determination(
                    event.identity,
                    ResolutionStatus.BLOCKED
                    if outcome in {"ambiguous", "inactive"}
                    else ResolutionStatus.RESOLVED,
                    None
                    if outcome == "ambiguous"
                    else revised
                    if outcome == "changed"
                    else event,
                    (event, revised) if outcome == "ambiguous" else (),
                )
            )
    inputs = Inputs(
        addressed[0].identity.entity,
        date(2026, 2, 1),
        (),
        (),
        (),
        (),
        (),
        (),
        (*addressed, *unrelated),
        (),
        (),
        categories=(SemanticKey("expense"),),
    )
    projected: list[tuple[Event, GuardReference | None]] = []
    original = corrections._event_targets  # noqa: SLF001 - deterministic work bound

    def observe(
        event: Event, *, active: bool, reference: GuardReference | None = None
    ) -> tuple[GuardTarget, ...]:
        projected.append((event, reference))
        return original(event, active=active, reference=reference)

    monkeypatch.setattr(corrections, "_event_targets", observe)
    result = fold(inputs, Resolution(tuple(determinations), ()), tuple(decisions))
    assert [receipt.evaluation.outcome.value for receipt in result.receipts] == [
        outcome
    ] * len(decisions)
    assert [len(receipt.candidates) for receipt in result.receipts] == [
        candidate_count
    ] * len(decisions)
    assert all(
        candidate.reference == receipt.expected.reference
        for receipt in result.receipts
        for candidate in receipt.candidates
    )
    inventory = [event for event, reference in projected if reference is None]
    assert all(reference is None for _, reference in projected)
    assert all(
        sum(event.identity == item.identity for item in inventory) == 1
        for event in unrelated
    )
    assert len(inventory) <= unrelated_count + len(decisions) * 3
    assert len(result.tail) == (unrelated_count + len(decisions) * candidate_count) * 3
    assert all(
        reference not in unrelated_refs
        for receipt in result.receipts
        for reference, _ in receipt.restore
    )
    assert (
        sum(len(receipt.restore) for receipt in result.receipts) <= len(decisions) * 3
    )
