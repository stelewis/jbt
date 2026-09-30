"""Independent artifact consumer: replay determinations, never book events."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import duckdb
from jsonschema import Draft202012Validator, ValidationError
from tests.integration.conformance.consumer_arithmetic import read_number

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jsonschema.protocols import Validator

type Row = dict[str, Any]
type Tables = Mapping[str, list[Row]]
type SliceKey = tuple[str, str, str, str]

COMPONENTS = ("principal", "capitalized_fee", "expensed_fee")
DATE_ROLES = frozenset({"authorized", "traded", "posted", "settled", "value"})
WORK_LIMIT = 10**384
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
SCHEMA_FILES = frozenset(
    name + ".json"
    for name in (
        "schema",
        "declaration_schema",
        "manifest_schema",
        "descriptor_schema",
        "configuration_schema",
        "registry_schema",
        "extraction_schema",
    )
)


class ReaderError(ValueError):
    """An artifact cannot be interpreted without violating its contract."""


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise ReaderError(message)


def _number(row: Row, name: str) -> Fraction | None:
    number = read_number(
        row[name + "_coefficient"],
        row[name + "_scale"],
        row[name + "_source_scale"],
    )
    if number is None:
        return None
    return Fraction(number.coefficient, 10**number.scale)


def _required_number(row: Row, name: str) -> Fraction:
    number = _number(row, name)
    _require(number is not None, f"required numeric group: {name}")
    assert number is not None
    return number


def _add(left: Fraction, right: Fraction) -> Fraction:
    _require(
        abs(left.numerator) * right.denominator
        + abs(right.numerator) * left.denominator
        < WORK_LIMIT
        and left.denominator * right.denominator < WORK_LIMIT,
        "intermediate arithmetic limit",
    )
    return left + right


def _date(value: date | str) -> date:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _index(tables: Tables, table: str, key: str, entity: str) -> dict[str, Row]:
    rows = [row for row in tables.get(table, []) if row["entity_id"] == entity]
    result = {row[key]: row for row in rows}
    _require(len(result) == len(rows), f"duplicate {table} identity")
    return result


def _slice(row: Row) -> SliceKey:
    lot, pool = row["lot_id"], row["pool_id"]
    _require((lot is None) != (pool is None), "exactly one inventory identity")
    return (
        row["entity_id"],
        row["position_id"],
        "lot" if lot is not None else "pool",
        lot if lot is not None else pool,
    )


@dataclass
class Inventory:
    units: Fraction = Fraction()
    principal: Fraction | None = Fraction()
    capitalized_fee: Fraction | None = Fraction()
    expensed_fee: Fraction | None = Fraction()
    fees: dict[str, Fraction] = field(default_factory=dict)
    book_fees: dict[str, Fraction | None] = field(default_factory=dict)


@dataclass
class ReplayResult:
    inventory: dict[SliceKey, Inventory]
    references: dict[tuple[str, str, str], Fraction]
    quantity_positions: dict[tuple[str, str], Fraction]
    allocation_edges: tuple[Row, ...]
    settlements: dict[tuple[str, str], Fraction]


def effective_posting_date(posting: Row, transaction: Row, *, role: str) -> date | None:
    _require(role in DATE_ROLES, "unknown explicit date role")
    name = "date_" + role
    mode = posting[name + "_mode"]
    _require(mode in {"inherit", "value", "unknown"}, "unknown date override mode")
    value = posting[name]
    _require((mode == "value") == (value is not None), "invalid date override")
    if mode == "inherit":
        value = transaction[name]
    return None if value is None else _date(value)


def project_quantities(
    tables: Tables,
    *,
    entity: str,
    date_role: str,
    cutoff: date,
    opening: bool = False,
) -> dict[str, Fraction]:
    transactions = _index(tables, "transactions", "txn_id", entity)
    quantities: dict[str, Fraction] = {}
    for posting in tables.get("postings", []):
        if posting["entity_id"] != entity or posting["leg_kind"] != "position":
            continue
        transaction = transactions[posting["txn_id"]]
        if transaction["event_state"] != "recognized":
            continue
        when = effective_posting_date(posting, transaction, role=date_role)
        _require(when is not None, "quantity projection has unknown effective date")
        assert when is not None
        if when > cutoff or (opening and when == cutoff):
            continue
        position = posting["position_id"]
        quantities[position] = _add(
            quantities.get(position, Fraction()), _required_number(posting, "amount")
        )
    return quantities


def _apply_change(state: Inventory, change: Row, fees: list[Row]) -> None:
    state.units = _add(state.units, _required_number(change, "units_delta"))
    for component in COMPONENTS:
        previous = getattr(state, component)
        delta = _number(change, component + "_delta")
        setattr(
            state,
            component,
            None if previous is None or delta is None else _add(previous, delta),
        )
    for fee in fees:
        identity = fee["fee_allocation_id"]
        state.fees[identity] = _add(
            state.fees.get(identity, Fraction()), _required_number(fee, "amount_delta")
        )
        previous = state.book_fees.get(identity, Fraction())
        delta = _number(fee, "book_amount_delta")
        state.book_fees[identity] = (
            None if previous is None or delta is None else _add(previous, delta)
        )


def _apply_reference(
    references: dict[tuple[str, str, str], Fraction],
    row: Row,
    inventory: dict[SliceKey, Inventory],
) -> None:
    key = (row["entity_id"], row["position_id"], row["lot_id"])
    before, after = _number(row, "reference_before"), _number(row, "reference_after")
    kind = row["change_kind"]
    _require(kind in {"entry", "reset", "close"}, "unknown reference transition")
    units = inventory[(key[0], key[1], "lot", key[2])].units
    if kind in {"entry", "reset"}:
        _require(
            _required_number(row, "units") == units and units != 0,
            "reference transition requires a complete named slice",
        )
    if kind == "entry":
        _require(before is None and after is not None, "invalid reference entry")
        _require(key not in references, "duplicate reference entry")
    else:
        _require(
            before == references.get(key) and before is not None, "stale reference"
        )
        _require((kind == "reset") == (after is not None), "invalid reference change")
    if after is not None:
        references[key] = after
    elif units == 0:
        del references[key]


def _step_prefix(steps: dict[str, Row], cutoff: date | None) -> list[Row]:
    ordered = sorted(steps.values(), key=lambda row: row["sequence"])
    _require(
        [row["sequence"] for row in ordered] == list(range(len(ordered))),
        "noncontiguous replay sequence",
    )
    if cutoff is None:
        return ordered
    included = [_date(row["effective_date"]) <= cutoff for row in ordered]
    _require(included == sorted(included, reverse=True), "as-of is not a replay prefix")
    return [row for row, include in zip(ordered, included, strict=True) if include]


def _rows_for(tables: Tables, name: str, entity: str) -> list[Row]:
    return [row for row in tables.get(name, []) if row["entity_id"] == entity]


def _path_precedes(edges: dict[str, set[str]], before: str, after: str) -> bool:
    pending = [before]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == after:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(edges[current] - seen)
    return False


def _split_source_steps(tables: Tables, *, entity: str, action_id: str) -> set[str]:
    allocations = {
        row["allocation_id"]
        for row in _rows_for(tables, "inventory_allocations", entity)
        if row["action_id"] == action_id
    }
    effects = _index(tables, "corporate_action_effects", "effect_id", entity)
    applied = {
        row["target_id"]
        for row in _rows_for(tables, "action_applications", entity)
        if row["target_kind"] == "inventory_allocation"
        and row["target_id"] in allocations
        and effects[row["effect_id"]]["action_id"] == action_id
    }
    lots = _index(tables, "lots", "lot_id", entity)
    return {
        lots[row["lot_id"]]["creation_step_id"]
        for row in _rows_for(tables, "inventory_changes", entity)
        if row["allocation_id"] in applied
        and row["change_role"] == "source"
        and row["lot_id"] in lots
    }


def _sequence_observation(
    tables: Tables, *, entity: str, step: Row
) -> tuple[str, str, Fraction]:
    names = {"source.sequence", "source.sequence_domain"}
    selections = [
        [
            row
            for row in _rows_for(tables, "observations", entity)
            if row["record_kind"] == kind
            and row["record_id"]
            == json.dumps([identity], ensure_ascii=False, separators=(",", ":"))
            and row["field_name"] in names
        ]
        for kind, identity in (
            ("book_steps", step["step_id"]),
            ("transactions", step["txn_id"]),
        )
    ]
    _require(not all(selections), "ambiguous source sequence observations")
    rows = selections[0] or selections[1]
    by_name = {row["field_name"]: row for row in rows}
    _require(
        len(rows) == len(by_name) == len(names)
        and len({row["source_record_id"] for row in rows}) == 1,
        "incomplete source sequence observations",
    )
    sequence = by_name["source.sequence"]
    domain = by_name["source.sequence_domain"]
    _require(
        sequence["observation_kind"] == domain["observation_kind"] == "source_field"
        and sequence["value_type"] == "decimal"
        and sequence["decimal_value_scale"] == 0
        and domain["value_type"] == "text"
        and bool(domain["text_value"])
        and all(
            row["source_record_id"] is not None
            and row["declaration_id"] is None
            and row["commodity_id"] is None
            for row in rows
        ),
        "invalid source sequence observations",
    )
    source_records = _index(tables, "source_records", "source_record_id", entity)
    source = source_records[sequence["source_record_id"]]
    return (
        source["source_scope_id"],
        domain["text_value"],
        _required_number(sequence, "decimal_value"),
    )


def _check_source_sequence_edges(
    tables: Tables,
    *,
    entity: str,
    directions: Mapping[tuple[str, str, str], str],
) -> None:
    steps = _index(tables, "book_steps", "step_id", entity)
    for edge in _rows_for(tables, "book_step_dependencies", entity):
        if edge["constraint_kind"] != "source_sequence":
            continue
        before = steps[edge["before_step_id"]]
        after = steps[edge["after_step_id"]]
        _require(
            before["sequence"] < after["sequence"], "backward source sequence edge"
        )
        left = _sequence_observation(tables, entity=entity, step=before)
        right = _sequence_observation(tables, entity=entity, step=after)
        _require(left[:2] == right[:2], "incomparable source sequence scope or domain")
        _require(left[2] != right[2], "duplicate source sequence ordinal")
        direction = directions.get((*left[:2], "source.sequence"))
        _require(
            direction in {"ascending", "descending"},
            "missing source sequence direction",
        )
        _require(
            (left[2] < right[2]) if direction == "ascending" else (left[2] > right[2]),
            "source sequence contradicts registered direction",
        )


def _check_same_day_split_order(tables: Tables, *, entity: str) -> None:
    steps = _index(tables, "book_steps", "step_id", entity)
    acquisitions = {
        row["step_id"]
        for row in _rows_for(tables, "inventory_allocations", entity)
        if row["allocation_kind"] == "acquisition"
    }
    commodities = {
        (row["step_id"], row["commodity_id"])
        for row in _rows_for(tables, "postings", entity)
        if row["step_id"] in acquisitions and row["leg_kind"] == "position"
    }
    edges: dict[str, set[str]] = defaultdict(set)
    for row in _rows_for(tables, "book_step_dependencies", entity):
        edges[row["before_step_id"]].add(row["after_step_id"])

    for action in _rows_for(tables, "corporate_actions", entity):
        if action["action_kind"] != "split":
            continue
        action_step = next(
            (
                row["step_id"]
                for row in steps.values()
                if row["txn_id"] == action["txn_id"]
            ),
            None,
        )
        if action_step is None:
            continue
        consumed_origins = _split_source_steps(
            tables, entity=entity, action_id=action["action_id"]
        )
        for buy_step, commodity in commodities:
            if (
                commodity != action["commodity_id"]
                or steps[buy_step]["effective_date"]
                != steps[action_step]["effective_date"]
            ):
                continue
            before, after = sorted(
                (action_step, buy_step), key=lambda key: steps[key]["sequence"]
            )
            if before == buy_step and buy_step in consumed_origins:
                continue
            _require(
                _path_precedes(edges, before, after), "missing decisive same-day order"
            )


def _check_step_state(
    inventory: dict[SliceKey, Inventory], positions: dict[str, Row]
) -> None:
    for key, state in inventory.items():
        if positions[key[1]]["inventory_method"] == "pooled" and key[2] == "lot":
            _require(state.units == 0, "individual remainder in pooled position")
        if state.units == 0:
            _require(
                all(getattr(state, component) in {None, 0} for component in COMPONENTS),
                "full close leaves book components",
            )


def _quantity_replay(
    tables: Tables, *, entity: str, selected_ids: set[str]
) -> dict[tuple[str, str], Fraction]:
    transactions = _index(tables, "transactions", "txn_id", entity)
    positions = _index(tables, "positions", "position_id", entity)
    steps = _index(tables, "book_steps", "step_id", entity)
    quantities: dict[tuple[str, str], Fraction] = {}
    for posting in _rows_for(tables, "postings", entity):
        transaction = transactions[posting["txn_id"]]
        if transaction["event_state"] != "recognized":
            _require(posting["step_id"] is None, "nonparticipating posting step")
            continue
        _require(posting["step_id"] in steps, "recognized posting missing step")
        if posting["step_id"] not in selected_ids or posting["leg_kind"] != "position":
            continue
        position = positions[posting["position_id"]]
        if position["measurement_kind"] == "quantity":
            key = (entity, posting["position_id"])
            quantities[key] = _add(
                quantities.get(key, Fraction()), _required_number(posting, "amount")
            )
    return quantities


def _settlement_replay(
    tables: Tables, *, entity: str, selected_ids: set[str]
) -> dict[tuple[str, str], Fraction]:
    references = _index(tables, "reference_changes", "reference_change_id", entity)
    settlements: dict[tuple[str, str], Fraction] = {}
    for row in _rows_for(tables, "reference_settlements", entity):
        if references[row["reference_change_id"]]["step_id"] in selected_ids:
            key = (entity, row["commodity_id"])
            settlements[key] = _add(
                settlements.get(key, Fraction()), _required_number(row, "amount")
            )
    return settlements


def replay_rows(
    tables: Tables, *, entity: str, effective_date: date | None = None
) -> ReplayResult:
    transactions = _index(tables, "transactions", "txn_id", entity)
    positions = _index(tables, "positions", "position_id", entity)
    steps = _index(tables, "book_steps", "step_id", entity)
    allocations = _index(tables, "inventory_allocations", "allocation_id", entity)
    changes: dict[str, list[Row]] = defaultdict(list)
    fees: dict[str, list[Row]] = defaultdict(list)
    references_by_step: dict[str, list[Row]] = defaultdict(list)
    for row in _rows_for(tables, "inventory_changes", entity):
        changes[allocations[row["allocation_id"]]["step_id"]].append(row)
    for row in _rows_for(tables, "inventory_fee_changes", entity):
        fees[row["change_id"]].append(row)
    for row in _rows_for(tables, "reference_changes", entity):
        references_by_step[row["step_id"]].append(row)
    for edge in _rows_for(tables, "book_step_dependencies", entity):
        _require(
            steps[edge["before_step_id"]]["sequence"]
            < steps[edge["after_step_id"]]["sequence"],
            "backward replay dependency",
        )
    _check_same_day_split_order(tables, entity=entity)
    result = ReplayResult({}, {}, {}, (), {})
    selected = _step_prefix(steps, effective_date)
    selected_ids = {row["step_id"] for row in selected}
    for step in selected:
        _require(
            transactions[step["txn_id"]]["event_state"] == "recognized",
            "nonparticipating replay step",
        )
        for change in changes[step["step_id"]]:
            key = _slice(change)
            state = result.inventory.setdefault(key, Inventory())
            _apply_change(state, change, fees[change["change_id"]])
        _check_step_state(result.inventory, positions)
        for reference in references_by_step[step["step_id"]]:
            _apply_reference(result.references, reference, result.inventory)
    result.quantity_positions = _quantity_replay(
        tables, entity=entity, selected_ids=selected_ids
    )
    result.settlements = _settlement_replay(
        tables, entity=entity, selected_ids=selected_ids
    )
    result.allocation_edges = tuple(
        row for row in allocations.values() if row["step_id"] in selected_ids
    )
    return result


@dataclass(frozen=True)
class AssertionResult:
    entity: str
    assertion_set_id: str
    status: str
    expected: dict[str, Fraction]
    observed: dict[str, Fraction]


def check_assertions(
    tables: Tables, *, entity: str, date_roles: Mapping[str, str]
) -> tuple[AssertionResult, ...]:
    scopes = _index(tables, "assertion_scopes", "scope_id", entity)
    positions = _index(tables, "positions", "position_id", entity)
    answers = []
    for assertion in tables.get("balance_assertions", []):
        if assertion["entity_id"] != entity:
            continue
        identity = assertion["assertion_set_id"]
        scope = scopes[assertion["scope_id"]]
        if scope["measurement"] != "units" or scope["coverage_basis"] not in date_roles:
            answers.append(AssertionResult(entity, identity, "not_evaluable", {}, {}))
            continue
        selected = {
            key: position
            for key, position in positions.items()
            if position["account_id"] == scope["account_id"]
            and (scope["selection"] == "account_all" or key in scope["position_ids"])
        }
        quantities = project_quantities(
            tables,
            entity=entity,
            date_role=date_roles[scope["coverage_basis"]],
            cutoff=_date(assertion["date"]),
            opening=assertion["assertion_kind"] == "opening",
        )
        observed: dict[str, Fraction] = {}
        for key, position in selected.items():
            measurement = (
                key if scope["scope_kind"] == "positions" else position["commodity_id"]
            )
            observed[measurement] = _add(
                observed.get(measurement, Fraction()), quantities.get(key, Fraction())
            )
        expected = {}
        for row in tables.get("balances", []):
            if row["entity_id"] != entity or row["assertion_set_id"] != identity:
                continue
            key = (
                row["position_id"]
                if scope["scope_kind"] == "positions"
                else row["commodity_id"]
            )
            _require(key not in expected, "duplicate scoped measurement")
            expected[key] = _required_number(row, "amount")
        keys = (
            observed.keys() | expected.keys()
            if assertion["is_complete"]
            else expected.keys()
        )
        status = (
            "pass"
            if all(
                expected.get(key, Fraction()) == observed.get(key, Fraction())
                for key in keys
            )
            else "fail"
        )
        answers.append(AssertionResult(entity, identity, status, expected, observed))
    return tuple(answers)


def _sort_value(value: object) -> tuple[int, object]:
    if value is None:
        return (0, "")
    return (1, value.encode("utf-8") if isinstance(value, str) else value)


def _validate_cell(column: Row, value: object) -> None:
    name, kind = column["name"], column["kind"]
    if value is None:
        _require(column["nullable"], f"null required field: {name}")
        return
    types = {"string": str, "integer": int, "boolean": bool, "date": str, "list": list}
    _require(type(value) is types[kind], f"wrong physical value type: {name}")
    if kind == "integer":
        assert isinstance(value, int)
        _require(-(2**63) <= value < 2**63, "INT64 range")
    elif kind == "string":
        assert isinstance(value, str)
        _require(unicodedata.normalize("NFC", value) == value, "non-NFC string")
        if name.endswith("_id") or name == "entity_id":
            _require(bool(value), "empty identity")
    elif kind == "date":
        assert isinstance(value, str)
        _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None, "date spelling")
        _date(value)
    elif kind == "list":
        assert isinstance(value, list)
        _require(all(type(item) is str for item in value), "non-string list member")
        _require(
            value == sorted(set(value), key=lambda item: item.encode("utf-8")),
            "list ordering or duplicate",
        )
        _require(
            all(unicodedata.normalize("NFC", item) == item for item in value),
            "non-NFC list member",
        )
    if "enum" in column:
        _require(value in column["enum"], f"unknown enumeration: {name}")


def _validate_table(table: Row, rows: list[Row]) -> None:
    columns = table["columns"]
    names = {column["name"] for column in columns}
    for row in rows:
        _require(set(row) == names, f"wrong fields: {table['name']}")
        for column in columns:
            _validate_cell(column, row[column["name"]])
        for group in table["numeric_groups"]:
            value = _number(row, group["name"])
            _require(
                value is not None or group["nullable"], "null required numeric group"
            )
    for key in [table["primary_key"], *table["unique"]]:
        keys = [tuple(row[name] for name in key) for row in rows]
        _require(len(set(keys)) == len(keys), f"duplicate key: {table['name']}")
    keys = [tuple(_sort_value(row[name]) for name in table["sort_key"]) for row in rows]
    _require(keys == sorted(keys), f"wrong row ordering: {table['name']}")


def _validate_symbol_resolution(tables: Tables) -> None:
    by_symbol: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in tables["commodity_symbols"]:
        by_symbol[(row["entity_id"], row["symbol"])].append(row)
    for symbols in by_symbol.values():
        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                if left["commodity_id"] == right["commodity_id"]:
                    continue
                distinct_context = any(
                    left[field] is not None
                    and right[field] is not None
                    and left[field] != right[field]
                    for field in ("source_scope_id", "exchange")
                )
                concurrent = (
                    left["valid_to"] is None or right["valid_from"] < left["valid_to"]
                ) and (
                    right["valid_to"] is None or left["valid_from"] < right["valid_to"]
                )
                _require(
                    distinct_context or not concurrent, "ambiguous commodity symbol"
                )


def validate_rows(
    tables: Tables,
    schema: Row,
    *,
    expected_version: int = 1,
    source_sequence_directions: Mapping[tuple[str, str, str], str] | None = None,
) -> None:
    _require(
        type(expected_version) is int
        and expected_version == 1
        and schema["schema_version"] == expected_version,
        "unsupported schema version",
    )
    specifications = {table["name"]: table for table in schema["tables"]}
    _require(set(tables) == set(specifications), "incomplete table inventory")
    for name, specification in specifications.items():
        _validate_table(specification, tables[name])
    for name, specification in specifications.items():
        for foreign in specification["foreign_keys"]:
            targets = {
                tuple(row[key] for key in foreign["target_columns"])
                for row in tables[foreign["table"]]
            }
            for row in tables[name]:
                reference = tuple(row[key] for key in foreign["columns"])
                if all(value is not None for value in reference):
                    _require(reference in targets, f"dangling reference: {name}")
    entities = {row["entity_id"] for row in tables["build"]}
    _require(bool(entities), "missing build entity")
    _require(
        all(row["entity_id"] in entities for rows in tables.values() for row in rows),
        "undeclared entity",
    )
    _validate_symbol_resolution(tables)
    for entity in entities:
        _check_source_sequence_edges(
            tables, entity=entity, directions=source_sequence_directions or {}
        )
        _validate_financial_rows(tables, entity=entity)
        _check_same_day_split_order(tables, entity=entity)
        _validate_action_applications(tables, entity=entity)
    for declaration in tables["declarations"]:
        _validate_declaration_revision(declaration)
    _validate_record_references(tables, specifications)
    _require_field_evidence(tables, specifications)


def _validate_declaration_revision(row: Row) -> None:
    payload_bytes = row["payload_json"].encode("utf-8")
    payload = _json(payload_bytes)
    _require(
        canonical_bytes(payload, integer_strings=False) == payload_bytes + b"\n",
        "noncanonical declaration payload",
    )
    _require(
        payload.get("kind") == row["declaration_kind"]
        and type(payload.get("schema_version")) is int
        and payload["schema_version"] == 1,
        "declaration kind or version mismatch",
    )
    projection = {
        "entity_id": row["entity_id"],
        "declaration_key": row["declaration_key"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "kind": payload["kind"],
        "schema_version": payload["schema_version"],
        "payload": payload,
    }
    digest = hashlib.sha256(
        canonical_bytes(projection, integer_strings=False)
    ).hexdigest()
    _require(
        row["declaration_id"] == digest and row["revision_digest"] == digest,
        "declaration revision identity mismatch",
    )


def _validate_action_applications(tables: Tables, *, entity: str) -> None:
    effects = _index(tables, "corporate_action_effects", "effect_id", entity)
    allocations = _index(tables, "inventory_allocations", "allocation_id", entity)
    changes = _index(tables, "inventory_changes", "change_id", entity)
    disposals = _index(tables, "disposals", "disposal_id", entity)
    postings = _index(tables, "postings", "posting_id", entity)
    lots = _index(tables, "lots", "lot_id", entity)
    pools = _index(tables, "inventory_pools", "pool_id", entity)
    for row in _rows_for(tables, "action_applications", entity):
        effect = effects[row["effect_id"]]
        kind = row["target_kind"]
        _require(
            kind in {"inventory_change", "inventory_allocation", "disposal", "posting"},
            "unknown action application target",
        )
        _require(
            effect["effect_kind"] != "symbol"
            and (effect["effect_kind"] == "basis") == (kind == "inventory_change"),
            "basis effect requires an exact inventory change target",
        )
        target = {
            "inventory_change": changes,
            "inventory_allocation": allocations,
            "disposal": disposals,
            "posting": postings,
        }[kind].get(row["target_id"])
        _require(target is not None, "dangling action application")
        assert target is not None
        if kind == "inventory_change":
            lot, pool = target["lot_id"], target["pool_id"]
            _require((lot is None) != (pool is None), "basis target lacks inventory")
            book_commodity = (
                lots[lot]["book_commodity_id"]
                if lot is not None
                else pools[pool]["book_commodity_id"]
            )
            if (
                effect["basis_total_coefficient"] is not None
                and effect["basis_commodity_id"] == book_commodity
            ):
                _require(
                    _required_number(effect, "basis_total")
                    == _required_number(target, "principal_delta"),
                    "basis effect targets the wrong change amount",
                )
            target = allocations[target["allocation_id"]]
        elif kind == "disposal":
            target = allocations[target["allocation_id"]]
        _require(target["step_id"] == row["step_id"], "action application step")
        if kind in {"inventory_change", "inventory_allocation", "disposal"}:
            _require(
                target["action_id"] == effect["action_id"],
                "action application identity",
            )


def _require_field_evidence(tables: Tables, specifications: dict[str, Row]) -> None:
    required = {
        "transactions": "event_kind event_state date_authorized date_traded "
        "date_posted date_settled date_value payee narration",
        "postings": "amount posting_role leg_kind price_per_unit original_amount "
        "fx_rate_as_stated",
        "book_steps": "sequence operation_kind effective_date",
        "posting_weights": "weight_kind amount",
        "inventory_changes": "change_role units_delta principal_delta "
        "capitalized_fee_delta expensed_fee_delta",
        "component_conversions": "source_amount target_amount rate",
        "disposals": "proceeds_total disposal_fees_total",
        "fee_allocations": "amount book_amount treatment",
        "inventory_fee_changes": "amount_delta book_amount_delta",
        "reference_changes": "change_kind units reference_before reference_after "
        "settlement_reference",
        "reference_settlements": "amount",
        "balances": "amount",
        "prices": "rate date basis",
    }
    covered = {
        (row["entity_id"], row["record_kind"], row["record_id"], row["field_name"])
        for row in tables["provenance"]
    }
    for name, fields in required.items():
        key_fields = specifications[name]["primary_key"][1:]
        for row in tables[name]:
            if name == "transactions" and row["event_state"] != "recognized":
                continue
            key = json.dumps(
                [row[field] for field in key_fields],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for field_name in fields.split():
                physical = (
                    field_name if field_name in row else field_name + "_coefficient"
                )
                _require(
                    row[physical] is None
                    or (row["entity_id"], name, key, field_name) in covered,
                    "missing canonical field provenance",
                )


def _validate_record_references(tables: Tables, specifications: dict[str, Row]) -> None:
    indices = {
        name: {
            tuple(row[key] for key in specification["primary_key"])
            for row in tables[name]
        }
        for name, specification in specifications.items()
    }
    for name in ("observations", "provenance"):
        for row in tables[name]:
            target = row["record_kind"]
            _require(
                target in specifications and target not in {"provenance", "build"},
                "unknown record reference table",
            )
            key = json.loads(row["record_id"])
            _require(
                isinstance(key, list)
                and len(key) == len(specifications[target]["primary_key"]) - 1
                and all(type(item) in {str, int, bool} for item in key),
                "record reference requires a complete scalar array key",
            )
            _require(
                json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                == row["record_id"],
                "noncanonical record reference",
            )
            _require(
                (row["entity_id"], *key) in indices[target],
                "dangling composite record reference",
            )
            if name == "provenance":
                _require(
                    row["field_name"] in specifications[target]["logical_fields"],
                    "unknown referenced field",
                )
                evidence = {
                    "source_record": "source_records",
                    "declaration": "declarations",
                }[row["evidence_kind"]]
                _require(
                    (row["entity_id"], row["evidence_id"]) in indices[evidence],
                    "dangling provenance evidence",
                )
                _require(
                    row["value_origin"] not in {"rule", "booking", "action"}
                    or row["derivation_id"] is not None,
                    "missing provenance derivation",
                )
            else:
                _require(
                    (row["source_record_id"] is None)
                    != (row["declaration_id"] is None),
                    "observation requires exactly one evidence source",
                )
                values = (
                    row["decimal_value_coefficient"],
                    row["text_value"],
                    row["date_value"],
                )
                expected = {"decimal": 0, "text": 1, "date": 2}[row["value_type"]]
                _require(
                    all(
                        (value is not None) == (index == expected)
                        for index, value in enumerate(values)
                    ),
                    "observation value shape",
                )


def _validate_postings(
    tables: Tables,
    *,
    entity: str,
    transactions: dict[str, Row],
    positions: dict[str, Row],
) -> None:
    postings = _index(tables, "postings", "posting_id", entity)
    changes: dict[str, list[Row]] = defaultdict(list)
    for row in tables["inventory_changes"]:
        if row["entity_id"] == entity:
            changes[row["posting_id"]].append(row)
    indices: dict[str, list[int]] = defaultdict(list)
    for posting in postings.values():
        indices[posting["txn_id"]].append(posting["posting_index"])
        transaction = transactions[posting["txn_id"]]
        recognized = transaction["event_state"] == "recognized"
        _require(
            recognized == (posting["step_id"] is not None), "posting participation"
        )
        for role in DATE_ROLES:
            effective_posting_date(posting, transaction, role=role)
        if posting["leg_kind"] == "boundary":
            _require(
                posting["position_id"] is None and posting["account_id"] is None,
                "boundary has a holding relationship",
            )
            continue
        position = positions[posting["position_id"]]
        _require(
            posting["account_id"] == position["account_id"]
            and posting["commodity_id"] == position["commodity_id"]
            and posting["category_id"] is None,
            "posting position mismatch",
        )
        if recognized and position["measurement_kind"] != "quantity":
            rows = changes[posting["posting_id"]]
            _require(bool(rows), "inventory posting without determination")
            total = Fraction()
            for change in rows:
                total = _add(total, _required_number(change, "units_delta"))
            _require(
                total == _required_number(posting, "amount"), "posting units mismatch"
            )
    for numbers in indices.values():
        _require(sorted(numbers) == list(range(len(numbers))), "posting index gap")


def _validate_allocations(tables: Tables, *, entity: str) -> None:
    allocations = _index(tables, "inventory_allocations", "allocation_id", entity)
    postings = _index(tables, "postings", "posting_id", entity)
    by_allocation: dict[str, list[Row]] = defaultdict(list)
    for row in tables["inventory_changes"]:
        if row["entity_id"] == entity:
            _slice(row)
            allocation = allocations[row["allocation_id"]]
            posting = postings[row["posting_id"]]
            _require(
                posting["step_id"] == allocation["step_id"]
                and posting["position_id"] == row["position_id"],
                "inventory determination disagrees with posting",
            )
            by_allocation[row["allocation_id"]].append(row)
    for identity, allocation in allocations.items():
        rows = by_allocation[identity]
        sources = [row for row in rows if row["change_role"] == "source"]
        targets = [row for row in rows if row["change_role"] == "target"]
        kind = allocation["allocation_kind"]
        if kind in {"opening", "acquisition"}:
            _require(len(sources) == 0 and len(targets) == 1, "acquisition shape")
        elif kind == "reduction":
            _require(len(sources) == 1 and len(targets) == 0, "reduction shape")
        else:
            _require(len(sources) == 1 and bool(targets), "allocation source shape")
        if kind in {"transfer", "pool_exit"}:
            _require(len(targets) == 1, "transfer target shape")
            for name in ("units", *COMPONENTS):
                values = [_number(row, name + "_delta") for row in rows]
                _require(
                    all(value is None for value in values)
                    or (
                        all(value is not None for value in values)
                        and sum(value for value in values if value is not None) == 0
                    ),
                    "transfer component conservation",
                )


def _validate_financial_rows(tables: Tables, *, entity: str) -> None:
    transactions = _index(tables, "transactions", "txn_id", entity)
    positions = _index(tables, "positions", "position_id", entity)
    _validate_postings(
        tables, entity=entity, transactions=transactions, positions=positions
    )
    _validate_allocations(tables, entity=entity)
    postings = _index(tables, "postings", "posting_id", entity)
    weights: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
    for row in tables["posting_weights"]:
        if row["entity_id"] == entity:
            posting = postings[row["posting_id"]]
            _require(posting["step_id"] is not None, "nonparticipating weight")
            key = (posting["txn_id"], row["commodity_id"])
            weights[key] = _add(weights[key], _required_number(row, "amount"))
    _require(all(value == 0 for value in weights.values()), "unbalanced weights")
    _validate_fee_lineage(tables, entity=entity)
    _validate_settlements(tables, entity=entity)
    _validate_reference_transfers(tables, entity=entity, positions=positions)
    replay_rows(tables, entity=entity)


def _validate_reference_transfers(
    tables: Tables, *, entity: str, positions: dict[str, Row]
) -> None:
    changes: dict[str, dict[str, Row]] = defaultdict(dict)
    for change in _rows_for(tables, "inventory_changes", entity):
        changes[change["allocation_id"]][change["change_role"]] = change
    references: dict[tuple[object, ...], list[Row]] = defaultdict(list)
    for change in _rows_for(tables, "reference_changes", entity):
        key = (
            change["step_id"],
            change["position_id"],
            change["lot_id"],
            change["change_kind"],
            _required_number(change, "units"),
        )
        references[key].append(change)
    for allocation in _rows_for(tables, "inventory_allocations", entity):
        if allocation["allocation_kind"] != "transfer":
            continue
        source = changes[allocation["allocation_id"]]["source"]
        target = changes[allocation["allocation_id"]]["target"]
        kinds = {
            positions[row["position_id"]]["measurement_kind"]
            for row in (source, target)
        }
        if "reference" not in kinds:
            continue
        _require(
            kinds == {"reference"}
            and source["lot_id"] is not None
            and target["lot_id"] is not None,
            "reference transfer requires two referenced lots",
        )
        step = allocation["step_id"]
        outgoing = references[
            (
                step,
                source["position_id"],
                source["lot_id"],
                "close",
                -_required_number(source, "units_delta"),
            )
        ]
        incoming = references[
            (
                step,
                target["position_id"],
                target["lot_id"],
                "entry",
                _required_number(target, "units_delta"),
            )
        ]
        _require(
            len(outgoing) == len(incoming) == 1,
            "reference transfer requires a paired close and entry",
        )
        before, after = outgoing[0], incoming[0]
        _require(
            before["reference_commodity_id"] == after["reference_commodity_id"]
            and before["terms_declaration_id"] == after["terms_declaration_id"]
            and _number(before, "reference_before") == _number(after, "reference_after")
            and before["settlement_reference_coefficient"] is None
            and after["settlement_reference_coefficient"] is None,
            "transferred reference changed",
        )


def _validate_fee_lineage(tables: Tables, *, entity: str) -> None:
    allocations = _index(tables, "fee_allocations", "fee_allocation_id", entity)
    changes = _index(tables, "inventory_changes", "change_id", entity)
    totals: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
    for row in _rows_for(tables, "inventory_fee_changes", entity):
        allocation = allocations[row["fee_allocation_id"]]
        _require(
            allocation["target_kind"] == "lot", "disposal fee in inventory lineage"
        )
        book = _number(row, "book_amount_delta")
        if book is not None:
            key = (row["change_id"], allocation["treatment"])
            totals[key] = _add(totals[key], book)
    for identity, change in changes.items():
        for treatment, component in (
            ("capitalized", "capitalized_fee_delta"),
            ("expensed", "expensed_fee_delta"),
        ):
            value = _number(change, component)
            if value is not None:
                _require(
                    totals[(identity, treatment)] == value,
                    "inventory fee component disagrees with original lineage",
                )


def _validate_settlements(tables: Tables, *, entity: str) -> None:
    postings = _index(tables, "postings", "posting_id", entity)
    totals: dict[str, Fraction] = defaultdict(Fraction)
    for row in _rows_for(tables, "reference_settlements", entity):
        posting = postings[row["settlement_posting_id"]]
        _require(
            posting["commodity_id"] == row["commodity_id"]
            and posting["step_id"] is not None,
            "settlement posting mismatch",
        )
        identity = row["settlement_posting_id"]
        totals[identity] = _add(totals[identity], _required_number(row, "amount"))
    for identity, amount in totals.items():
        _require(
            amount == _required_number(postings[identity], "amount"),
            "settlement attributions disagree with existing cash",
        )


def _canonical(value: object, *, integer_strings: bool) -> object:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return str(value) if integer_strings else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_canonical(item, integer_strings=integer_strings) for item in value]
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value), "non-string JSON key")
        normalized = {
            unicodedata.normalize("NFC", key): _canonical(
                item, integer_strings=integer_strings
            )
            for key, item in value.items()
        }
        _require(len(normalized) == len(value), "duplicate normalized JSON key")
        return normalized
    message = "non-JSON or floating-point canonical value"
    raise ReaderError(message)


def canonical_bytes(value: object, *, integer_strings: bool = True) -> bytes:
    return (
        json.dumps(
            _canonical(value, integer_strings=integer_strings),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def logical_table_digest(columns: list[Row], rows: list[Row], *, version: int) -> str:
    digest = hashlib.sha256(
        canonical_bytes({"columns": columns, "schema_version": version})
    )
    for row in rows:
        digest.update(canonical_bytes([row[column["name"]] for column in columns]))
    return digest.hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _reject_float(_value: str) -> object:
    message = "non-integer JSON number"
    raise ReaderError(message)


def _json(payload: bytes) -> Row:
    result = json.loads(
        payload,
        object_pairs_hook=_unique_pairs,
        parse_float=_reject_float,
        parse_constant=_reject_float,
    )
    _require(isinstance(result, dict), "expected JSON object")
    return result


def _pinned_file(root: Path, name: str, digest: str) -> bytes:
    _require(
        re.fullmatch(r"[a-z][a-z0-9_]*\.(json|parquet)", name) is not None,
        "unsafe artifact filename",
    )
    _require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "invalid digest")
    path = root / name
    _require(not path.is_symlink() and path.is_file(), "missing or symlink payload")
    with path.open("rb") as stream:
        content = stream.read(MAX_PAYLOAD_BYTES + 1)
    _require(len(content) <= MAX_PAYLOAD_BYTES, "payload size limit")
    _require(hashlib.sha256(content).hexdigest() == digest, "payload checksum mismatch")
    return content


def _local_schema(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"}:
                _require(
                    isinstance(item, str) and item.startswith("#"), "nonlocal schema"
                )
            _local_schema(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema(item)


@lru_cache(maxsize=16)
def _checked_validator(schema_bytes: bytes) -> Validator:
    schema = _json(schema_bytes)
    _local_schema(schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_json(document: object, schema: Row) -> None:
    try:
        _checked_validator(canonical_bytes(schema, integer_strings=False)).validate(
            document
        )
    except ValidationError as error:
        message = "document violates approved schema"
        raise ReaderError(message) from error


def _wire_integers(value: object, node: Row, schema: Row) -> object:
    if "$ref" in node:
        target = schema
        for segment in node["$ref"].removeprefix("#/").split("/"):
            target = target[segment]
        return _wire_integers(value, target, schema)
    variants = node.get("oneOf", node.get("anyOf"))
    if variants:
        for variant in variants:
            try:
                decoded = _wire_integers(value, variant, schema)
                Draft202012Validator({**variant, "$defs": schema["$defs"]}).validate(
                    decoded
                )
            except ReaderError, ValidationError:
                continue
            return decoded
        message = "wire value matches no schema variant"
        raise ReaderError(message)
    if node.get("type") == "integer" or type(node.get("const")) is int:
        _require(
            isinstance(value, str)
            and re.fullmatch(r"0|-?[1-9][0-9]*", value) is not None
            and len(value) <= 20,
            "invalid wire integer",
        )
        assert isinstance(value, str)
        return int(value)
    if isinstance(value, dict):
        return {
            key: _wire_integers(item, node.get("properties", {}).get(key, {}), schema)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_wire_integers(item, node.get("items", {}), schema) for item in value]
    return value


def _read_parquet(root: Path, schema: Row) -> dict[str, list[Row]]:
    physical = {
        "string": "VARCHAR",
        "integer": "BIGINT",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "list": "VARCHAR[]",
    }
    tables = {}
    with duckdb.connect(
        config={
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "allow_community_extensions": False,
        }
    ) as connection:
        connection.execute("SET allowed_directories = ?", [[str(root)]])
        connection.execute("SET enable_external_access = false")
        for specification in schema["tables"]:
            name = specification["name"]
            _require(
                re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None, "invalid table"
            )
            columns = specification["columns"]
            physical_fields = connection.execute(
                "SELECT name, type, repetition_type, num_children, converted_type "
                "FROM parquet_schema(?)",
                [str(root / (name + ".parquet"))],
            ).fetchall()
            _check_physical_fields(physical_fields, columns)
            relation = connection.execute(
                "SELECT * FROM read_parquet(?)", [str(root / (name + ".parquet"))]
            )
            _require(
                [(item[0], str(item[1])) for item in relation.description]
                == [(column["name"], physical[column["kind"]]) for column in columns],
                "Parquet column or physical type mismatch",
            )
            tables[name] = [
                {
                    column["name"]: value.isoformat() if type(value) is date else value
                    for column, value in zip(columns, values, strict=True)
                }
                for values in relation.fetchall()
            ]
    return tables


def _check_physical_fields(fields: list[tuple], columns: list[Row]) -> None:
    _require(
        fields[0][1:4] == (None, "REQUIRED", len(columns)),
        "invalid Parquet root",
    )
    leaves = {
        "string": ("BYTE_ARRAY", "UTF8"),
        "integer": ("INT64", None),
        "boolean": ("BOOLEAN", None),
        "date": ("INT32", "DATE"),
    }
    index = 1
    for column in columns:
        _require(index < len(fields), "missing physical Parquet column")
        name, physical, repetition, children, annotation = fields[index]
        index += 1
        _require(
            name == column["name"]
            and repetition == ("OPTIONAL" if column["nullable"] else "REQUIRED"),
            "Parquet field name or nullability mismatch",
        )
        if column["kind"] == "list":
            _require(
                (physical, children, annotation) == (None, 1, "LIST")
                and index + 1 < len(fields)
                and fields[index][1:] == (None, "REPEATED", 1, None)
                and fields[index + 1][1:] == ("BYTE_ARRAY", "REQUIRED", None, "UTF8"),
                "Parquet list shape or element nullability mismatch",
            )
            index += 2
        else:
            _require(
                (physical, annotation) == leaves[column["kind"]] and children is None,
                "Parquet physical encoding mismatch",
            )
    _require(index == len(fields), "extra physical Parquet columns")


@dataclass(frozen=True)
class Snapshot:
    tables: dict[str, list[Row]]
    schema: Row
    manifest: Row
    financial_digest: str


def _artifact_documents(
    root: Path,
    descriptor_digest: str,
    expected_version: int,
    expected_schema_digests: Mapping[str, str],
) -> tuple[Row, Row, Row, dict[str, Row]]:
    _require(
        set(expected_schema_digests) == SCHEMA_FILES,
        "complete independently approved schema pins required",
    )
    _require(not root.is_symlink() and root.is_dir(), "unsafe snapshot root")
    descriptor_bytes = _pinned_file(root, "descriptor.json", descriptor_digest)
    descriptor = _json(descriptor_bytes)
    _require(descriptor_bytes == canonical_bytes(descriptor), "noncanonical descriptor")
    _require(
        set(descriptor)
        == {"schema_version", "manifest_path", "manifest_digest", "payloads"},
        "descriptor fields",
    )
    _require(
        type(expected_version) is int
        and expected_version == 1
        and descriptor["schema_version"] == str(expected_version),
        "unsupported schema version",
    )
    _require(descriptor["manifest_path"] == "manifest.json", "manifest filename")
    manifest_bytes = _pinned_file(
        root, descriptor["manifest_path"], descriptor["manifest_digest"]
    )
    manifest = _json(manifest_bytes)
    _require(manifest_bytes == canonical_bytes(manifest), "noncanonical manifest")
    _require(
        manifest["schema_version"] == str(expected_version), "mixed schema versions"
    )
    paths = [entry["path"] for entry in descriptor["payloads"]]
    _require(paths == sorted(set(paths)), "duplicate or unsorted inventory")
    _require(
        {path.name for path in root.iterdir()}
        == {*paths, "descriptor.json", "manifest.json"},
        "unexpected artifact inventory",
    )
    payloads = {}
    for entry in descriptor["payloads"]:
        _require(set(entry) == {"path", "byte_digest"}, "payload declaration fields")
        payloads[entry["path"]] = _pinned_file(
            root, entry["path"], entry["byte_digest"]
        )
    for name, digest in expected_schema_digests.items():
        _require(
            name in payloads and hashlib.sha256(payloads[name]).hexdigest() == digest,
            "unapproved schema semantics",
        )
        _require(
            payloads[name]
            == canonical_bytes(_json(payloads[name]), integer_strings=False),
            "noncanonical schema",
        )
    schema = _json(payloads["schema.json"])
    _require(
        payloads["schema.json"] == canonical_bytes(schema, integer_strings=False),
        "noncanonical schema",
    )
    _require(schema["schema_version"] == expected_version, "mixed schema versions")
    documents = {
        name: _json(payload)
        for name, payload in payloads.items()
        if name.endswith(".json")
    }
    for name, document in (
        ("descriptor_schema.json", descriptor),
        ("manifest_schema.json", manifest),
    ):
        _require(name in documents, "missing publication schema")
        _local_schema(documents[name])
        decoded = _wire_integers(document, documents[name], documents[name])
        validate_json(decoded, documents[name])
        _require(isinstance(decoded, dict), "decoded publication object")
        document.clear()
        document.update(cast("Row", decoded))
    return descriptor, manifest, schema, documents


def _verify_manifest(
    descriptor: Row, manifest: Row, schema: Row, tables: dict[str, list[Row]]
) -> None:
    payloads = {entry["path"]: entry["byte_digest"] for entry in descriptor["payloads"]}
    _require(
        len({entry["schema_id"] for entry in manifest["schemas"]})
        == len(manifest["schemas"]),
        "duplicate schema identity",
    )
    for entry in manifest["schemas"]:
        _require(
            entry["byte_digest"] == payloads[entry["path"]]
            and entry["schema_version"] == 1
            and entry["path"] == entry["schema_id"] + ".json",
            "manifest schema mismatch",
        )
    entries = manifest["tables"]
    names = [entry["name"] for entry in entries]
    _require(
        len(names) == len(set(names)) and set(names) == set(tables) - {"build"},
        "manifest table inventory",
    )
    specifications = {table["name"]: table for table in schema["tables"]}
    for entry in entries:
        name = entry["name"]
        _require(entry["path"] == name + ".parquet", "table path mismatch")
        _require(
            entry["byte_digest"] == payloads[entry["path"]], "mixed payload generation"
        )
        _require(entry["row_count"] == len(tables[name]), "table row count mismatch")
        _require(
            entry["logical_digest"]
            == logical_table_digest(
                specifications[name]["columns"], tables[name], version=1
            ),
            "logical table digest mismatch",
        )
    _require(
        canonical_bytes(manifest["entities"])
        == canonical_bytes(
            [
                {key: value for key, value in row.items() if key != "manifest_digest"}
                for row in tables["build"]
            ]
        ),
        "build metadata mismatch",
    )
    for row in tables["build"]:
        _require(
            row["manifest_digest"] == descriptor["manifest_digest"]
            and row["schema_digest"] == payloads["schema.json"],
            "mixed build generation",
        )


def read_snapshot(
    root: Path,
    *,
    descriptor_digest: str,
    expected_schema_digests: Mapping[str, str],
    expected_version: int = 1,
) -> Snapshot:
    """Require independently approved schema pins, not self-certified versions."""
    descriptor, manifest, schema, documents = _artifact_documents(
        root, descriptor_digest, expected_version, expected_schema_digests
    )
    tables = _read_parquet(root.resolve(), schema)
    _verify_manifest(descriptor, manifest, schema, tables)
    resources = _verify_resources(descriptor, manifest, documents, tables)
    validate_rows(
        tables,
        schema,
        expected_version=expected_version,
        source_sequence_directions=_sequence_directions(manifest, resources),
    )
    bindings = [
        {
            **binding,
            "account_mappings": sorted(
                binding["account_mappings"], key=canonical_bytes
            ),
        }
        for binding in manifest["bindings"]
    ]
    financial = {
        "entities": [[row["entity_id"], row["as_of"]] for row in tables["build"]],
        "schemas": {
            name.removesuffix(".json"): documents[name] for name in sorted(SCHEMA_FILES)
        },
        "bindings": sorted(bindings, key=canonical_bytes),
        "tables": [
            {"name": item["name"], "logical_digest": item["logical_digest"]}
            for item in manifest["tables"]
        ],
        "resources": resources,
        **{
            name: manifest[name]
            for name in (
                "effective_declarations",
                "derivations",
                "checks",
            )
        },
    }
    digest = hashlib.sha256(canonical_bytes(financial)).hexdigest()
    _require(digest == manifest["logical_content_digest"], "financial digest mismatch")
    return Snapshot(tables, schema, manifest, digest)


def _verify_resources(
    descriptor: Row, manifest: Row, documents: dict[str, Row], tables: Tables
) -> dict[str, Row]:
    payloads = {entry["path"]: entry["byte_digest"] for entry in descriptor["payloads"]}
    schemas = {entry["path"] for entry in manifest["schemas"]}
    resources = {
        name: value for name, value in documents.items() if name not in schemas
    }
    _require(schemas == SCHEMA_FILES, "unsupported schema resource inventory")
    entries = [
        *manifest["tables"],
        *manifest["artifacts"],
        *manifest["schemas"],
        *manifest["registries"],
        manifest["configuration"],
    ]
    paths = [entry["path"] for entry in entries]
    _require(len(set(paths)) == len(paths), "duplicate manifest payload")
    _require(set(payloads) == {*paths, "build.parquet"}, "manifest payload inventory")
    for entry in entries:
        _require(
            payloads[entry["path"]] == entry["byte_digest"],
            "resource checksum mismatch",
        )
    configuration = manifest["configuration"]
    _require(
        payloads[configuration["path"]] == configuration["byte_digest"],
        "configuration checksum mismatch",
    )
    validate_json(
        resources[configuration["path"]], documents["configuration_schema.json"]
    )
    _verify_source_authority(resources[configuration["path"]], tables)
    for registry in manifest["registries"]:
        _require(
            payloads[registry["path"]] == registry["byte_digest"],
            "registry checksum mismatch",
        )
        registry_schema = documents["registry_schema.json"]
        decoded = resources[registry["path"]]
        validate_json(decoded, registry_schema)
        _require(
            registry["schema_id"] == "registry_schema"
            and registry["content_digest"]
            == hashlib.sha256(canonical_bytes(resources[registry["path"]])).hexdigest(),
            "registry semantics mismatch",
        )
    for row in tables["declarations"]:
        validate_json(
            _json(row["payload_json"].encode()), documents["declaration_schema.json"]
        )
    _verify_provenance(manifest, tables, resources[configuration["path"]])
    checks = {row["check_id"]: row for row in manifest["checks"]}
    _require(len(checks) == len(manifest["checks"]), "duplicate publication check")
    for row in checks.values():
        _require(
            row["severity"] != "error" or row["status"] == "pass",
            "failed error finding",
        )
    for output in manifest["outputs"]:
        for identity in output["required_checks"]:
            _require(
                identity in checks and checks[identity]["status"] == "pass",
                "required check did not pass",
            )
    _require(
        set(payloads) == set(documents) | {name + ".parquet" for name in tables},
        "undeclared schema-bearing payload",
    )
    return resources


def _verify_source_authority(configuration: Row, tables: Tables) -> None:
    scopes = {
        table: {(row["entity_id"], row[key]) for row in tables[table]}
        for table, key in (
            ("accounts", "account_id"),
            ("commodities", "commodity_id"),
            ("source_records", "source_record_id"),
            ("statements", "statement_id"),
        )
    }
    intervals: dict[tuple[object, ...], list[Row]] = defaultdict(list)
    for entry in configuration["source_authority"]:
        scope = entry["scope"]
        action = entry["fact_kind"] == "action"
        _require(
            scope["table"] == ("commodities" if action else "accounts")
            and len(scope["key"]) == 1
            and (entry["entity_id"], scope["key"][0]) in scopes[scope["table"]]
            and (entry["coverage_basis_id"] is None) == action
            and entry["period_start"] <= entry["period_end"],
            "invalid source authority scope",
        )
        for source in entry["authoritative_sources"]:
            _require(
                source["table"] in {"source_records", "statements"}
                and len(source["key"]) == 1
                and (entry["entity_id"], source["key"][0]) in scopes[source["table"]],
                "invalid authoritative source",
            )
        identity = (
            entry["entity_id"],
            scope["table"],
            tuple(scope["key"]),
            entry["fact_kind"],
            entry["coverage_basis_id"],
        )
        _require(
            all(
                entry["period_start"] > previous["period_end"]
                or previous["period_start"] > entry["period_end"]
                for previous in intervals[identity]
            ),
            "overlapping source authority",
        )
        intervals[identity].append(entry)


def _sequence_directions(
    manifest: Row, resources: dict[str, Row]
) -> dict[tuple[str, str, str], str]:
    directions: dict[tuple[str, str, str], str] = {}
    for entry in manifest["registries"]:
        for sequence in resources[entry["path"]]["source_sequences"]:
            key = (
                sequence["source_scope_id"],
                sequence["sequence_domain"],
                sequence["field_name"],
            )
            _require(key not in directions, "ambiguous source sequence direction")
            directions[key] = sequence["direction"]
    return directions


def _verify_provenance(manifest: Row, tables: Tables, configuration: Row) -> None:
    _require(
        manifest["outputs"] == configuration["outputs"],
        "configuration publication outputs disagree",
    )
    _require(
        all(row["as_of"] == configuration["as_of"] for row in tables["build"]),
        "configuration as-of disagrees",
    )
    declarations = {
        (row["entity_id"], row["declaration_id"]): row for row in tables["declarations"]
    }
    effective = [
        (row["entity_id"], row["declaration_id"])
        for row in manifest["effective_declarations"]
    ]
    _require(
        len(effective) == len(set(effective))
        and all(key in declarations for key in effective),
        "invalid effective declaration",
    )
    active_keys = [
        (entity, declarations[(entity, identity)]["declaration_key"])
        for entity, identity in effective
    ]
    _require(
        len(active_keys) == len(set(active_keys)),
        "multiple active declaration revisions",
    )
    _verify_custody(manifest, tables)
    _verify_decision_evidence(manifest, tables, declarations, effective)


def _verify_custody(manifest: Row, tables: Tables) -> None:
    entities = {row["entity_id"] for row in tables["build"]}
    bindings = [
        (row["entity_id"], row["source_scope_id"]) for row in manifest["bindings"]
    ]
    _require(
        len(bindings) == len(set(bindings))
        and all(entity in entities for entity, _ in bindings),
        "invalid source binding",
    )
    retained = manifest["recovery_set"]["vault_objects"]
    objects = {row["object_id"]: row for row in retained}
    _require(len(objects) == len(retained), "duplicate retained object")
    for identity, row in objects.items():
        _require(
            row["digest_algorithm"] == "sha256"
            and row["digest"] == identity
            and re.fullmatch(r"[0-9a-f]{64}", identity) is not None
            and row["size_bytes"] >= 0,
            "invalid source custody digest",
        )
    for row in manifest["recovery_set"]["accession_records"]:
        _require(row["object_id"] in objects, "dangling accession object")
    for row in tables["source_records"]:
        _require(
            row["source_blob_digest"] in objects
            and (row["entity_id"], row["source_scope_id"]) in bindings,
            "missing source custody",
        )
    for row in tables["commodity_symbols"]:
        _require(
            row["source_scope_id"] is None
            or (row["entity_id"], row["source_scope_id"]) in bindings,
            "missing symbol source binding",
        )


def _verify_decision_evidence(
    manifest: Row,
    tables: Tables,
    declarations: dict[tuple[str, str], Row],
    effective: list[tuple[str, str]],
) -> None:
    entities = {row["entity_id"] for row in tables["build"]}
    derivations = {row["derivation_id"]: row for row in manifest["derivations"]}
    _require(len(derivations) == len(manifest["derivations"]), "duplicate derivation")
    for row in tables["provenance"]:
        _require(
            row["derivation_id"] is None or row["derivation_id"] in derivations,
            "undeclared provenance derivation",
        )
        if row["value_origin"] == "correction":
            _require(
                row["evidence_kind"] == "declaration"
                and declarations[(row["entity_id"], row["evidence_id"])][
                    "declaration_kind"
                ]
                == "correction",
                "correction provenance is not a correction declaration",
            )
    for row in manifest["checks"]:
        _require(row["entity_id"] in entities, "unknown publication check entity")
        for identity in row["policy_declaration_ids"]:
            _require(
                (row["entity_id"], identity) in effective,
                "inactive publication check policy",
            )
        if row["exception_declaration_id"] is not None:
            _require(
                (row["entity_id"], row["exception_declaration_id"]) in effective,
                "inactive publication exception",
            )
        if row["check_kind"] in {
            "schema",
            "identity",
            "arithmetic",
            "referential_integrity",
        }:
            _require(
                row["status"] == "pass" and row["exception_declaration_id"] is None,
                "nonwaivable publication check",
            )
