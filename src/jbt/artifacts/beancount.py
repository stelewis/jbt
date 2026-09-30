"""Scoped, exact Beancount projection; never a booking implementation.

Input is an already validated table record set with ISO date strings,
not Parquet. Only individual cost and quantity positions are supported,
with one book step per event. Explicit split and merger allocations can
replace a lot's unit cost without recalculating an action ratio. Unknown
basis, pooled/reference inventory, nonterminating unit costs, negative
posting prices, intraday/complete assertions, and differing effective
posting dates fail.
Numbers and conservative accumulation bounds must fit 28 decimal digits.
No tolerance, padding, inferred amount, or implicit lot selection repairs
an unrepresentable projection. Beancount is a test-only dependency.
"""

# Structured exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from typing import Literal

from jbt.domain.errors import ArithmeticDomainError, NumericLimitError
from jbt.domain.numbers import BoundedRational, ExactDecimal

type Row = Mapping[str, object]
type Tables = Mapping[str, Sequence[Row]]
type DateRole = Literal["authorized", "traded", "posted", "settled", "value"]

_ACCOUNT = re.compile(
    r"(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9-]*)+"
)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9'._-]*[A-Z0-9]\Z|[A-Z]\Z")
_PRECISION = 28


class BeancountProjectionError(ValueError):
    """A named sink capability failure, never a model correction."""

    def __init__(self, code: str, record_id: str) -> None:
        """Retain a machine-readable reason and the affected identity."""
        self.code = code
        self.record_id = record_id
        super().__init__(f"{code}: {record_id}")


@dataclass(frozen=True)
class BeancountPolicy:
    """Explicit sink names and one date basis; no clearing is inferred.

    Distinct positions/categories must have distinct paths. Source symbols
    never identify commodities; omitted symbols use entity-qualified IDs.
    """

    entity_id: str
    date_role: DateRole
    position_accounts: Mapping[str, str]
    category_accounts: Mapping[str, str]
    commodity_symbols: Mapping[str, str]
    position_lifetimes: Mapping[str, DirectiveLifetime]
    category_lifetimes: Mapping[str, DirectiveLifetime]
    commodity_lifetimes: Mapping[str, DirectiveLifetime]
    note_accounts: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectiveLifetime:
    """A declaration-supplied directive date, never a first-use estimate."""

    declaration_id: str
    opened: date
    closed: date | None = None

    def __post_init__(self) -> None:
        """Reject invalid dates before rendering a lifecycle directive."""
        if (
            not self.declaration_id
            or type(self.opened) is not date
            or (self.closed is not None and type(self.closed) is not date)
            or (self.closed is not None and self.closed < self.opened)
        ):
            raise BeancountProjectionError(
                "invalid_directive_lifetime", self.declaration_id
            )


