"""Bounded downstream policy proofs over independently replayed records.

This is not a declaration-file parser, tax engine, or general report model.
Callers provide reviewed typed declarations; artifact verification belongs
to ``reader.read_snapshot``.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from math import gcd
from re import fullmatch
from typing import TYPE_CHECKING

from tests.integration.conformance.consumer_arithmetic import (
    Number,
    Ratio,
    add,
    divide,
    multiply,
    read_number,
)
from tests.integration.conformance.reader import (
    effective_posting_date,
    project_quantities,
    replay_rows,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from fractions import Fraction

    from tests.integration.conformance.reader import Row, Tables


class PolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:  # noqa: FBT001
    if not condition:
        raise PolicyError(code)


@dataclass(frozen=True, order=True)
class Key:
    entity: str
    local: str

    def __post_init__(self) -> None:
        _require(bool(self.entity) and bool(self.local), "qualified-identity")


@dataclass(frozen=True)
class Interval:
    start: date
    end: date

    def __post_init__(self) -> None:
        _require(self.start < self.end, "invalid-interval")

    def contains(self, when: date) -> bool:
        return self.start <= when < self.end


@dataclass(frozen=True)
class Revision:
    declaration_id: str
    digest: str
    schema_version: int
    effective: Interval

    def check(self, when: date) -> None:
        _require(self.schema_version == 1, "policy-version")
        _require(
            bool(self.declaration_id)
            and fullmatch("[0-9a-f]{64}", self.digest) is not None,
            "policy-revision",
        )
        _require(self.effective.contains(when), "policy-not-effective")


class DateRole(StrEnum):
    POSTED = "posted"
    SETTLED = "settled"
    TRADED = "traded"


@dataclass(frozen=True)
class Member:
    entity: str
    descriptor_digest: str
    schema_version: int


@dataclass(frozen=True)
class Feed:
    """Rows and descriptor identity supplied by the independent artifact reader."""

    member: Member
    tables: Tables


def require_members(
    expected: Sequence[Member], available: Sequence[Feed]
) -> dict[str, Feed]:
    _require(bool(expected), "missing-snapshot-members")
    wanted = {member.entity: member for member in expected}
    actual = {feed.member.entity: feed for feed in available}
    _require(len(wanted) == len(expected), "duplicate-snapshot-member")
    _require(len(actual) == len(available), "duplicate-snapshot-member")
    _require(set(wanted) == set(actual), "missing-or-unapproved-snapshot-member")
    _require({member.schema_version for member in expected} == {1}, "snapshot-version")
    for entity, member in wanted.items():
        _require(actual[entity].member == member, "snapshot-pin-mismatch")
        _require(
            fullmatch("[0-9a-f]{64}", member.descriptor_digest) is not None,
            "snapshot-pin-mismatch",
        )
        builds = actual[entity].tables["build"]
        _require(
            len(builds) == 1
            and builds[0]["entity_id"] == entity
            and builds[0]["schema_version"] == member.schema_version,
            "snapshot-member-metadata",
        )
        _require(
            all(
                row["entity_id"] == entity
                for rows in actual[entity].tables.values()
                for row in rows
            ),
            "snapshot-entity-leak",
        )
    return actual


def _number(row: Row, name: str) -> Number | None:
    return read_number(
        row[name + "_coefficient"], row[name + "_scale"], row[name + "_source_scale"]
    )


def _required_number(row: Row, name: str) -> Number:
    value = _number(row, name)
    _require(value is not None, "unknown-financial-value")
    assert value is not None
    return value


def _finite(value: Fraction | Ratio) -> Number:
    ratio = Ratio(value.numerator, value.denominator)
    denominator = ratio.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    _require(denominator == 1, "nonterminating-decimal")
    scale = max(twos, fives)
    _require(scale <= 152, "intermediate-scale")
    factor = 2 ** (scale - twos) * 5 ** (scale - fives)
    return multiply(Number(ratio.numerator, 0), Number(factor, scale))


def _rows(tables: Tables, table: str, entity: str) -> list[Row]:
    return [row for row in tables[table] if row["entity_id"] == entity]


def _one(tables: Tables, table: str, field: str, key: Key) -> Row:
    matches = [
        row for row in _rows(tables, table, key.entity) if row[field] == key.local
    ]
    _require(len(matches) == 1, "unresolved-qualified-identity")
    return matches[0]


@dataclass(frozen=True)
class AccountBinding:
    account: Key
    shared_account: str
    currency: Key


@dataclass(frozen=True)
class Authority:
    shared_account: str
    entity: str
    period: Interval
    basis: DateRole


def check_authority(
    assignments: Sequence[Authority],
    *,
    shared_account: str,
    period: Interval,
    basis: DateRole,
) -> tuple[Authority, ...]:
    selected = sorted(
        (
            authority
            for authority in assignments
            if authority.shared_account == shared_account
            and authority.basis is basis
            and authority.period.start < period.end
            and authority.period.end > period.start
        ),
        key=lambda authority: authority.period.start,
    )
    cursor = period.start
    for authority in selected:
        _require(
            authority.period.start <= cursor < authority.period.end, "authority-gap"
        )
        _require(
            authority is selected[0] or authority.period.start == cursor,
            "authority-overlap",
        )
        cursor = min(authority.period.end, period.end)
    _require(cursor == period.end and bool(selected), "authority-gap")
    return tuple(selected)


@dataclass(frozen=True)
class PopulationPolicy:
    revision: Revision
    members: tuple[Member, ...]
    bindings: tuple[AccountBinding, ...]
    authorities: tuple[Authority, ...]
    basis: DateRole


def cash_balance(
    tables: Tables, binding: AccountBinding, *, when: date, basis: DateRole
) -> Number:
    _one(tables, "accounts", "account_id", binding.account)
    positions = [
        row
        for row in _rows(tables, "positions", binding.account.entity)
        if row["account_id"] == binding.account.local
    ]
    quantities = project_quantities(
        tables, entity=binding.account.entity, date_role=basis.value, cutoff=when
    )
    active = [row for row in positions if quantities.get(row["position_id"], 0) != 0]
    _require(
        all(
            row["commodity_id"] == binding.currency.local
            and binding.currency.entity == binding.account.entity
            and row["measurement_kind"] == "quantity"
            for row in active
        ),
        "cash-question-requires-valuation",
    )
    return add([_finite(quantities[row["position_id"]]) for row in active])


def consolidate_cash(
    feeds: Sequence[Feed],
    policy: PopulationPolicy | None,
    *,
    period: Interval,
    when: date,
) -> dict[str, Number]:
    _require(policy is not None, "missing-population-policy")
    assert policy is not None
    policy.revision.check(when)
    _require(period.contains(when), "report-boundary")
    members = require_members(policy.members, feeds)
    _require(
        len({binding.account for binding in policy.bindings}) == len(policy.bindings),
        "duplicate-account-mapping",
    )
    totals = {}
    for shared in sorted({binding.shared_account for binding in policy.bindings}):
        assignments = check_authority(
            policy.authorities, shared_account=shared, period=period, basis=policy.basis
        )
        _require(
            len({assignment.entity for assignment in assignments}) == 1,
            "authority-handoff-required",
        )
        bindings = [
            binding for binding in policy.bindings if binding.shared_account == shared
        ]
        _require(
            all(binding.account.entity in members for binding in bindings),
            "unresolved-member",
        )
        selected = next(
            assignment.entity
            for assignment in assignments
            if assignment.period.contains(when)
        )
        authoritative = [
            binding for binding in bindings if binding.account.entity == selected
        ]
        _require(len(authoritative) == 1, "unresolved-authoritative-account")
        balances = [
            cash_balance(
                members[binding.account.entity].tables,
                binding,
                when=when,
                basis=policy.basis,
            )
            for binding in bindings
        ]
        _require(len(set(balances)) == 1, "conflicting-owner-copy")
        totals[shared] = balances[0]
    return totals


@dataclass(frozen=True)
class Share:
    beneficiary: str
    amount: Number


@dataclass(frozen=True)
class OwnershipPolicy:
    revision: Revision
    shared_account: str
    shares: tuple[Share, ...]


def attribute(
    total: Number, policy: OwnershipPolicy | None, *, shared_account: str, when: date
) -> dict[str, Number]:
    _require(policy is not None, "missing-ownership-policy")
    assert policy is not None
    policy.revision.check(when)
    _require(policy.shared_account == shared_account, "ownership-scope")
    _require(
        len({share.beneficiary for share in policy.shares}) == len(policy.shares)
        and all(share.amount.coefficient >= 0 for share in policy.shares)
        and add([share.amount for share in policy.shares]) == Number(1, 0),
        "ownership-shares",
    )
    return {share.beneficiary: multiply(total, share.amount) for share in policy.shares}


@dataclass(frozen=True)
class Origin:
    lot: Key
    posting: Key
    commodity: Key
    book_commodity: Key | None
    acquired: date
    units: Number
    principal: Number | None
    capitalized_fee: Number | None
    expensed_fee: Number | None


def original_acquisitions(tables: Tables, *, entity: str) -> tuple[Origin, ...]:
    allocations = {
        row["allocation_id"]
        for row in _rows(tables, "inventory_allocations", entity)
        if row["allocation_kind"] in {"acquisition", "opening"}
    }
    origins = []
    for change in _rows(tables, "inventory_changes", entity):
        if change["allocation_id"] not in allocations or change["lot_id"] is None:
            continue
        lot = _one(tables, "lots", "lot_id", Key(entity, change["lot_id"]))
        _require(lot["acquisition_date"] is not None, "unavailable-acquisition-date")
        origins.append(
            Origin(
                Key(entity, lot["lot_id"]),
                Key(entity, lot["origin_posting_id"]),
                Key(entity, lot["commodity_id"]),
                Key(entity, lot["book_commodity_id"])
                if lot["book_commodity_id"] is not None
                else None,
                date.fromisoformat(str(lot["acquisition_date"])),
                _required_number(change, "units_delta"),
                _number(change, "principal_delta"),
                _number(change, "capitalized_fee_delta"),
                _number(change, "expensed_fee_delta"),
            )
        )
    _require(
        len({origin.lot for origin in origins}) == len(origins),
        "ambiguous-original-acquisition",
    )
    return tuple(sorted(origins, key=lambda origin: origin.lot))


@dataclass(frozen=True)
class FeeState:
    identity: Key
    commodity: Key
    book_commodity: Key | None
    treatment: str
    amount: Number
    book_amount: Number | None


@dataclass(frozen=True)
class SliceState:
    identity: Key
    kind: str
    units: Number
    principal: Number | None
    capitalized_fee: Number | None
    expensed_fee: Number | None
    reference: Number | None
    fees: tuple[FeeState, ...]
    origins: tuple[Origin, ...]


@dataclass(frozen=True)
class PositionState:
    identity: Key
    commodity: Key
    counterparty: Key | None
    terms: Key | None
    kind: str
    measurement: str
    units: Number
    slices: tuple[SliceState, ...]


@dataclass(frozen=True)
class GrossState:
    account: Key
    boundary: date
    positions: tuple[PositionState, ...]


def _ancestors(tables: Tables, *, entity: str, target: str) -> set[str]:
    parents: dict[str, set[str]] = {}
    changes = _rows(tables, "inventory_changes", entity)
    for allocation in _rows(tables, "inventory_allocations", entity):
        members = [
            row
            for row in changes
            if row["allocation_id"] == allocation["allocation_id"]
        ]
        sources = {
            row["lot_id"] or row["pool_id"]
            for row in members
            if row["change_role"] == "source"
        }
        for row in members:
            if row["change_role"] == "target":
                parents.setdefault(row["lot_id"] or row["pool_id"], set()).update(
                    sources
                )
    for lot in _rows(tables, "lots", entity):
        if lot["parent_lot_id"] is not None:
            parents.setdefault(lot["lot_id"], set()).add(lot["parent_lot_id"])
    found: set[str] = set()
    pending = [target]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(parents.get(current, set()) - found)
    return found


def gross_state(
    tables: Tables, account: Key, *, boundary: date, basis: DateRole
) -> GrossState:
    _one(tables, "accounts", "account_id", account)
    replay = replay_rows(tables, entity=account.entity, effective_date=boundary)
    quantities = project_quantities(
        tables, entity=account.entity, date_role=basis.value, cutoff=boundary
    )
    origins = original_acquisitions(tables, entity=account.entity)
    positions = []
    for position in _rows(tables, "positions", account.entity):
        if position["account_id"] != account.local:
            continue
        identity = Key(account.entity, position["position_id"])
        slices = []
        for key, state in replay.inventory.items():
            if key[1] != identity.local or state.units == 0:
                continue
            lineage = _ancestors(tables, entity=account.entity, target=key[3])
            fees = []
            for fee_id, amount in sorted(state.fees.items()):
                fee = _one(
                    tables,
                    "fee_allocations",
                    "fee_allocation_id",
                    Key(account.entity, fee_id),
                )
                book_amount = state.book_fees[fee_id]
                fees.append(
                    FeeState(
                        Key(account.entity, fee_id),
                        Key(account.entity, fee["commodity_id"]),
                        Key(account.entity, fee["book_commodity_id"])
                        if fee["book_commodity_id"]
                        else None,
                        fee["treatment"],
                        _finite(amount),
                        _finite(book_amount) if book_amount is not None else None,
                    )
                )
            reference = replay.references.get((account.entity, identity.local, key[3]))
            slices.append(
                SliceState(
                    Key(account.entity, key[3]),
                    key[2],
                    _finite(state.units),
                    _finite(state.principal) if state.principal is not None else None,
                    _finite(state.capitalized_fee)
                    if state.capitalized_fee is not None
                    else None,
                    _finite(state.expensed_fee)
                    if state.expensed_fee is not None
                    else None,
                    _finite(reference) if reference is not None else None,
                    tuple(fees),
                    tuple(origin for origin in origins if origin.lot.local in lineage),
                )
            )
        units = quantities.get(identity.local)
        if units is None and not slices:
            continue
        _require(units is not None, "unavailable-position-quantity")
        assert units is not None
        if slices:
            _require(
                add([item.units for item in slices]) == _finite(units),
                "handoff-incompatible-date-basis",
            )
        positions.append(
            PositionState(
                identity,
                Key(account.entity, position["commodity_id"]),
                Key(account.entity, position["counterparty_id"])
                if position["counterparty_id"]
                else None,
                Key(account.entity, position["terms_declaration_id"])
                if position["terms_declaration_id"]
                else None,
                position["position_kind"],
                position["measurement_kind"],
                _finite(units),
                tuple(sorted(slices, key=lambda item: item.identity)),
            )
        )
    return GrossState(
        account, boundary, tuple(sorted(positions, key=lambda item: item.identity))
    )


def verify_handoff(
    before: GrossState, after: GrossState, correspondence: Mapping[Key, Key]
) -> None:
    """Compare full replayed state after an explicit bijective identity mapping."""
    from dataclasses import fields, is_dataclass  # noqa: PLC0415

    _require(before.boundary == after.boundary, "handoff-boundary")
    _require(
        len(set(correspondence.values())) == len(correspondence), "handoff-nonbijective"
    )

    def remap(value: object) -> object:
        if isinstance(value, Key):
            _require(value in correspondence, "handoff-missing-correspondence")
            return correspondence[value]
        if isinstance(value, tuple):
            return frozenset(remap(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value),
                tuple(remap(getattr(value, item.name)) for item in fields(value)),
            )
        return value

    def identities(value: object) -> object:
        if isinstance(value, Key):
            return value
        if isinstance(value, tuple):
            return frozenset(identities(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value),
                tuple(identities(getattr(value, item.name)) for item in fields(value)),
            )
        return value

    _require(remap(before) == identities(after), "handoff-gross-state-conflict")


class PriceBasis(StrEnum):
    MARK = "mark"
    INDEX = "index"
    TRADE = "trade"
    SETTLEMENT = "settlement"


class BoundaryConvention(StrEnum):
    OPENING = "opening"
    CLOSING = "closing"


@dataclass(frozen=True)
class FXPolicy:
    source_market: str
    conversion_date: date
    basis: PriceBasis


@dataclass(frozen=True)
class ValuationPolicy:
    revision: Revision
    boundary: date
    civil_zone: str
    convention: BoundaryConvention
    price_basis: PriceBasis
    market: str
    max_age_days: int
    reporting_currency: Key
    fx: FXPolicy | None
    reject_action_crossing: bool


def select_price(
    tables: Tables, commodity: Key, policy: ValuationPolicy | None
) -> Number:
    _require(policy is not None, "missing-valuation-policy")
    assert policy is not None
    policy.revision.check(policy.boundary)
    _require(
        policy.civil_zone == "Etc/UTC"
        and policy.max_age_days >= 0
        and policy.reject_action_crossing,
        "unsupported-valuation-convention",
    )
    quotes = [
        row
        for row in _rows(tables, "prices", commodity.entity)
        if row["commodity_id"] == commodity.local
        and row["basis"] == policy.price_basis.value
        and row["market"] == policy.market
        and (
            date.fromisoformat(str(row["date"])) < policy.boundary
            or (
                policy.convention is BoundaryConvention.CLOSING
                and date.fromisoformat(str(row["date"])) == policy.boundary
            )
        )
    ]
    _require(bool(quotes), "missing-price-evidence")
    latest = max(date.fromisoformat(str(row["date"])) for row in quotes)
    quotes = [row for row in quotes if date.fromisoformat(str(row["date"])) == latest]
    _require((policy.boundary - latest).days <= policy.max_age_days, "stale-price")
    _require(
        not any(
            latest < date.fromisoformat(str(row["effective_date"])) <= policy.boundary
            for row in _rows(tables, "corporate_actions", commodity.entity)
            if row["commodity_id"] == commodity.local
        ),
        "price-crosses-action",
    )
    values = {
        (_required_number(row, "rate"), row["quote_commodity_id"]) for row in quotes
    }
    _require(len(values) == 1, "conflicting-price-evidence")
    rate, currency = next(iter(values))
    if Key(commodity.entity, currency) == policy.reporting_currency:
        return rate
    _require(policy.fx is not None, "missing-fx-policy")
    assert policy.fx is not None
    _require(policy.fx.conversion_date == policy.boundary, "unsupported-fx-date")
    conversions = [
        row
        for row in _rows(tables, "prices", commodity.entity)
        if row["commodity_id"] == currency
        and Key(commodity.entity, row["quote_commodity_id"])
        == policy.reporting_currency
        and row["market"] == policy.fx.source_market
        and row["basis"] == policy.fx.basis.value
        and date.fromisoformat(str(row["date"])) == policy.fx.conversion_date
    ]
    _require(bool(conversions), "missing-fx-evidence")
    rates = {_required_number(row, "rate") for row in conversions}
    _require(len(rates) == 1, "conflicting-fx-evidence")
    return multiply(rate, next(iter(rates)))


class FlowKind(StrEnum):
    CONTRIBUTION = "contribution"
    WITHDRAWAL = "withdrawal"
    INTERNAL = "internal"
    RETURN = "return"
    FEE = "fee"


@dataclass(frozen=True)
class Flow:
    posting: Key
    amount: Number
    commodity: Key
    when: date
    kind: FlowKind


@dataclass(frozen=True)
class Transfer:
    outgoing: Key
    incoming: Key
    currency_out: Key
    currency_in: Key
    amount: Number


def external_flows(flows: Sequence[Flow], transfers: Sequence[Transfer]) -> Number:
    indexed = {flow.posting: flow for flow in flows}
    _require(len(indexed) == len(flows), "duplicate-flow")
    internal = {flow.posting for flow in flows if flow.kind is FlowKind.INTERNAL}
    consumed: set[Key] = set()
    for transfer in transfers:
        _require(
            transfer.outgoing in internal
            and transfer.incoming in internal
            and transfer.outgoing not in consumed
            and transfer.incoming not in consumed,
            "unresolved-internal-correspondence",
        )
        outgoing, incoming = indexed[transfer.outgoing], indexed[transfer.incoming]
        _require(
            outgoing.amount
            == Number(-transfer.amount.coefficient, transfer.amount.scale)
            and incoming.amount == transfer.amount
            and transfer.amount.coefficient > 0
            and outgoing.commodity == transfer.currency_out
            and incoming.commodity == transfer.currency_in
            and outgoing.when == incoming.when,
            "changed-internal-correspondence",
        )
        consumed.update((transfer.outgoing, transfer.incoming))
    _require(consumed == internal, "missing-internal-correspondence")
    return add(
        [
            flow.amount
            for flow in flows
            if flow.kind in {FlowKind.CONTRIBUTION, FlowKind.WITHDRAWAL}
        ]
    )


@dataclass(frozen=True)
class PerformancePolicy:
    revision: Revision
    period: Interval
    accounts: tuple[Key, ...]
    currency: Key
    basis: DateRole
    flows: tuple[Flow, ...]
    transfers: tuple[Transfer, ...]
    method: str
    flow_timing: str
    fee_treatment: str


def cash_performance(tables: Tables, policy: PerformancePolicy | None) -> Ratio:
    _require(policy is not None, "missing-performance-policy")
    assert policy is not None
    policy.revision.check(policy.period.start)
    _require(
        policy.method == "simple"
        and policy.flow_timing == "start"
        and policy.fee_treatment == "net",
        "unsupported-performance-policy",
    )
    _require(
        bool(policy.accounts)
        and {key.entity for key in policy.accounts} == {policy.currency.entity},
        "performance-perimeter",
    )
    entity = policy.currency.entity
    positions = [
        row
        for row in _rows(tables, "positions", entity)
        if Key(entity, row["account_id"]) in policy.accounts
    ]
    eligible = {
        row["position_id"]
        for row in positions
        if row["commodity_id"] == policy.currency.local
    }
    _require(len(eligible) == len(positions), "performance-requires-valuation")
    opening = project_quantities(
        tables,
        entity=entity,
        date_role=policy.basis.value,
        cutoff=policy.period.start,
        opening=True,
    )
    closing = project_quantities(
        tables,
        entity=entity,
        date_role=policy.basis.value,
        cutoff=policy.period.end,
        opening=True,
    )
    observed = {}
    for posting in _rows(tables, "postings", entity):
        if posting["position_id"] not in eligible:
            continue
        txn = _one(tables, "transactions", "txn_id", Key(entity, posting["txn_id"]))
        if txn["event_state"] != "recognized":
            continue
        when = effective_posting_date(posting, txn, role=policy.basis.value)
        _require(when is not None, "unknown-flow-date")
        assert when is not None
        if policy.period.contains(when):
            observed[Key(entity, posting["posting_id"])] = (
                _required_number(posting, "amount"),
                when,
            )
    declared = {flow.posting: (flow.amount, flow.when) for flow in policy.flows}
    _require(
        observed == declared and len(declared) == len(policy.flows),
        "incomplete-flow-classification",
    )
    _require(
        all(flow.commodity == policy.currency for flow in policy.flows), "flow-currency"
    )
    _require(
        all(
            flow.when == policy.period.start
            for flow in policy.flows
            if flow.kind in {FlowKind.CONTRIBUTION, FlowKind.WITHDRAWAL}
        ),
        "unsupported-flow-timing",
    )
    external = external_flows(policy.flows, policy.transfers)
    start = add([_finite(value) for key, value in opening.items() if key in eligible])
    end = add([_finite(value) for key, value in closing.items() if key in eligible])
    funded = add([start, external])
    _require(funded.coefficient > 0, "nonpositive-performance-base")
    gain = add([end, Number(-funded.coefficient, funded.scale)])
    return divide(gain, funded)


@dataclass(frozen=True)
class LotPolicy:
    revision: Revision
    jurisdiction: str
    taxpayer: str
    statutory_version: str
    ordered_origins: tuple[Key, ...]
    fee_treatment: str


@dataclass(frozen=True)
class LotMatch:
    origin: Origin
    units: Number
    principal: Ratio


def match_original_lots(
    tables: Tables, *, entity: str, units: Number, when: date, policy: LotPolicy | None
) -> tuple[LotMatch, ...]:
    _require(policy is not None, "missing-lot-policy")
    assert policy is not None
    policy.revision.check(when)
    _require(
        bool(policy.jurisdiction and policy.taxpayer and policy.statutory_version),
        "incomplete-lot-policy",
    )
    _require(policy.fee_treatment == "principal-only", "unsupported-lot-fee-policy")
    originals = {
        origin.lot: origin for origin in original_acquisitions(tables, entity=entity)
    }
    _require(
        len(set(policy.ordered_origins)) == len(policy.ordered_origins),
        "duplicate-lot-election",
    )
    _require(
        units.coefficient > 0
        and all(key in originals for key in policy.ordered_origins),
        "unresolved-lot-election",
    )
    selected = [originals[key] for key in policy.ordered_origins]
    _require(
        len({(origin.commodity, origin.book_commodity) for origin in selected}) == 1
        and all(origin.units.coefficient > 0 for origin in selected),
        "incompatible-original-lots",
    )
    remaining = units
    matches = []
    for key in policy.ordered_origins:
        origin = originals[key]
        _require(
            origin.acquired <= when and origin.principal is not None,
            "unavailable-original-basis",
        )
        assert origin.principal is not None
        difference = add(
            [remaining, Number(-origin.units.coefficient, origin.units.scale)]
        )
        chosen = origin.units if difference.coefficient >= 0 else remaining
        matches.append(
            LotMatch(
                origin, chosen, divide(multiply(origin.principal, chosen), origin.units)
            )
        )
        remaining = add([remaining, Number(-chosen.coefficient, chosen.scale)])
        if remaining.coefficient == 0:
            return tuple(matches)
    message = "insufficient-original-units"
    raise PolicyError(message)


def matched_principal(matches: Sequence[LotMatch]) -> Ratio:
    result = Ratio(0, 1)
    for match in matches:
        common = gcd(result.denominator, match.principal.denominator)
        left = multiply(
            Number(result.numerator, 0),
            Number(match.principal.denominator // common, 0),
        )
        right = multiply(
            Number(match.principal.numerator, 0),
            Number(result.denominator // common, 0),
        )
        denominator = multiply(
            Number(result.denominator, 0),
            Number(match.principal.denominator // common, 0),
        )
        result = Ratio(add([left, right]).coefficient, denominator.coefficient)
    return result
