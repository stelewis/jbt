from copy import deepcopy
from dataclasses import replace
from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from tests.integration.step_0.consumer_arithmetic import Number, Ratio, multiply
from tests.integration.step_0.consumer_policy import (
    AccountBinding,
    Authority,
    BoundaryConvention,
    DateRole,
    Feed,
    Flow,
    FlowKind,
    FXPolicy,
    Interval,
    Key,
    LotPolicy,
    Member,
    OwnershipPolicy,
    PerformancePolicy,
    PolicyError,
    PopulationPolicy,
    PriceBasis,
    Revision,
    Share,
    Transfer,
    ValuationPolicy,
    attribute,
    cash_performance,
    check_authority,
    consolidate_cash,
    external_flows,
    gross_state,
    match_original_lots,
    matched_principal,
    original_acquisitions,
    require_members,
    select_price,
    verify_handoff,
)
from tests.integration.step_0.corpus import FixtureCase, load_case
from tests.integration.step_0.reader import read_snapshot
from tests.integration.step_0.test_reader import _snapshot, approved_schemas

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.step_0.reader import Tables

pytestmark = [pytest.mark.integration, pytest.mark.golden]
YEAR = Interval(date(2026, 1, 1), date(2027, 1, 1))
JANUARY = Interval(date(2026, 1, 1), date(2026, 2, 1))
JAN_END = date(2026, 1, 31)


def _revision(name: str) -> Revision:
    return Revision(name, sha256(name.encode()).hexdigest(), 1, YEAR)


def _key(case: FixtureCase, alias: str) -> Key:
    return Key(cast("str", case.tables["build"][0]["entity_id"]), case.aliases[alias])


def _feed(case: FixtureCase) -> Feed:
    entity = cast("str", case.tables["build"][0]["entity_id"])
    return Feed(
        Member(entity, sha256(case.case_id.encode()).hexdigest(), 1),
        cast("Tables", case.tables),
    )


