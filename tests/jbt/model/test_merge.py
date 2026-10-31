from dataclasses import replace
from datetime import date
from hashlib import sha256
from itertools import permutations
from typing import TYPE_CHECKING

import pytest

from jbt.domain.identity import (
    AcquiredAnchor,
    Occurrence,
    SourceKeyAnchor,
    event_id,
    source_record_digest,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind
from jbt.domain.time import Precision, TimeObservation, TimeRole
from jbt.model import merge as source_merge
from jbt.model.corrections import targets
from jbt.model.merge import (
    _overlaps,
    balance_dispositions,
    coverage_segments,
    occurrence_evidence,
    source_membership,
    statement_evidence,
)
from jbt.model.quantity import (
    AuthorityMode,
    Binding,
    FindingCode,
    IdentitySelection,
    Inputs,
    Movement,
    Position,
    ResolutionStatus,
    RevisionAction,
    SourceAuthority,
    SourceClass,
    SourceLeg,
    SourceRevision,
    SourceStatementRevision,
    Statement,
    StatementPart,
    StatementReplacement,
    SupportKind,
    Symbol,
    Validity,
    event_dates,
    merge,
    resolve,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

ENTITY = EntityId("synthetic")
ACCOUNT = SemanticKey("bank")
SECTION = SemanticKey("account-section")
COMPONENT = SemanticKey("cash-movement")
BASIS = SemanticKey("posted")
JANUARY = Validity(date(2026, 1, 1), date(2026, 2, 1))
UNBOUNDED = Validity(None, None)


@pytest.mark.parametrize("count", [32, 64])
@pytest.mark.parametrize("case", ["keyed", "single_blob_keyless", "overlap"])
def test_overlap_work_only_compares_keyless_records_from_other_downloads(
    count: int,
    case: str,
    mocker: MockerFixture,
) -> None:
    movements = tuple(
        movement("keyed", f"key-{index}", -5, locator=f"row-{index}")
        for index in range(count)
    )
    if case != "keyed":
        movements = (
            *movements,
            movement(
                "other" if case == "overlap" else "keyed", None, -7, locator="keyless"
            ),
        )
    context = inputs(movements)
    groups = {
        event_id(ENTITY, Occurrence(item.source_key or item.anchor, COMPONENT)): [item]
        for item in movements
    }
    bindings = mocker.spy(source_merge, "resolve_binding")
    comparisons = mocker.spy(source_merge, "_intervals_overlap")
    blocked = _overlaps(context, groups, {})
    if case == "overlap":
        assert blocked == {
            event_id(ENTITY, Occurrence(movements[-1].anchor, COMPONENT))
        }
        assert bindings.call_count == len(movements)
        assert comparisons.call_count == count
    else:
        assert blocked == set()
        assert bindings.call_count == comparisons.call_count == 0


def digest(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def movement(  # noqa: PLR0913 - independent synthetic source coordinates
    download: str,
    key: str | None,
    amount: int,
    *,
    day: int = 12,
    scope: str = "bank-feed",
    state: EventState = EventState.RECOGNIZED,
    locator: str = "row",
) -> Movement:
    anchor = AcquiredAnchor(
        digest(download), SemanticKey(scope), SECTION, SemanticKey(f"test:{locator}")
    )
    return Movement(
        RecordId(ENTITY, RecordKind.SOURCE_RECORD, source_record_digest(anchor)),
        anchor,
        SourceKeyAnchor(
            anchor.namespace, SECTION, SemanticKey("fitid"), SemanticKey(key)
        )
        if key is not None
        else None,
        COMPONENT,
        date(2026, 1, day),
        state,
        "Synthetic merchant",
        None,
        (
            SourceLeg(
                SemanticKey("cash"), "USD", ExactDecimal(str(amount), 0, None), None
            ),
        ),
        False,  # noqa: FBT003
        (date(2026, 1, day), None, None, None, None)
        if state == EventState.PENDING
        else (None, None, date(2026, 1, day), None, None),
        (),
        None,
        JANUARY,
        effective_role=TimeRole.AUTHORIZED
        if state == EventState.PENDING
        else TimeRole.POSTED,
        statement=SemanticKey(digest(f"statement:{download}")),
    )


def statement(  # noqa: PLR0913 - independent synthetic evidence coordinates
    download: str,
    movements: tuple[Movement, ...],
    *,
    complete: bool = True,
    validity: Validity = JANUARY,
    basis: SemanticKey = BASIS,
    role: TimeRole = TimeRole.POSTED,
    scope: str = "bank-feed",
) -> Statement:
    return Statement(
        SemanticKey(digest(f"statement:{download}")),
        SemanticKey(scope),
        SECTION,
        digest(download),
        ACCOUNT,
        (
            StatementPart(
                SemanticKey("movements"),
                FactKind.MOVEMENT,
                basis,
                role,
                validity,
                complete,
                tuple(m.source_record for m in movements),
            ),
        ),
    )


def inputs(movements: tuple[Movement, ...], **changes: object) -> Inputs:
    scopes = {m.anchor.namespace for m in movements}
    policy = RecordId(ENTITY, RecordKind.DECLARATION_REVISION, digest("binding"))
    context = Inputs(
        ENTITY,
        date(2026, 2, 1),
        tuple(Binding(scope, SECTION, ACCOUNT, UNBOUNDED, policy) for scope in scopes),
        (),
        (Symbol("USD", SemanticKey("USD"), None, UNBOUNDED, policy),),
        (
            Position(
                SemanticKey("cash"),
                ACCOUNT,
                SemanticKey("USD"),
                None,
                None,
                None,
                UNBOUNDED,
                policy,
            ),
        ),
        (),
        movements,
        (),
        (),
        (),
    )
    return replace(context, **changes)


def authority(
    *,
    source_scope: str = "official",
    role: TimeRole = TimeRole.POSTED,
    basis: SemanticKey = BASIS,
    mode: AuthorityMode = AuthorityMode.COMPLETE_REPLACEMENT,
) -> SourceAuthority:
    return SourceAuthority(
        ACCOUNT,
        FactKind.MOVEMENT,
        basis,
        role,
        JANUARY,
        SemanticKey(source_scope),
        mode,
    )


def correction(
    movement: Movement, target: Movement, action: RevisionAction
) -> SourceRevision:
    assert target.source_key is not None
    return SourceRevision(movement.source_record, target.source_key, action)


def keyed(movement: Movement) -> SourceKeyAnchor:
    assert movement.source_key is not None
    return movement.source_key


def test_causal_replacement_chain_keeps_original_id_without_date_or_arrival_order() -> (
    None
):
    original = movement("original", "A", -5, day=20)
    middle = movement("middle", "B", -6, day=15)
    corrected = movement("corrected", "C", -7, day=10)
    revisions = (
        correction(middle, original, RevisionAction.REPLACE),
        correction(corrected, middle, RevisionAction.REPLACE),
    )
    identity = event_id(ENTITY, Occurrence(keyed(original), COMPONENT))
    for ordering in permutations((original, middle, corrected)):
        for edges in (revisions, tuple(reversed(revisions))):
            resolution = merge(inputs(ordering, source_revisions=edges))
            assert not any(f.blocking for f in resolution.findings)
            (event,) = resolution.events
            assert event.identity == identity
            assert event.when == date(2026, 1, 10)
            assert event.legs[0].amount.coefficient == "-7"
            assert {s.source_record: s.kind for s in event.support} == {
                original.source_record: SupportKind.REPLACED,
                middle.source_record: SupportKind.REPLACED,
                corrected.source_record: SupportKind.SELECTED,
            }
            assert resolution.retired == ()


def test_future_source_event_retains_native_candidate_without_emitting_event() -> None:
    source = movement("future", "A", -5)
    source = replace(
        source,
        when=date(2026, 2, 20),
        dates=(None, None, date(2026, 2, 20), None, None),
    )
    resolution = merge(inputs((source,)))
    assert resolution.events == ()
    (determination,) = resolution.determinations
    assert determination.status == ResolutionStatus.RESOLVED
    assert determination.event is None
    (candidate,) = determination.candidates
    assert candidate.identity == event_id(ENTITY, Occurrence(keyed(source), COMPONENT))
    assert candidate.when == date(2026, 2, 20)
    assert candidate.legs[0].amount.coefficient == "-5"
    reviewed = targets(resolution)
    assert len(reviewed) == 3
    assert all(not target.active for target in reviewed)


def test_keyless_overlap_uses_statement_scope_without_movement_search_bounds() -> None:
    first, second = (
        replace(movement(download, None, -5), movement_interval=None)
        for download in ("first", "second")
    )
    resolution = merge(
        inputs(
            (first, second),
            statements=(
                statement("first", (first,)),
                statement("second", (second,)),
            ),
        )
    )
    assert not resolution.events
    assert {
        finding.scope
        for finding in resolution.findings
        if finding.code == FindingCode.OVERLAP and finding.blocking
    } == {
        event_id(ENTITY, Occurrence(item.anchor, COMPONENT)) for item in (first, second)
    }


def test_deletion_retracts_original_family_not_a_later_reversal() -> None:
    original = replace(movement("first", "A", -5), dates=())
    deleted = replace(movement("second", "delete-A", -5, day=10), dates=())
    reversal = movement("third", "reversal", 5, day=20)
    resolution = resolve(
        inputs(
            (reversal, deleted, original),
            source_revisions=(correction(deleted, original, RevisionAction.DELETE),),
        )
    )
    by_state = {event.state: event for event in resolution.events}
    retracted = by_state[EventState.RETRACTED]
    assert retracted.identity == event_id(
        ENTITY, Occurrence(keyed(original), COMPONENT)
    )
    assert all(s.kind == SupportKind.REPLACED for s in retracted.support)
    assert event_dates(retracted) == (None, None, retracted.when, None, None)
    assert by_state[EventState.RECOGNIZED].legs[0].amount.coefficient == "5"


def test_replacement_can_name_original_family_without_original_download() -> None:
    original = movement("not-selected", "A", -5)
    corrected = movement("corrected", "B", -7)
    resolution = merge(
        inputs(
            (corrected,),
            source_revisions=(correction(corrected, original, RevisionAction.REPLACE),),
        )
    )
    (event,) = resolution.events
    assert event.identity == event_id(ENTITY, Occurrence(keyed(original), COMPONENT))
    assert event.legs[0].amount.coefficient == "-7"


def test_same_key_source_correction_selects_stated_correcting_occurrence() -> None:
    original = movement("original", "A", -5)
    corrected = movement("corrected", "A", -7)
    resolution = merge(
        inputs(
            (original, corrected),
            source_revisions=(correction(corrected, original, RevisionAction.REPLACE),),
        )
    )
    (event,) = resolution.events
    assert event.legs[0].amount.coefficient == "-7"
    assert {s.kind for s in event.support} == {
        SupportKind.SELECTED,
        SupportKind.REPLACED,
    }


def test_repeated_correcting_fitid_is_confirmation_not_branch() -> None:
    original = movement("original", "A", -5)
    first = movement("correcting-first", "B", -7)
    repeated = movement("correcting-repeat", "B", -7)
    context = inputs(
        (repeated, original, first),
        source_revisions=(
            correction(first, original, RevisionAction.REPLACE),
            correction(repeated, original, RevisionAction.REPLACE),
        ),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    (event,) = resolution.events
    assert event.identity == event_id(ENTITY, Occurrence(keyed(original), COMPONENT))
    assert {support.kind for support in event.support} == {
        SupportKind.REPLACED,
        SupportKind.SELECTED,
        SupportKind.CONFIRMING,
    }


def test_identity_confirmation_cannot_collapse_different_bound_accounts() -> None:
    first = movement("first", "A", -5)
    other = movement("other", "B", -5, scope="other-feed")
    context = inputs(
        (first, other),
        identity_selections=(
            IdentitySelection(
                Occurrence(keyed(first), COMPONENT), families=(keyed(other),)
            ),
        ),
    )
    other_account = SemanticKey("other-bank")
    context = replace(
        context,
        bindings=tuple(
            replace(binding, account=other_account)
            if binding.scope.value == "other-feed"
            else binding
            for binding in context.bindings
        ),
        positions=(
            *context.positions,
            replace(
                context.positions[0],
                key=SemanticKey("other-cash"),
                account=other_account,
            ),
        ),
    )
    resolution = merge(context)
    assert resolution.events == ()
    assert any(f.code == FindingCode.KEY_CONFLICT for f in resolution.findings)


def test_balance_authority_eclipses_same_basis_point_not_parallel_view() -> None:
    first = movement("first", "A", -5, scope="rolling")
    official = movement("official", "B", -5, scope="official")
    point = TimeObservation(date(2026, 1, 20), None, None, None, Precision.DATE, None)
    first_statement = statement("first", (first,), scope="rolling")
    official_statement = statement("official", (official,), scope="official")
    balance = replace(
        first_statement.parts[0],
        fact_kind=FactKind.BALANCE,
        point=point,
        validity=Validity(point.date, date(2026, 1, 21)),
    )
    official_balance = replace(balance, source_records=(official.source_record,))
    parallel = replace(
        balance,
        basis=SemanticKey("available"),
        source_records=(
            RecordId(ENTITY, RecordKind.SOURCE_RECORD, digest("available")),
        ),
    )
    context = inputs(
        (first, official),
        statements=(
            replace(first_statement, parts=(balance, parallel)),
            replace(official_statement, parts=(official_balance,)),
        ),
        authorities=(replace(authority(), fact_kind=FactKind.BALANCE),),
    )
    assert balance_dispositions(context) == {first.source_record: SupportKind.ECLIPSED}


@pytest.mark.parametrize("cycle", [False, True])
def test_conflicting_or_cyclic_source_revision_evidence_blocks_every_affected_family(
    *,
    cycle: bool,
) -> None:
    original = movement("original", "A", -5)
    left = movement("left", "B", -6)
    right = movement("right", "C", -7)
    revisions = (
        correction(left, original, RevisionAction.REPLACE),
        correction(original, left, RevisionAction.REPLACE)
        if cycle
        else correction(right, original, RevisionAction.REPLACE),
    )
    context = inputs(
        (original, left) if cycle else (original, left, right),
        source_revisions=revisions,
    )
    resolution = merge(context)
    assert resolution.events == ()
    assert all(d.status == ResolutionStatus.BLOCKED for d in resolution.determinations)
    assert any(f.code == FindingCode.SOURCE_REVISION for f in resolution.findings)
    evidence = occurrence_evidence(context)
    assert len(evidence) == len(context.movements)
    assert all(e.legs[0].account == ACCOUNT for e in evidence)


def test_complete_authority_eclipse_uses_nonreplaced_complete_part() -> None:
    covered = movement("rolling", None, -3, scope="rolling", day=10, locator="inside")
    outside = movement("rolling", None, -4, scope="rolling", day=25, locator="outside")
    official = movement("official", "official-key", -5, scope="official")
    context = inputs(
        (covered, outside, official),
        statements=(
            statement("rolling", (covered, outside), scope="rolling"),
            statement(
                "official",
                (official,),
                scope="official",
                validity=Validity(date(2026, 1, 1), date(2026, 1, 20)),
            ),
        ),
        authorities=(authority(),),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    assert coverage_segments(
        context, context.statements[0], context.statements[0].parts[0]
    ) == (
        (date(2026, 1, 1), date(2026, 1, 20), True),
        (date(2026, 1, 20), date(2026, 2, 1), False),
    )
    by_id = {e.identity: e for e in resolution.events}
    eclipsed = by_id[event_id(ENTITY, Occurrence(covered.anchor, COMPONENT))]
    assert eclipsed.state == EventState.RETRACTED
    assert eclipsed.support[0].kind == SupportKind.ECLIPSED
    assert (
        by_id[event_id(ENTITY, Occurrence(outside.anchor, COMPONENT))].state
        == EventState.RECOGNIZED
    )
    partial = replace(
        context.statements[1],
        parts=(replace(context.statements[1].parts[0], complete=False),),
    )
    unresolved = merge(replace(context, statements=(context.statements[0], partial)))
    partial_context = replace(context, statements=(context.statements[0], partial))
    assert coverage_segments(
        partial_context, context.statements[0], context.statements[0].parts[0]
    ) == ((date(2026, 1, 1), date(2026, 2, 1), False),)
    assert any(
        f.code == FindingCode.OVERLAP and f.blocking for f in unresolved.findings
    )


def test_authority_uses_declared_basis_role_not_posted_selection_date() -> None:
    rolling = movement("rolling", None, -3, scope="rolling", day=25)
    rolling = replace(
        rolling, dates=(None, None, date(2026, 1, 25), date(2026, 1, 10), None)
    )
    official = movement("official", "key", -5, scope="official")
    basis = SemanticKey("settled")
    context = inputs(
        (rolling, official),
        statements=(
            statement(
                "rolling",
                (rolling,),
                scope="rolling",
                basis=basis,
                role=TimeRole.SETTLED,
            ),
            statement(
                "official",
                (official,),
                scope="official",
                basis=basis,
                role=TimeRole.SETTLED,
                validity=Validity(date(2026, 1, 1), date(2026, 1, 20)),
            ),
        ),
        authorities=(authority(role=TimeRole.SETTLED, basis=basis),),
    )
    (evidence,) = [e for e in occurrence_evidence(context) if e.movement == rolling]
    assert evidence.disposition == SupportKind.ECLIPSED
    missing_role = replace(rolling, dates=(None, None, date(2026, 1, 10), None, None))
    (evidence,) = [
        e
        for e in occurrence_evidence(
            replace(context, movements=(missing_role, official))
        )
        if e.movement == missing_role
    ]
    assert evidence.disposition is None


def test_replaced_complete_authoritative_part_cannot_eclipse_with_partial_reissue() -> (
    None
):
    rolling = movement("rolling", None, -3, scope="rolling")
    old = movement("old-official", "A", -5, scope="official")
    new = movement("new-official", "B", -7, scope="official")
    rolling_statement = statement("rolling", (rolling,), scope="rolling")
    old_statement = statement("old-official", (old,), scope="official")
    new_statement = statement("new-official", (new,), complete=False, scope="official")
    context = inputs(
        (rolling, old, new),
        statements=(rolling_statement, old_statement, new_statement),
        source_statement_revisions=(
            SourceStatementRevision(
                new_statement.identity, old_statement.blob_digest, replaces=True
            ),
        ),
        authorities=(authority(),),
    )
    evidence = next(e for e in occurrence_evidence(context) if e.movement == rolling)
    assert evidence.disposition is None
    assert any(f.code == FindingCode.OVERLAP for f in merge(context).findings)


def test_family_redirect_chains_and_exact_occurrence_split_have_distinct_scope() -> (
    None
):
    first = movement("first", "A", -5)
    second = movement("second", "B", -5)
    canonical = movement("third", "C", -5)
    context = inputs(
        (first, second, canonical),
        identity_selections=(
            IdentitySelection(
                Occurrence(keyed(second), COMPONENT), families=(keyed(first),)
            ),
            IdentitySelection(
                Occurrence(keyed(canonical), COMPONENT),
                families=(keyed(second),),
            ),
        ),
    )
    assert len(merge(context).events) == 1
    exact = IdentitySelection(
        Occurrence(first.anchor, COMPONENT), (first.source_record,)
    )
    split = merge(
        replace(context, identity_selections=(*context.identity_selections, exact))
    )
    assert len(split.events) == 2
    assert not any(f.blocking for f in split.findings)


def test_family_redirect_preserves_independent_event_components() -> None:
    first = movement("first", "A", -5)
    canonical = movement("second", "B", -5)
    fee = replace(
        movement("first", "A", -1, locator="fee"),
        event_key=SemanticKey("fee"),
    )
    context = inputs(
        (first, canonical, fee),
        identity_selections=(
            IdentitySelection(
                Occurrence(keyed(canonical), COMPONENT), families=(keyed(first),)
            ),
        ),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    assert source_membership(context)[fee.source_record] == Occurrence(
        keyed(first), fee.event_key
    )
    assert {event.identity for event in resolution.events} == {
        event_id(ENTITY, Occurrence(keyed(canonical), COMPONENT)),
        event_id(ENTITY, Occurrence(keyed(first), fee.event_key)),
    }


def test_authoritative_overlap_with_shared_and_distinct_keys_is_valid() -> None:
    first = movement("first", "A", -5)
    confirming = movement("second", "A", -5)
    distinct = movement("second", "B", -5, locator="second-row")
    context = inputs(
        (distinct, first, confirming),
        statements=(
            statement("first", (first,)),
            statement("second", (confirming, distinct)),
        ),
        authorities=(authority(source_scope="bank-feed"),),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    assert len(resolution.events) == 2
    assert sorted(len(e.support) for e in resolution.events) == [1, 2]
    disagreement = replace(
        confirming,
        legs=(replace(confirming.legs[0], amount=ExactDecimal("-7", 0, None)),),
    )
    conflict = merge(replace(context, movements=(first, disagreement, distinct)))
    assert any(
        f.code == FindingCode.AUTHORITY and f.blocking for f in conflict.findings
    )
    assert len(conflict.events) == 1


def test_complete_reissue_selects_new_facts_over_same_scope_rolling_observation() -> (
    None
):
    rolling = movement("rolling", "A", -5)
    old = movement("old", "A", -5)
    revised = movement("revised", "A", -6)
    rolling_statement = replace(
        statement("rolling", (rolling,)), source_class=SourceClass.ROLLING
    )
    old_statement = statement("old", (old,))
    revised_statement = statement("revised", (revised,))
    context = inputs(
        (rolling, old, revised),
        statements=(rolling_statement, old_statement, revised_statement),
        authorities=(authority(source_scope="bank-feed"),),
        statement_replacements=(
            StatementReplacement(
                (old_statement.identity,),
                (revised_statement.identity,),
                ACCOUNT,
                FactKind.MOVEMENT,
                BASIS,
                TimeRole.POSTED,
                JANUARY,
            ),
        ),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    (event,) = resolution.events
    assert event.legs[0].amount.coefficient == "-6"
    assert {s.source_record: s.kind for s in event.support} == {
        old.source_record: SupportKind.REPLACED,
        revised.source_record: SupportKind.SELECTED,
        rolling.source_record: SupportKind.ECLIPSED,
    }


def test_parallel_authority_bases_valid_but_same_basis_overlapping_policies_block() -> (
    None
):
    first = movement("first", "A", -5)
    context = inputs(
        (first,),
        statements=(statement("first", (first,)),),
        authorities=(
            authority(source_scope="bank-feed"),
            authority(basis=SemanticKey("available")),
        ),
    )
    assert not any(f.blocking for f in merge(context).findings)
    conflict = merge(
        replace(context, authorities=(context.authorities[0], context.authorities[0]))
    )
    assert any(
        f.code == FindingCode.AUTHORITY and f.blocking for f in conflict.findings
    )


@pytest.mark.parametrize("scope", ["matching", "period", "basis", "fact_kind", "empty"])
def test_authority_conflicts_report_policy_scope_without_blocking_unrelated_facts(
    scope: str,
) -> None:
    source = movement("first", "A", -5, day=25 if scope == "period" else 12)
    policy = replace(
        authority(source_scope="bank-feed"),
        validity=Validity(date(2026, 1, 1), date(2026, 1, 20)),
        basis=SemanticKey("available") if scope == "basis" else BASIS,
        fact_kind=FactKind.BALANCE if scope == "fact_kind" else FactKind.MOVEMENT,
    )
    context = inputs(
        () if scope == "empty" else (source,),
        statements=(statement("first", (source,)),),
        authorities=(policy, policy),
    )
    resolution = merge(context)
    assert any(
        finding.code == FindingCode.AUTHORITY
        and finding.scope == ACCOUNT
        and finding.blocking
        for finding in resolution.findings
    )
    if scope == "empty":
        assert not resolution.determinations
    else:
        (determination,) = resolution.determinations
        assert determination.status == (
            ResolutionStatus.BLOCKED
            if scope == "matching"
            else ResolutionStatus.RESOLVED
        )


def test_pending_to_recognized_selects_recognized_facts_after_identity_merge() -> None:
    pending = movement("authorization", None, -5, state=EventState.PENDING, day=10)
    posted = movement("posted", "posted-key", -6, day=12)
    canonical = Occurrence(keyed(posted), COMPONENT)
    context = inputs(
        (pending, posted),
        identity_selections=(IdentitySelection(canonical, (pending.source_record,)),),
    )
    resolution = merge(context)
    (event,) = resolution.events
    assert event.state == EventState.RECOGNIZED
    assert event.when == date(2026, 1, 12)
    assert event.legs[0].amount.coefficient == "-6"
    assert source_membership(context)[pending.source_record] == canonical
    pending_only = merge(inputs((pending,)))
    assert pending_only.events[0].state == EventState.PENDING
    assert any(
        f.code == FindingCode.PENDING and not f.blocking for f in pending_only.findings
    )


def test_scoped_reissue_retracts_only_replaced_period_and_retains_reversal() -> None:
    old = movement("old", "A", -5, day=10)
    outside = movement("old", "outside", -3, day=25, locator="outside")
    reversal = movement("reissue", "reversal", 5, day=12)
    original_statement = statement("old", (old, outside))
    new_statement = statement("reissue", (reversal,))
    replacement = StatementReplacement(
        (original_statement.identity,),
        (new_statement.identity,),
        ACCOUNT,
        FactKind.MOVEMENT,
        BASIS,
        TimeRole.POSTED,
        Validity(date(2026, 1, 1), date(2026, 1, 20)),
    )
    context = inputs(
        (outside, reversal, old),
        statements=(original_statement, new_statement),
        statement_replacements=(replacement,),
    )
    resolution = merge(context)
    assert not any(f.blocking for f in resolution.findings)
    assert {e.state for e in resolution.events} == {
        EventState.RETRACTED,
        EventState.RECOGNIZED,
    }
    (retracted,) = [e for e in resolution.events if e.state == EventState.RETRACTED]
    assert retracted.identity == event_id(ENTITY, Occurrence(keyed(old), COMPONENT))
    assert retracted.support[0].kind == SupportKind.REPLACED
    assert len([e for e in resolution.events if e.state == EventState.RECOGNIZED]) == 2
    assert all(
        revisions == (replacement,) for _, revisions in statement_evidence(context)
    )


def test_source_reissue_retracts_and_blocks_missing_or_cyclic_target() -> None:
    old = movement("old", "A", -5)
    new = movement("new", "B", -7)
    old_statement, new_statement = statement("old", (old,)), statement("new", (new,))
    revision = SourceStatementRevision(
        new_statement.identity, old_statement.blob_digest, replaces=True
    )
    context = inputs(
        (new, old),
        statements=(old_statement, new_statement),
        source_statement_revisions=(revision,),
    )
    assert [e.state for e in merge(context).events].count(EventState.RETRACTED) == 1
    missing = merge(
        replace(
            context,
            source_statement_revisions=(
                replace(revision, target_blob_digest=digest("missing")),
            ),
        )
    )
    assert any(
        f.code == FindingCode.SOURCE_REVISION and f.blocking for f in missing.findings
    )
    cyclic = merge(
        replace(
            context,
            source_statement_revisions=(
                revision,
                SourceStatementRevision(
                    old_statement.identity, new_statement.blob_digest, replaces=True
                ),
            ),
        )
    )
    assert cyclic.events == ()


def test_statement_revision_branches_block_but_disjoint_scope_edges_do_not_cycle() -> (
    None
):
    old = movement("old", "A", -5, day=10)
    left = movement("left", "B", -6, day=25)
    right = movement("right", "C", -7)
    old_statement = statement("old", (old,))
    left_statement = statement("left", (left,))
    right_statement = statement("right", (right,))
    first = StatementReplacement(
        (old_statement.identity,),
        (left_statement.identity,),
        ACCOUNT,
        FactKind.MOVEMENT,
        BASIS,
        TimeRole.POSTED,
        JANUARY,
    )
    context = inputs(
        (old, left, right),
        statements=(old_statement, left_statement, right_statement),
        statement_replacements=(
            first,
            replace(first, replacement=(right_statement.identity,)),
        ),
    )
    assert merge(context).events == ()
    assert any(f.code == FindingCode.SOURCE_REVISION for f in merge(context).findings)
    scoped = replace(
        context,
        statement_replacements=(
            replace(first, validity=Validity(date(2026, 1, 1), date(2026, 1, 20))),
            replace(
                first,
                replaced=(left_statement.identity,),
                replacement=(old_statement.identity,),
                validity=Validity(date(2026, 1, 20), date(2026, 2, 1)),
            ),
        ),
    )
    assert not any(f.blocking for f in merge(scoped).findings)


def test_occurrence_split_and_family_redirect_retire_only_wholly_moved_ids() -> None:
    first = movement("first", "reused", -5)
    second = movement("second", "reused", -7)
    context = inputs(
        (first, second),
        identity_selections=(
            IdentitySelection(
                Occurrence(first.anchor, COMPONENT),
                (first.source_record,),
            ),
        ),
    )
    split = merge(context)
    assert len(split.events) == 2
    assert split.retired == ()
    fully_split = merge(
        replace(
            context,
            identity_selections=(
                *context.identity_selections,
                IdentitySelection(
                    Occurrence(second.anchor, COMPONENT), (second.source_record,)
                ),
            ),
        )
    )
    original = event_id(ENTITY, Occurrence(keyed(first), COMPONENT))
    assert fully_split.retired == (original,)
    assert fully_split.redirects == ()
    canonical = Occurrence(keyed(first), COMPONENT)
    other_family = movement("third", "other", -5)
    redirected = merge(
        inputs(
            (first, other_family),
            identity_selections=(
                IdentitySelection(canonical, families=(keyed(other_family),)),
            ),
        )
    )
    assert redirected.redirects == (
        (
            event_id(ENTITY, Occurrence(keyed(other_family), COMPONENT)),
            event_id(ENTITY, canonical),
        ),
    )
    assert len(redirected.events) == 1