def _string(row: Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise BeancountProjectionError("string_required", key)
    return value


def _date(row: Row, key: str) -> date:
    value = row.get(key)
    if not isinstance(value, str):
        raise BeancountProjectionError("known_date_required", key)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise BeancountProjectionError("iso_date_required", key) from error


def _number(row: Row, key: str) -> BoundedRational:
    coefficient = row.get(f"{key}_coefficient")
    scale = row.get(f"{key}_scale")
    source_scale = row.get(f"{key}_source_scale")
    if not isinstance(coefficient, str) or type(scale) is not int:
        raise BeancountProjectionError("known_number_required", key)
    if source_scale is not None and type(source_scale) is not int:
        raise BeancountProjectionError("source_scale_required", key)
    number = ExactDecimal(coefficient, scale, source_scale)
    return number.rational()


def _decimal(value: BoundedRational, record_id: str) -> str:
    try:
        number = value.exact_decimal()
    except ArithmeticDomainError as error:
        raise BeancountProjectionError("nonterminating_unit_cost", record_id) from error
    except NumericLimitError as error:
        raise BeancountProjectionError("unit_value_storage_limit", record_id) from error
    scale = number.scale
    digits = number.coefficient.removeprefix("-")
    if len(digits) > _PRECISION:
        raise BeancountProjectionError("beancount_decimal_precision", record_id)
    digits = digits.zfill(scale + 1)
    text = digits if not scale else f"{digits[:-scale]}.{digits[-scale:]}"
    return ("-" if number.coefficient.startswith("-") else "") + text


def _absolute(value: BoundedRational) -> BoundedRational:
    return BoundedRational(abs(value.numerator), value.denominator, value.scale)


def _equal(left: BoundedRational, right: BoundedRational) -> bool:
    return left.subtract(right).numerator == 0


def _sum(values: Sequence[BoundedRational]) -> BoundedRational:
    total = BoundedRational(0, 1, 0)
    for value in values:
        total = total.add(value)
    return total


def _quoted(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metadata(row: Row, keys: Sequence[str], indent: str = "  ") -> list[str]:
    lines = []
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            value = _quoted(value)
        lines.append(f"{indent}jbt_{key}: {_quoted(value)}")
    return lines


class _Projection:
    def __init__(self, tables: Tables, policy: BeancountPolicy) -> None:
        self.policy = policy
        self.tables = {
            name: tuple(row for row in rows if row["entity_id"] == policy.entity_id)
            for name, rows in tables.items()
        }
        self.positions = self.index("positions", "position_id")
        self.lots = self.index("lots", "lot_id")
        self.allocations = self.index("inventory_allocations", "allocation_id")
        self.lot_units: dict[tuple[str, str], BoundedRational] = {}
        self.lot_costs: dict[tuple[str, str], BoundedRational] = {}
        self.accounts = dict(policy.position_accounts)
        self.categories = dict(policy.category_accounts)
        self.symbols = {
            _string(row, "commodity_id"): policy.commodity_symbols.get(
                _string(row, "commodity_id"),
                "J"
                + sha256(_quoted([policy.entity_id, row["commodity_id"]]).encode())
                .hexdigest()
                .upper(),
            )
            for row in self.rows("commodities")
        }
        self._validate_names()

    def rows(self, name: str) -> tuple[Row, ...]:
        return self.tables.get(name, ())

    def index(self, name: str, key: str) -> dict[str, Row]:
        return {_string(row, key): row for row in self.rows(name)}

    def related(self, name: str, key: str, value: object) -> tuple[Row, ...]:
        return tuple(row for row in self.rows(name) if row[key] == value)

    def _validate_names(self) -> None:
        if self.policy.date_role not in {
            "authorized",
            "traded",
            "posted",
            "settled",
            "value",
        }:
            raise BeancountProjectionError("date_role", self.policy.entity_id)
        paths = [*self.accounts.values(), *self.categories.values()]
        if any(not _ACCOUNT.fullmatch(path) for path in paths):
            raise BeancountProjectionError(
                "invalid_account_path", self.policy.entity_id
            )
        if len(set(paths)) != len(paths):
            raise BeancountProjectionError(
                "account_path_collision", self.policy.entity_id
            )
        if any(not _SYMBOL.fullmatch(symbol) for symbol in self.symbols.values()):
            raise BeancountProjectionError("invalid_symbol", self.policy.entity_id)
        if len(set(self.symbols.values())) != len(self.symbols):
            raise BeancountProjectionError("symbol_collision", self.policy.entity_id)
        for category in self.rows("categories"):
            category_id = _string(category, "category_id")
            root = {"income": "Income", "expense": "Expenses", "equity": "Equity"}[
                _string(category, "category_type")
            ]
            if category_id in self.categories and not self.categories[
                category_id
            ].startswith(root + ":"):
                raise BeancountProjectionError("category_root_mismatch", category_id)

    def account(self, posting: Row) -> str:
        key = "position_id" if posting["leg_kind"] == "position" else "category_id"
        names = self.accounts if key == "position_id" else self.categories
        identity = posting.get(key)
        if not isinstance(identity, str) or identity not in names:
            raise BeancountProjectionError(
                "unmapped_posting", _string(posting, "posting_id")
            )
        return names[identity]

    def effective_date(self, transaction: Row, posting: Row) -> date:
        field = f"date_{self.policy.date_role}"
        mode = posting[f"{field}_mode"]
        if mode == "inherit":
            return _date(transaction, field)
        if mode == "value":
            return _date(posting, field)
        raise BeancountProjectionError(
            "unknown_posting_date", _string(posting, "posting_id")
        )

    def lifecycle_directives(self) -> list[tuple[date, str, list[str]]]:
        directives = []
        declarations = self.index("declarations", "declaration_id")
        for kind, names, lifetimes in (
            ("position", self.accounts, self.policy.position_lifetimes),
            ("category", self.categories, self.policy.category_lifetimes),
            ("commodity", self.symbols, self.policy.commodity_lifetimes),
        ):
            if set(names) != set(lifetimes):
                raise BeancountProjectionError("directive_lifetime_required", kind)
            for identity, name in names.items():
                lifetime = lifetimes[identity]
                if lifetime.declaration_id not in declarations:
                    raise BeancountProjectionError(
                        "directive_declaration_required", identity
                    )
                metadata = _metadata(
                    {"declaration_id": lifetime.declaration_id, f"{kind}_id": identity},
                    ("declaration_id", f"{kind}_id"),
                )
                directive = (
                    f"{lifetime.opened} commodity {name}"
                    if kind == "commodity"
                    else f'{lifetime.opened} open {name} "STRICT"'
                )
                directives.append(
                    (lifetime.opened, f"open:{kind}:{identity}", [directive, *metadata])
                )
                if lifetime.closed is not None:
                    if kind == "commodity":
                        raise BeancountProjectionError(
                            "commodity_close_unsupported", identity
                        )
                    directives.append(
                        (
                            lifetime.closed,
                            f"close:{kind}:{identity}",
                            [f"{lifetime.closed} close {name}", *metadata],
                        )
                    )
        return directives

    def note(self, transaction: Row) -> tuple[date, str, list[str]]:
        identity = _string(transaction, "txn_id")
        account = self.policy.note_accounts.get(identity)
        if account not in {*self.accounts.values(), *self.categories.values()}:
            raise BeancountProjectionError("note_account_required", identity)
        when = _date(transaction, f"date_{self.policy.date_role}")
        for kind, names in (("position", self.accounts), ("category", self.categories)):
            for target, path in names.items():
                if path == account:
                    self.check_lifetime(kind, target, when)
        return (
            when,
            identity,
            [
                f"{when} note {account} {_quoted(transaction.get('narration') or '')}",
                *_metadata(transaction, ("entity_id", "txn_id", "event_kind")),
                *self.evidence("transactions", identity),
                *self.link_metadata(identity),
                *self.action_metadata(identity, when),
            ],
        )

    def check_lifetime(self, kind: str, identity: str, when: date) -> None:
        lifetimes = {
            "position": self.policy.position_lifetimes,
            "category": self.policy.category_lifetimes,
            "commodity": self.policy.commodity_lifetimes,
        }[kind]
        if identity not in lifetimes:
            raise BeancountProjectionError("directive_lifetime_required", identity)
        lifetime = lifetimes[identity]
        if when < lifetime.opened or (
            lifetime.closed is not None and when > lifetime.closed
        ):
            raise BeancountProjectionError("outside_directive_lifetime", identity)

    def evidence(
        self,
        kind: str,
        identity: str,
        indent: str = "  ",
        *,
        metadata_key: str = "evidence",
    ) -> list[str]:
        references = sorted(
            f"{row['evidence_kind']}:{row['evidence_id']}"
            for row in self.rows("provenance")
            if row["record_kind"] == kind
            and json.loads(_string(row, "record_id"))
            == [self.policy.entity_id, identity]
        )
        return (
            [f"{indent}jbt_{metadata_key}: {_quoted(_quoted(references))}"]
            if references
            else []
        )

    def action_effects(
        self, targets: Sequence[tuple[str, str]], indent: str = "    "
    ) -> list[str]:
        effects = sorted(
            {
                _string(row, "effect_id")
                for row in self.rows("action_applications")
                if (row["target_kind"], row["target_id"]) in targets
            }
        )
        return (
            _metadata(
                {"action_effect_ids": _quoted(effects)},
                ("action_effect_ids",),
                indent,
            )
            if effects
            else []
        )

    def link_metadata(self, txn_id: str) -> list[str]:
        members = sorted(
            [
                _string(row, "link_id"),
                _string(row, "link_kind"),
                _string(row, "member_role"),
            ]
            for row in self.rows("links")
            if row["member_kind"] == "transaction" and row["member_id"] == txn_id
        )
        return (
            _metadata({"link_members": _quoted(members)}, ("link_members",))
            if members
            else []
        )

    def posting(self, posting: Row) -> list[str]:
        posting_id = _string(posting, "posting_id")
        amount = _number(posting, "amount")
        symbol = self.symbols[_string(posting, "commodity_id")]
        account = self.account(posting)
        weights = self.related("posting_weights", "posting_id", posting_id)
        currencies = {_string(weight, "commodity_id") for weight in weights}
        if len(currencies) > 1:
            raise BeancountProjectionError("multi_currency_weight", posting_id)
        weight = _sum([_number(row, "amount") for row in weights])
        if (
            posting.get("price_per_unit_coefficient") is not None
            and _number(posting, "price_per_unit").numerator < 0
        ):
            raise BeancountProjectionError("negative_price", posting_id)
        changes = self.related("inventory_changes", "posting_id", posting_id)
        self.validate_position(posting, changes)
        if changes:
            implicit_cost = not weights and all(
                self.allocations[_string(change, "allocation_id")]["allocation_kind"]
                in {"transfer", "transformation"}
                for change in changes
            )
            lines = self.cost_postings(
                posting, changes, None if implicit_cost else weight, currencies
            )
        else:
            annotation = ""
            if not weights:
                if posting.get("price_per_unit_coefficient") is not None:
                    raise BeancountProjectionError("unrepresentable_weight", posting_id)
            elif currencies != {posting["commodity_id"]} or not _equal(weight, amount):
                if currencies == {posting["commodity_id"]}:
                    raise BeancountProjectionError(
                        "same_currency_weight_mismatch", posting_id
                    )
                if not currencies or not amount.numerator:
                    raise BeancountProjectionError("unrepresentable_weight", posting_id)
                unit_price = weight.divide(amount)
                if unit_price.numerator < 0:
                    raise BeancountProjectionError("negative_price", posting_id)
                quote = self.symbols[next(iter(currencies))]
                annotation = f" @ {_decimal(unit_price, posting_id)} {quote}"
            lines = [
                f"  {account} {_decimal(amount, posting_id)} {symbol}{annotation}",
                *_metadata(posting, ("posting_id", "posting_role", "step_id"), "    "),
                *self.action_effects([("posting", posting_id)]),
                *self.evidence("postings", posting_id, "    "),
            ]
        return lines

    def validate_position(self, posting: Row, changes: Sequence[Row]) -> None:
        position_id = posting.get("position_id")
        if not isinstance(position_id, str):
            return
        position = self.positions[position_id]
        posting_id = _string(posting, "posting_id")
        if (
            position["measurement_kind"] == "reference"
            or position["inventory_method"] == "pooled"
        ):
            raise BeancountProjectionError("unsupported_inventory", posting_id)
        if position["measurement_kind"] == "cost" and not changes:
            raise BeancountProjectionError("missing_inventory_changes", posting_id)

    def validate_capacity(self) -> None:
        recognized = {
            row["txn_id"]
            for row in self.rows("transactions")
            if row["event_state"] == "recognized" and row["event_kind"] != "note"
        }
        bounds: dict[tuple[str, str], BoundedRational] = {}
        for posting in self.rows("postings"):
            if posting["txn_id"] not in recognized:
                continue
            key = (self.account(posting), _string(posting, "commodity_id"))
            bounds[key] = bounds.get(key, BoundedRational(0, 1, 0)).add(
                _absolute(_number(posting, "amount"))
            )
        for (account, _), value in bounds.items():
            _decimal(value, account)

    def record_lot_change(
        self,
        change: Row,
        units: BoundedRational,
        unit_cost: BoundedRational,
        kind: str,
    ) -> None:
        key = (_string(change, "position_id"), _string(change, "lot_id"))
        previous = self.lot_units.get(key, BoundedRational(0, 1, 0))
        if not previous.numerator and kind not in {
            "acquisition",
            "opening",
            "transfer",
            "transformation",
        }:
            raise BeancountProjectionError("lot_unit_cost_changed", key[1])
        if previous.numerator and not _equal(unit_cost, self.lot_costs[key]):
            raise BeancountProjectionError("lot_unit_cost_changed", key[1])
        remaining = previous.add(units)
        if previous.numerator * remaining.numerator < 0:
            raise BeancountProjectionError("lot_overdrawn", key[1])
        if remaining.numerator:
            self.lot_units[key] = remaining
            self.lot_costs[key] = unit_cost
        else:
            self.lot_units.pop(key, None)
            self.lot_costs.pop(key, None)

    def action_metadata(self, txn_id: str, event_date: date) -> list[str]:
        actions = self.related("corporate_actions", "txn_id", txn_id)
        if not actions:
            return []
        if len(actions) != 1 or _date(actions[0], "effective_date") != event_date:
            raise BeancountProjectionError("action_date_mismatch", txn_id)
        action_id = _string(actions[0], "action_id")
        effects = self.related("corporate_action_effects", "action_id", action_id)
        metadata = [
            *_metadata(actions[0], ("action_id", "action_kind")),
            *self.evidence("corporate_actions", action_id),
        ]
        if effects:
            metadata.extend(
                _metadata(
                    {
                        "action_effect_ids": _quoted(
                            sorted(_string(row, "effect_id") for row in effects)
                        )
                    },
                    ("action_effect_ids",),
                )
            )
        if actions[0]["action_kind"] == "symbol_change":
            symbols = self.index("commodity_symbols", "symbol_id")
            aliases = [
                symbols[_string(effect, "symbol_id")]
                for effect in effects
                if effect.get("symbol_id") is not None
            ]
            metadata.extend(
                _metadata(
                    {
                        "symbol_ids": _quoted(
                            sorted(_string(row, "symbol_id") for row in aliases)
                        ),
                        "symbols": _quoted(
                            sorted(_string(row, "symbol") for row in aliases)
                        ),
                    },
                    ("symbol_ids", "symbols"),
                )
            )
        return metadata

    def validate_actions(self) -> None:
        for action in self.rows("corporate_actions"):
            identity = _string(action, "action_id")
            if action["action_kind"] not in {
                "split",
                "reverse_split",
                "spinoff",
                "return_of_capital",
                "merger",
                "redenomination",
                "symbol_change",
            }:
                raise BeancountProjectionError("unsupported_action_kind", identity)
            if not any(
                row["txn_id"] == action["txn_id"] and row["event_state"] == "recognized"
                for row in self.rows("transactions")
            ):
                raise BeancountProjectionError("unmatched_action", identity)

    def cost_postings(
        self,
        posting: Row,
        changes: Sequence[Row],
        weight: BoundedRational | None,
        currencies: set[str],
    ) -> list[str]:
        posting_id = _string(posting, "posting_id")
        lines: list[str] = []
        total_units = total_cost = BoundedRational(0, 1, 0)
        for change in sorted(
            changes,
            key=lambda row: (
                _number(row, "units_delta").numerator >= 0,
                _string(row, "change_id"),
            ),
        ):
            if change["pool_id"] is not None:
                raise BeancountProjectionError("pooled_inventory", posting_id)
            allocation = self.allocations[_string(change, "allocation_id")]
            kind = _string(allocation, "allocation_kind")
            if kind not in {
                "acquisition",
                "opening",
                "reduction",
                "transfer",
                "transformation",
            }:
                raise BeancountProjectionError("inventory_transformation", posting_id)
            lot = self.lots[_string(change, "lot_id")]
            units = _number(change, "units_delta")
            cost = _number(change, "principal_delta").add(
                _number(change, "capitalized_fee_delta")
            )
            if not units.numerator:
                raise BeancountProjectionError("zero_unit_cost_change", posting_id)
            unit_cost = cost.divide(units)
            if unit_cost.numerator < 0:
                raise BeancountProjectionError("negative_cost", posting_id)
            self.record_lot_change(change, units, unit_cost, kind)
            commodity = _string(lot, "book_commodity_id")
            if weight is not None and currencies != {commodity}:
                raise BeancountProjectionError("inventory_weight_currency", posting_id)
            total_units = total_units.add(units)
            total_cost = total_cost.add(cost)
            _decimal(cost, posting_id)
            label = _quoted(_string(lot, "lot_id"))
            annotation = (
                f"{{{_decimal(unit_cost, posting_id)} {self.symbols[commodity]}, "
                f"{_date(lot, 'acquisition_date')}, {label}}}"
            )
            lines.extend(
                [
                    (
                        f"  {self.account(posting)} {_decimal(units, posting_id)} "
                        f"{self.symbols[_string(posting, 'commodity_id')]} {annotation}"
                    ),
                    *_metadata(
                        posting, ("posting_id", "posting_role", "step_id"), "    "
                    ),
                    *_metadata(
                        change, ("change_id", "allocation_id", "lot_id"), "    "
                    ),
                    *self.action_effects(
                        [
                            ("posting", posting_id),
                            ("inventory_change", _string(change, "change_id")),
                            ("inventory_allocation", _string(change, "allocation_id")),
                        ]
                    ),
                    *self.evidence("postings", posting_id, "    "),
                ]
            )
        if not _equal(total_units, _number(posting, "amount")) or (
            weight is not None and not _equal(total_cost, weight)
        ):
            raise BeancountProjectionError("inventory_weight_mismatch", posting_id)
        return lines

    def transaction(self, transaction: Row) -> tuple[date, str, list[str]]:
        txn_id = _string(transaction, "txn_id")
        postings = sorted(
            self.related("postings", "txn_id", txn_id),
            key=lambda row: _string(row, "posting_id"),
        )
        if not postings:
            raise BeancountProjectionError("no_financial_postings", txn_id)
        dates = {self.effective_date(transaction, posting) for posting in postings}
        if len(dates) != 1:
            raise BeancountProjectionError(
                "split_posting_dates_requires_clearing", txn_id
            )
        event_date = dates.pop()
        for posting in postings:
            kind = "position" if posting["leg_kind"] == "position" else "category"
            self.check_lifetime(kind, _string(posting, f"{kind}_id"), event_date)
            self.check_lifetime(
                "commodity", _string(posting, "commodity_id"), event_date
            )
        lines = [
            (
                f"{event_date} * {_quoted(transaction.get('payee') or '')} "
                f"{_quoted(transaction.get('narration') or '')}"
            ),
            *_metadata(
                transaction,
                (
                    "entity_id",
                    "txn_id",
                    "event_kind",
                    "tags",
                    "links",
                    "date_authorized",
                    "date_traded",
                    "date_posted",
                    "date_settled",
                    "date_value",
                ),
            ),
            *self.evidence("transactions", txn_id),
            *self.link_metadata(txn_id),
            *self.action_metadata(txn_id, event_date),
        ]
        for posting in postings:
            lines.extend(self.posting(posting))
        self.check_weights(postings, txn_id)
        return event_date, txn_id, lines

    def check_weights(self, postings: Sequence[Row], txn_id: str) -> None:
        # Exact integer admission protects Beancount's 28-digit Decimal sums,
        # including cancellation after large intermediate values.
        totals: dict[str, BoundedRational] = {}
        absolute: dict[str, BoundedRational] = {}
        ids = {row["posting_id"] for row in postings}
        for weight in self.rows("posting_weights"):
            if weight["posting_id"] not in ids:
                continue
            currency = _string(weight, "commodity_id")
            amount = _number(weight, "amount")
            totals[currency] = totals.get(currency, BoundedRational(0, 1, 0)).add(
                amount
            )
            absolute[currency] = absolute.get(currency, BoundedRational(0, 1, 0)).add(
                _absolute(amount)
            )
        for posting in postings:
            if self.related("posting_weights", "posting_id", posting["posting_id"]):
                continue
            changes = self.related(
                "inventory_changes", "posting_id", posting["posting_id"]
            )
            if not changes:
                currency = _string(posting, "commodity_id")
                amount = _number(posting, "amount")
                totals[currency] = totals.get(currency, BoundedRational(0, 1, 0)).add(
                    amount
                )
                absolute[currency] = absolute.get(
                    currency, BoundedRational(0, 1, 0)
                ).add(_absolute(amount))
            for change in changes:
                currency = _string(
                    self.lots[_string(change, "lot_id")], "book_commodity_id"
                )
                amount = _number(change, "principal_delta").add(
                    _number(change, "capitalized_fee_delta")
                )
                totals[currency] = totals.get(currency, BoundedRational(0, 1, 0)).add(
                    amount
                )
                absolute[currency] = absolute.get(
                    currency, BoundedRational(0, 1, 0)
                ).add(_absolute(amount))
        if any(value.numerator for value in totals.values()):
            raise BeancountProjectionError("unbalanced_weights", txn_id)
        for value in absolute.values():
            _decimal(value, txn_id)

    def assertions(self) -> list[tuple[date, str, list[str]]]:
        scopes = self.index("assertion_scopes", "scope_id")
        directives = []
        for assertion in self.rows("balance_assertions"):
            identity = _string(assertion, "assertion_set_id")
            scope = scopes[_string(assertion, "scope_id")]
            if (
                assertion["assertion_kind"] == "point"
                or assertion["is_complete"]
                or scope["measurement"] != "units"
                or scope["scope_kind"] != "positions"
                or assertion.get("timestamp_local") is not None
            ):
                raise BeancountProjectionError("unsupported_assertion_scope", identity)
            when = _date(assertion, "date")
            if assertion["assertion_kind"] == "closing":
                if when == date.max:
                    raise BeancountProjectionError("assertion_date_overflow", identity)
                when += timedelta(days=1)
            for balance in self.related("balances", "assertion_set_id", identity):
                position_id = _string(balance, "position_id")
                if position_id not in self.accounts:
                    raise BeancountProjectionError("unmapped_assertion", identity)
                self.check_lifetime("position", position_id, when)
                self.check_lifetime("commodity", _string(balance, "commodity_id"), when)
                text = (
                    f"{when} balance {self.accounts[position_id]} "
                    f"{_decimal(_number(balance, 'amount'), identity)} ~ 0 "
                    f"{self.symbols[_string(balance, 'commodity_id')]}"
                )
                directives.append(
                    (
                        when,
                        _string(balance, "balance_id"),
                        [
                            text,
                            *_metadata(assertion, ("assertion_set_id",)),
                            *_metadata(balance, ("balance_id",)),
                            *self.evidence("balance_assertions", identity),
                            *self.evidence(
                                "balances",
                                _string(balance, "balance_id"),
                                metadata_key="balance_evidence",
                            ),
                        ],
                    )
                )
        return directives

    def prices(self) -> list[tuple[date, str, list[str]]]:
        directives = []
        keys: set[tuple[date, str, str]] = set()
        for price in self.rows("prices"):
            identity = _string(price, "price_id")
            when = _date(price, "date")
            commodity = self.symbols[_string(price, "commodity_id")]
            quote = self.symbols[_string(price, "quote_commodity_id")]
            self.check_lifetime("commodity", _string(price, "commodity_id"), when)
            self.check_lifetime("commodity", _string(price, "quote_commodity_id"), when)
            key = (when, commodity, quote)
            if key in keys or price.get("timestamp_local") is not None:
                raise BeancountProjectionError("ambiguous_price_projection", identity)
            keys.add(key)
            rate = _number(price, "rate")
            directives.append(
                (
                    when,
                    identity,
                    [
                        f"{when} price {commodity} {_decimal(rate, identity)} {quote}",
                        *_metadata(price, ("price_id", "basis", "origin", "market")),
                        *self.evidence("prices", identity),
                    ],
                )
            )
        return directives


def render_beancount(tables: Tables, *, policy: BeancountPolicy) -> str:
    """Render validated records or fail without changing economic choices."""
    projection = _Projection(tables, policy)
    projection.validate_capacity()
    projection.validate_actions()
    order: dict[str, int] = {}
    for step in projection.rows("book_steps"):
        sequence = step["sequence"]
        if type(sequence) is not int:
            raise BeancountProjectionError("step_sequence_required", "book_steps")
        txn_id = _string(step, "txn_id")
        if txn_id in order:
            raise BeancountProjectionError("multi_step_transaction", txn_id)
        order[txn_id] = sequence
    transactions = [
        transaction
        for transaction in projection.rows("transactions")
        if transaction["event_state"] == "recognized"
        and transaction["event_kind"] != "note"
        and projection.related("postings", "txn_id", transaction["txn_id"])
    ]
    if any(_string(row, "txn_id") not in order for row in transactions):
        raise BeancountProjectionError("book_order_required", policy.entity_id)
    directives = [
        projection.transaction(transaction)
        for transaction in sorted(
            transactions, key=lambda row: order[_string(row, "txn_id")]
        )
    ]
    if any(identity not in order for _, identity, _ in directives):
        raise BeancountProjectionError("book_order_required", policy.entity_id)
    dates = [item[0] for item in sorted(directives, key=lambda item: order[item[1]])]
    if dates != sorted(dates):
        raise BeancountProjectionError("date_policy_reorders_book", policy.entity_id)
    directives.extend(projection.assertions())
    directives.extend(projection.prices())
    directives.extend(
        projection.note(transaction)
        for transaction in projection.rows("transactions")
        if transaction["event_state"] == "recognized"
        and (
            transaction["event_kind"] == "note"
            or not projection.related("postings", "txn_id", transaction["txn_id"])
        )
    )
    directives.extend(projection.lifecycle_directives())
    lines = [
        'option "tolerance_multiplier" "0"',
        'option "inferred_tolerance_default" "*:0"',
        "",
    ]
    for _, _, block in sorted(
        directives, key=lambda item: (item[0], order.get(item[1], -1), item[1])
    ):
        lines.extend(["", *block])
    return "\n".join(lines) + "\n"