@pytest.fixture
def verified_root() -> Iterator[Path]:
    root = Path.cwd() / (".reader-policy-" + uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        rmtree(root)


def _verified_case(root: Path, name: str) -> tuple[FixtureCase, Feed]:
    location = root / name.replace("/", "_")
    location.mkdir()
    descriptor = _snapshot(location, case_id=name)
    snapshot = read_snapshot(
        location,
        descriptor_digest=descriptor,
        expected_schema_digests=approved_schemas(),
    )
    case = replace(
        load_case(name),
        tables=cast("dict[str, list[dict[str, object]]]", snapshot.tables),
    )
    entity = cast("str", snapshot.tables["build"][0]["entity_id"])
    return case, Feed(Member(entity, descriptor, 1), snapshot.tables)


@pytest.fixture(scope="module")
def cases() -> dict[str, FixtureCase]:
    names = (
        "breadth/consumer-a",
        "breadth/consumer-b",
        "breadth/quotes",
        "compound-expensed",
        "breadth/invested-copy",
        "pools",
        "breadth/pools-copy",
        "reference",
        "breadth/reference-copy",
        "breadth/onchain",
    )
    return {name: load_case(name) for name in names}


def _population(left: FixtureCase, right: FixtureCase) -> PopulationPolicy:
    return PopulationPolicy(
        _revision("household-population"),
        (_feed(left).member, _feed(right).member),
        tuple(
            AccountBinding(_key(case, "A"), "joint", _key(case, "USD"))
            for case in (left, right)
        ),
        (Authority("joint", "owner-a", JANUARY, DateRole.SETTLED),),
        DateRole.SETTLED,
    )


def test_independent_owner_copies_count_joint_once_and_attribute_explicit_shares(
    cases: dict[str, FixtureCase],
) -> None:
    left, right = cases["breadth/consumer-a"], cases["breadth/consumer-b"]
    assert set(left.evidence["blobs"]) != set(right.evidence["blobs"])
    result = consolidate_cash(
        [_feed(left), _feed(right)],
        _population(left, right),
        period=JANUARY,
        when=JAN_END,
    )
    assert result == {"joint": Number(110, 0)}
    ownership = OwnershipPolicy(
        _revision("sixty-forty"),
        "joint",
        (Share("owner-a", Number(6, 1)), Share("owner-b", Number(4, 1))),
    )
    assert attribute(
        result["joint"], ownership, shared_account="joint", when=JAN_END
    ) == {
        "owner-a": Number(66, 0),
        "owner-b": Number(44, 0),
    }
    with pytest.raises(PolicyError, match="missing-ownership-policy"):
        attribute(result["joint"], None, shared_account="joint", when=JAN_END)


def test_verified_snapshots_feed_ownership_performance_and_alternate_matching(
    verified_root: Path,
) -> None:
    verified = {
        name: _verified_case(verified_root, name)
        for name in ("breadth/consumer-a", "breadth/consumer-b", "pools")
    }
    left, left_feed = verified["breadth/consumer-a"]
    right, right_feed = verified["breadth/consumer-b"]
    population = replace(
        _population(left, right),
        members=(left_feed.member, right_feed.member),
    )
    joint = consolidate_cash(
        [left_feed, right_feed], population, period=JANUARY, when=JAN_END
    )
    assert joint == {"joint": Number(110, 0)}
    shares = OwnershipPolicy(
        _revision("sixty-forty"),
        "joint",
        (Share("owner-a", Number(6, 1)), Share("owner-b", Number(4, 1))),
    )
    assert attribute(joint["joint"], shares, shared_account="joint", when=JAN_END) == {
        "owner-a": Number(66, 0),
        "owner-b": Number(44, 0),
    }
    assert cash_performance(left_feed.tables, _performance(left)) == Ratio(1, 10)

    pool_case, pool_feed = verified["pools"]
    policy = LotPolicy(
        _revision("synthetic-fifo"),
        "synthetic-not-tax-law",
        "owner",
        "proof-v1",
        (_key(pool_case, "L1"), _key(pool_case, "L2")),
        "principal-only",
    )
    for origins, expected in (
        (policy.ordered_origins, Ratio(126, 1)),
        (tuple(reversed(policy.ordered_origins)), Ratio(156, 1)),
    ):
        matches = match_original_lots(
            pool_feed.tables,
            entity=pool_feed.member.entity,
            units=Number(12, 0),
            when=date(2026, 1, 4),
            policy=replace(policy, ordered_origins=origins),
        )
        assert matched_principal(matches) == expected


def test_selected_authority_does_not_waive_comparable_111_conflict(
    cases: dict[str, FixtureCase],
) -> None:
    left, right = cases["breadth/consumer-a"], cases["breadth/consumer-b"]
    tables = deepcopy(right.tables)
    posting = next(
        row
        for row in tables["postings"]
        if row["posting_id"] == right.aliases["joint-credit-cash"]
    )
    posting["amount_coefficient"] = "11"
    conflict = replace(_feed(right), tables=tables)
    with pytest.raises(PolicyError, match="conflicting-owner-copy"):
        consolidate_cash(
            [_feed(left), conflict],
            _population(left, right),
            period=JANUARY,
            when=JAN_END,
        )


def test_equal_local_keys_neither_collide_nor_imply_account_equivalence(
    cases: dict[str, FixtureCase],
) -> None:
    original = _feed(cases["breadth/consumer-a"])
    tables = {
        name: [{**row, "entity_id": "independent"} for row in rows]
        for name, rows in original.tables.items()
    }
    other = Feed(Member("independent", "b" * 64, 1), tables)
    members = require_members((original.member, other.member), (original, other))
    local = cases["breadth/consumer-a"].aliases["A"]
    assert Key("owner-a", local) != Key("independent", local)
    assert set(members) == {"owner-a", "independent"}
    assert members["owner-a"].tables["accounts"][0]["entity_id"] == "owner-a"
    assert members["independent"].tables["accounts"][0]["entity_id"] == "independent"


@pytest.mark.parametrize("fault", ["pin", "missing", "duplicate", "entity", "version"])
def test_population_rejects_unapproved_or_mixed_snapshot_members(
    cases: dict[str, FixtureCase],
    fault: str,
) -> None:
    feed = _feed(cases["breadth/consumer-a"])
    expected, available = [feed.member], [feed]
    if fault == "pin":
        expected = [replace(feed.member, descriptor_digest="b" * 64)]
    elif fault == "missing":
        available = []
    elif fault == "duplicate":
        available.append(feed)
    elif fault == "entity":
        tables = deepcopy(feed.tables)
        tables["accounts"][0]["entity_id"] = "other"
        available = [replace(feed, tables=tables)]
    else:
        expected = [replace(feed.member, schema_version=2)]
    with pytest.raises(PolicyError):
        require_members(expected, available)


def test_authority_intervals_are_half_open_and_require_complete_unique_coverage() -> (
    None
):
    boundary = date(2026, 1, 15)
    assignments = (
        Authority(
            "joint", "owner-a", Interval(JANUARY.start, boundary), DateRole.SETTLED
        ),
        Authority(
            "joint", "owner-b", Interval(boundary, JANUARY.end), DateRole.SETTLED
        ),
    )
    assert (
        check_authority(
            assignments,
            shared_account="joint",
            period=JANUARY,
            basis=DateRole.SETTLED,
        )
        == assignments
    )
    assert not assignments[0].period.contains(boundary)
    assert assignments[1].period.contains(boundary)
    for start, code in [
        (date(2026, 1, 14), "authority-overlap"),
        (date(2026, 1, 16), "authority-gap"),
    ]:
        changed = replace(assignments[1], period=Interval(start, JANUARY.end))
        with pytest.raises(PolicyError, match=code):
            check_authority(
                (assignments[0], changed),
                shared_account="joint",
                period=JANUARY,
                basis=DateRole.SETTLED,
            )
    with pytest.raises(PolicyError, match="authority-gap"):
        check_authority(
            assignments,
            shared_account="joint",
            period=JANUARY,
            basis=DateRole.TRADED,
        )


@pytest.mark.parametrize(
    ("source", "copy", "boundary", "account"),
    [
        ("compound-expensed", "breadth/invested-copy", date(2026, 3, 4), "B"),
        ("pools", "breadth/pools-copy", date(2026, 1, 7), "A"),
        ("reference", "breadth/reference-copy", date(2026, 1, 3), "A"),
    ],
)
def test_invested_authority_handoff_maps_exact_lots_pools_fees_and_references(
    verified_root: Path,
    source: str,
    copy: str,
    boundary: date,
    account: str,
) -> None:
    left, _ = _verified_case(verified_root, source)
    right, _ = _verified_case(verified_root, copy)
    before = gross_state(
        left.tables, _key(left, account), boundary=boundary, basis=DateRole.POSTED
    )
    after = gross_state(
        right.tables, _key(right, account), boundary=boundary, basis=DateRole.POSTED
    )
    correspondence = {_key(left, alias): _key(right, alias) for alias in left.aliases}
    verify_handoff(before, after, correspondence)
    held = [item for position in after.positions for item in position.slices]
    assert held
    if source == "compound-expensed":
        assert held[0].principal == Number(15, 0)
        assert held[0].expensed_fee == Number(3, 1)
        assert held[0].fees[0].amount == Number(3, 1)
        assert held[0].origins[0].principal == Number(100, 0)
    elif source == "pools":
        pool = next(item for item in held if item.identity == _key(right, "P"))
        assert pool.kind == "pool"
        assert pool.units == Number(12, 0)
        assert pool.principal == Number(144, 0)
        assert {origin.lot for origin in pool.origins} == {
            _key(right, "L1"),
            _key(right, "L2"),
        }
    else:
        assert held[0].reference == Number(103, 0)
    with pytest.raises(PolicyError, match="handoff-missing-correspondence"):
        verify_handoff(before, after, {})


@pytest.mark.parametrize(
    "component",
    [
        "units",
        "principal",
        "capitalized_fee",
        "expensed_fee",
        "reference",
        "fees",
        "origins",
    ],
)
def test_equal_market_value_cannot_mask_changed_handoff_components(
    cases: dict[str, FixtureCase],
    component: str,
) -> None:
    case = cases["compound-expensed"]
    state = gross_state(
        case.tables, _key(case, "B"), boundary=date(2026, 3, 4), basis=DateRole.POSTED
    )
    position = next(position for position in state.positions if position.slices)
    item = position.slices[0]
    changed = replace(
        item, **{component: () if component in {"fees", "origins"} else Number(999, 0)}
    )
    after = replace(
        state,
        positions=tuple(
            replace(candidate, slices=(changed,))
            if candidate == position
            else candidate
            for candidate in state.positions
        ),
    )
    correspondence = {_key(case, alias): _key(case, alias) for alias in case.aliases}
    with pytest.raises(PolicyError, match="handoff-gross-state-conflict"):
        verify_handoff(state, after, correspondence)


def test_internal_transfer_twenty_has_zero_external_flow(
    cases: dict[str, FixtureCase],
) -> None:
    case = cases["breadth/consumer-a"]
    when = date(2026, 2, 1)
    postings = {row["posting_id"]: row for row in case.tables["postings"]}
    flows = tuple(
        Flow(
            _key(case, alias),
            Number(
                int(cast("str", postings[case.aliases[alias]]["amount_coefficient"])),
                0,
            ),
            _key(case, "USD"),
            when,
            FlowKind.INTERNAL,
        )
        for alias in ("transfer-out", "transfer-in")
    )
    transfer = Transfer(
        _key(case, "transfer-out"),
        _key(case, "transfer-in"),
        _key(case, "USD"),
        _key(case, "USD"),
        Number(20, 0),
    )
    assert external_flows(flows, (transfer,)) == Number(0, 0)
    assert [flow.amount for flow in flows] == [Number(-20, 0), Number(20, 0)]
    with pytest.raises(PolicyError, match="missing-internal-correspondence"):
        external_flows(flows, ())
    with pytest.raises(PolicyError, match="changed-internal-correspondence"):
        external_flows(flows, (replace(transfer, amount=Number(21, 0)),))


def _valuation(case: FixtureCase) -> ValuationPolicy:
    return ValuationPolicy(
        _revision("valuation"),
        date(2026, 1, 4),
        "Etc/UTC",
        BoundaryConvention.CLOSING,
        PriceBasis.MARK,
        "Invented Exchange",
        1,
        _key(case, "USD"),
        None,
        reject_action_crossing=True,
    )


def test_selected_quote_and_direct_fx_policy_never_substitute_another_basis(
    verified_root: Path,
) -> None:
    case, _ = _verified_case(verified_root, "breadth/quotes")
    policy = _valuation(case)
    mark = select_price(case.tables, _key(case, "F"), policy)
    assert mark == Number(103, 0)
    assert multiply(multiply(Number(2, 0), mark), Number(10, 0)) == Number(2060, 0)
    assert select_price(
        case.tables,
        _key(case, "F"),
        replace(policy, price_basis=PriceBasis.INDEX),
    ) == Number(102, 0)
    converted = replace(
        policy,
        reporting_currency=_key(case, "EUR"),
        fx=FXPolicy("Invented FX", policy.boundary, PriceBasis.MARK),
    )
    assert select_price(case.tables, _key(case, "F"), converted) == Number(515, 1)
    assert multiply(
        Number(20, 0), select_price(case.tables, _key(case, "F"), converted)
    ) == Number(1030, 0)
    reverse = [
        row
        for row in case.tables["prices"]
        if row["commodity_id"] == case.aliases["EUR"]
    ]
    assert reverse[0]["rate_coefficient"] == "19"
    assert reverse[0]["rate_scale"] == 1
    for modified, code in [
        (replace(policy, price_basis=PriceBasis.SETTLEMENT), "missing-price-evidence"),
        (replace(policy, max_age_days=0), "stale-price"),
        (replace(converted, fx=None), "missing-fx-policy"),
        (
            replace(
                converted, fx=FXPolicy("Absent FX", policy.boundary, PriceBasis.MARK)
            ),
            "missing-fx-evidence",
        ),
    ]:
        with pytest.raises(PolicyError, match=code):
            select_price(case.tables, _key(case, "F"), modified)


def _performance(case: FixtureCase) -> PerformancePolicy:
    return PerformancePolicy(
        _revision("simple-start-net"),
        Interval(date(2026, 3, 1), date(2026, 4, 1)),
        (_key(case, "performance"),),
        _key(case, "USD"),
        DateRole.SETTLED,
        (
            Flow(
                _key(case, "performance-contribution-cash"),
                Number(50, 0),
                _key(case, "USD"),
                date(2026, 3, 1),
                FlowKind.CONTRIBUTION,
            ),
            Flow(
                _key(case, "performance-return-cash"),
                Number(15, 0),
                _key(case, "USD"),
                date(2026, 3, 31),
                FlowKind.RETURN,
            ),
        ),
        (),
        "simple",
        "start",
        "net",
    )


def test_performance_uses_explicit_perimeter_flow_timing_and_not_balance_change(
    cases: dict[str, FixtureCase],
) -> None:
    case = cases["breadth/consumer-a"]
    policy = _performance(case)
    assert cash_performance(case.tables, policy) == Ratio(1, 10)
    assert Fraction(165 - 100, 100) == Fraction(65, 100)
    with pytest.raises(PolicyError, match="missing-performance-policy"):
        cash_performance(case.tables, None)
    with pytest.raises(PolicyError, match="incomplete-flow-classification"):
        cash_performance(case.tables, replace(policy, flows=policy.flows[1:]))
    with pytest.raises(PolicyError, match="unsupported-performance-policy"):
        cash_performance(case.tables, replace(policy, flow_timing="intraperiod"))
    contribution = replace(policy.flows[0], when=date(2026, 3, 2))
    with pytest.raises(PolicyError, match="incomplete-flow-classification"):
        cash_performance(
            case.tables, replace(policy, flows=(contribution, policy.flows[1]))
        )


def test_tax_experiment_rematches_original_pool_contributions_without_overwrite(
    cases: dict[str, FixtureCase],
) -> None:
    case = cases["pools"]
    before = deepcopy(case.tables)
    policy = LotPolicy(
        _revision("synthetic-fifo"),
        "synthetic-not-tax-law",
        "owner",
        "proof-v1",
        (_key(case, "L1"), _key(case, "L2")),
        "principal-only",
    )
    matches = match_original_lots(
        case.tables,
        entity=_key(case, "A").entity,
        units=Number(12, 0),
        when=date(2026, 1, 4),
        policy=policy,
    )
    assert [match.units for match in matches] == [Number(10, 0), Number(2, 0)]
    assert matched_principal(matches) == Ratio(126, 1)
    source = next(
        row
        for row in case.tables["inventory_changes"]
        if row["change_id"] == case.aliases["p-c7"]
    )
    assert source["principal_delta_coefficient"] == "-144"
    assert source["lot_id"] is None
    assert source["pool_id"] == case.aliases["P"]
    assert case.tables == before
    assert len(original_acquisitions(case.tables, entity=_key(case, "A").entity)) == 3


def test_unknown_fork_basis_blocks_rematching_not_observation_retention(
    cases: dict[str, FixtureCase],
) -> None:
    case = cases["breadth/onchain"]
    origins = original_acquisitions(case.tables, entity=_key(case, "A").entity)
    assert len(origins) == 2
    assert all(origin.principal is None for origin in origins)
    policy = LotPolicy(
        _revision("synthetic-fork"),
        "synthetic-not-tax-law",
        "owner",
        "proof-v1",
        (_key(case, "FKL"),),
        "principal-only",
    )
    with pytest.raises(PolicyError, match="unavailable-original-basis"):
        match_original_lots(
            case.tables,
            entity=_key(case, "A").entity,
            units=Number(1, 0),
            when=date(2026, 1, 31),
            policy=policy,
        )
