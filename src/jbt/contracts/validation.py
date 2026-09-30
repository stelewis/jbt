"""Strict complete-table validation; no missing determination is synthesized."""

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from fractions import Fraction

from jbt.contracts.catalog import schema_document
from jbt.contracts.declarations import decode_declaration
from jbt.contracts.primitives import ContractError, number, require, validate_number
from jbt.domain.identity import (
    AcquiredAnchor,
    AuthoredAnchor,
    Occurrence,
    child_id,
    declaration_id,
    event_id,
    revision_id,
    successor_lot_id,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey

type Row = dict
type Tables = Mapping[str, list[Row]]

MAX_OFFSET_MINUTES = 1439


def _text_scalar(value: object, kind: str, location: str) -> str:
    if not isinstance(value, str):
        code = "physical_type"
        raise ContractError(code, location)
    require(unicodedata.normalize("NFC", value) == value, "text_nfc", location)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "text_utf8"
        raise ContractError(msg, location) from error
    if kind == "date":
        require(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
            "date_encoding",
            location,
        )
        try:
            date.fromisoformat(value)
        except ValueError as error:
            msg = "date_value"
            raise ContractError(msg, location) from error
    return value


def _scalar(value: object, column: dict, location: str) -> None:
    if value is None:
        require(column["nullable"], "required_value", location)
        return
    kind = column["kind"]
    if kind in {"string", "date"}:
        value = _text_scalar(value, kind, location)
        if column["name"].endswith(("_id", "_key", "_digest")):
            require(bool(value), "empty_identifier", location)
    elif kind == "integer":
        require(
            type(value) is int and -(2**63) <= value < 2**63, "physical_type", location
        )
    elif kind == "boolean":
        require(type(value) is bool, "physical_type", location)
    elif kind == "list":
        if not isinstance(value, list):
            code = "physical_type"
            raise ContractError(code, location)
        for item in value:
            _scalar(
                item, {"name": "item_id", "kind": "string", "nullable": False}, location
            )
        require(value == sorted(set(value)), "list_order_unique", location)
    if "enum" in column:
        require(value in column["enum"], "enum_value", location)


def _key(row: Row, columns: list[str]) -> tuple:
    return tuple(row[name] for name in columns)


def _sort_key(row: Row, columns: list[str]) -> tuple:
    return tuple((row[name] is not None, row[name]) for name in columns)


def _structure(tables: Tables, specs: dict) -> dict:  # noqa: C901
    require(set(tables) == set(specs), "complete_table_set", "snapshot")
    indexes = {}
    for name, spec in specs.items():
        rows = tables[name]
        require(type(rows) is list, "table_rows_type", name)
        expected = {c["name"] for c in spec["columns"]}
        keys = set()
        unique_sets = [set() for _ in spec["unique"]]
        previous = None
        indexes[name] = {}
        for index, row in enumerate(rows):
            location = f"{name}[{index}]"
            require(type(row) is dict and set(row) == expected, "closed_row", location)
            for column in spec["columns"]:
                _scalar(row[column["name"]], column, f"{location}.{column['name']}")
            for group in spec["numeric_groups"]:
                validate_number(
                    {
                        suffix: row[f"{group['name']}_{suffix}"]
                        for suffix in ("coefficient", "scale", "source_scale")
                    },
                    f"{location}.{group['name']}",
                )
            primary = _key(row, spec["primary_key"])
            require(primary not in keys, "primary_key_unique", location)
            keys.add(primary)
            indexes[name][primary] = row
            for unique, seen in zip(spec["unique"], unique_sets, strict=True):
                key = _key(row, unique)
                require(key not in seen, "composite_key_unique", location)
                seen.add(key)
            ordered = _sort_key(row, spec["sort_key"])
            require(previous is None or previous <= ordered, "row_order", location)
            previous = ordered
            for field in (
                "posting_index",
                "effect_index",
                "sequence",
                "display_precision",
            ):
                if field in row and row[field] is not None:
                    require(row[field] >= 0, "nonnegative_index", location)
            if name == "build":
                require(row["schema_version"] == 1, "schema_version", location)
    entities = {row["entity_id"] for row in tables["build"]}
    for name, rows in tables.items():
        for index, row in enumerate(rows):
            require(row["entity_id"] in entities, "entity_build", f"{name}[{index}]")
            for reference in specs[name]["foreign_keys"]:
                key = _key(row, reference["columns"])
                if key[-1] is not None:
                    require(
                        key in indexes[reference["table"]],
                        "foreign_key",
                        f"{name}[{index}].{reference['columns'][-1]}",
                    )
    return indexes


def _lookup(indexes: dict, table: str, row: Row, field: str) -> Row:
    return indexes[table][(row["entity_id"], row[field])]


def _required_number(row: Row, field: str, location: str) -> Fraction:
    value = number(row, field)
    if value is None:
        code = "required_value"
        raise ContractError(code, location)
    return value


def _together(row: Row, fields: tuple[str, ...], location: str) -> None:
    present = [row[field] is not None for field in fields]
    require(all(present) or not any(present), "required_combination", location)


def _time(row: Row, location: str) -> None:
    if "timestamp_date" not in row:
        return
    names = (
        "timestamp_date",
        "timestamp_local",
        "timestamp_offset_minutes",
        "timestamp_zone",
        "timestamp_precision",
        "timestamp_fraction_digits",
    )
    if all(row[name] is None for name in names):
        return
    precision = row["timestamp_precision"]
    require(
        row["timestamp_date"] is not None and precision is not None,
        "time_bundle",
        location,
    )
    local, offset, digits = (
        row["timestamp_local"],
        row["timestamp_offset_minutes"],
        row["timestamp_fraction_digits"],
    )
    if precision == "date":
        require(
            local is None and offset is None and digits is None,
            "date_only_time",
            location,
        )
        return
    patterns = {
        "minute": r"([01][0-9]|2[0-3]):[0-5][0-9]",
        "second": r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]",
        "fractional_second": r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]+",
    }
    require(
        isinstance(local, str) and re.fullmatch(patterns[precision], local) is not None,
        "time_precision",
        location,
    )
    require(
        offset is None or -MAX_OFFSET_MINUTES <= offset <= MAX_OFFSET_MINUTES,
        "time_offset",
        location,
    )
    if precision == "fractional_second":
        require(
            type(digits) is int and digits == len(local.split(".")[1]),
            "time_fraction_digits",
            location,
        )
    else:
        require(digits is None, "time_fraction_digits", location)


def _contiguous(
    rows: list[Row], groups: tuple[str, ...], field: str, location: str
) -> None:
    grouped = defaultdict(list)
    for row in rows:
        grouped[_key(row, ["entity_id", *groups])].append(row[field])
    for values in grouped.values():
        require(
            sorted(values) == list(range(len(values))), "contiguous_order", location
        )


def _sequence_observation(
    step: Row, tables: Tables, indexes: dict
) -> tuple[str, str, Fraction]:
    names = {"source.sequence", "source.sequence_domain"}
    entity = step["entity_id"]
    step_id = json.dumps([step["step_id"]], ensure_ascii=False, separators=(",", ":"))
    txn_id = json.dumps([step["txn_id"]], ensure_ascii=False, separators=(",", ":"))
    step_rows = [
        row
        for row in tables["observations"]
        if row["entity_id"] == entity
        and row["record_kind"] == "book_steps"
        and row["record_id"] == step_id
        and row["field_name"] in names
    ]
    transaction_rows = [
        row
        for row in tables["observations"]
        if row["entity_id"] == entity
        and row["record_kind"] == "transactions"
        and row["record_id"] == txn_id
        and row["field_name"] in names
    ]
    require(
        not (step_rows and transaction_rows),
        "source_sequence_ambiguous",
        "observations",
    )
    rows = step_rows or transaction_rows
    by_name = {row["field_name"]: row for row in rows}
    require(
        len(rows) == len(by_name) == len(names)
        and len({row["source_record_id"] for row in rows}) == 1,
        "source_sequence_observation",
        "observations",
    )
    sequence = by_name["source.sequence"]
    domain = by_name["source.sequence_domain"]
    require(
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
        "source_sequence_observation",
        "observations",
    )
    source = indexes["source_records"][(entity, sequence["source_record_id"])]
    return (
        source["source_scope_id"],
        domain["text_value"],
        _required_number(sequence, "decimal_value", "observations"),
    )


def _source_sequence_edge(
    before: Row, after: Row, tables: Tables, indexes: dict
) -> None:
    left = _sequence_observation(before, tables, indexes)
    right = _sequence_observation(after, tables, indexes)
    require(
        left[:2] == right[:2],
        "source_sequence_domain",
        "book_step_dependencies",
    )
    require(
        left[2] != right[2],
        "source_sequence_distinct",
        "book_step_dependencies",
    )


def validate_source_sequence_directions(tables: Tables, registries: list[dict]) -> None:
    """Bind comparable source edges to their retained direction rules."""
    definitions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for registry in registries:
        for rule in registry["source_sequences"]:
            definitions[(rule["source_scope_id"], rule["sequence_domain"])].append(rule)
    steps = {(row["entity_id"], row["step_id"]): row for row in tables["book_steps"]}
    sources = {
        (row["entity_id"], row["source_record_id"]): row
        for row in tables["source_records"]
    }
    indexes = {"source_records": sources}
    for edge in tables["book_step_dependencies"]:
        if edge["constraint_kind"] != "source_sequence":
            continue
        entity = edge["entity_id"]
        before = steps[(entity, edge["before_step_id"])]
        after = steps[(entity, edge["after_step_id"])]
        left = _sequence_observation(before, tables, indexes)
        right = _sequence_observation(after, tables, indexes)
        require(
            left[:2] == right[:2],
            "source_sequence_domain",
            "book_step_dependencies",
        )
        rules = definitions[left[:2]]
        require(
            len(rules) == 1 and rules[0]["field_name"] == "source.sequence",
            "source_sequence_registry",
            "registries.source_sequences",
        )
        direction = rules[0]["direction"]
        require(
            (left[2] < right[2]) if direction == "ascending" else (left[2] > right[2]),
            "source_sequence_direction",
            "book_step_dependencies",
        )


def _step_dependencies(tables: Tables, indexes: dict) -> None:
    for edge in tables["book_step_dependencies"]:
        before = _lookup(indexes, "book_steps", edge, "before_step_id")
        after = _lookup(indexes, "book_steps", edge, "after_step_id")
        require(
            before["sequence"] < after["sequence"],
            "precedence_forward",
            "book_step_dependencies",
        )
        if edge["constraint_kind"] == "decision":
            require(
                edge["declaration_id"] is not None,
                "ordering_declaration",
                "book_step_dependencies",
            )
            declaration = _lookup(indexes, "declarations", edge, "declaration_id")
            require(
                declaration["declaration_kind"] == "ordering",
                "ordering_declaration",
                "book_step_dependencies",
            )
        if edge["constraint_kind"] == "source_sequence":
            _source_sequence_edge(before, after, tables, indexes)


def _participation(tables: Tables, indexes: dict) -> None:
    _contiguous(tables["postings"], ("txn_id",), "posting_index", "postings")
    _contiguous(tables["book_steps"], (), "sequence", "book_steps")
    _contiguous(
        tables["corporate_action_effects"],
        ("action_id",),
        "effect_index",
        "corporate_action_effects",
    )
    for step in tables["book_steps"]:
        event = _lookup(indexes, "transactions", step, "txn_id")
        require(
            event["event_state"] == "recognized" and event["event_kind"] != "note",
            "recognized_steps",
            "book_steps",
        )
    _step_dependencies(tables, indexes)
    for row in tables["postings"]:
        event = _lookup(indexes, "transactions", row, "txn_id")
        require(event["event_kind"] != "note", "note_financial_leg", "postings")
        recognized = event["event_state"] == "recognized"
        require(
            (row["step_id"] is not None) == recognized,
            "posting_participation",
            "postings",
        )
        if recognized:
            step = _lookup(indexes, "book_steps", row, "step_id")
            require(step["txn_id"] == row["txn_id"], "posting_step_event", "postings")
        if row["leg_kind"] == "position":
            require(
                row["position_id"] is not None
                and row["account_id"] is not None
                and row["category_id"] is None,
                "position_leg",
                "postings",
            )
            position = _lookup(indexes, "positions", row, "position_id")
            require(
                position["account_id"] == row["account_id"]
                and position["commodity_id"] == row["commodity_id"],
                "position_leg_identity",
                "postings",
            )
        else:
            require(
                row["position_id"] is None and row["account_id"] is None,
                "boundary_leg",
                "postings",
            )
        for field in ("authorized", "traded", "posted", "settled", "value"):
            require(
                (row[f"date_{field}"] is not None)
                == (row[f"date_{field}_mode"] == "value"),
                "date_override",
                "postings",
            )
        for fields in (
            ("price_per_unit_coefficient", "price_commodity_id"),
            ("original_amount_coefficient", "original_commodity_id"),
            (
                "fx_rate_as_stated_coefficient",
                "fx_base_commodity_id",
                "fx_quote_commodity_id",
            ),
        ):
            _together(row, fields, "postings")
    totals = defaultdict(Fraction)
    for weight in tables["posting_weights"]:
        posting = _lookup(indexes, "postings", weight, "posting_id")
        require(
            posting["step_id"] is not None, "inactive_determination", "posting_weights"
        )
        totals[(weight["entity_id"], posting["txn_id"], weight["commodity_id"])] += (
            _required_number(weight, "amount", "posting_weights")
        )
    require(
        all(total == 0 for total in totals.values()),
        "weight_conservation",
        "posting_weights",
    )


def _positions_and_lineage(tables: Tables, indexes: dict) -> None:
    for row in tables["positions"]:
        declaration = _lookup(indexes, "declarations", row, "declaration_id")
        require(
            declaration["declaration_kind"] == "position"
            and row["position_id"]
            == declaration_id(
                EntityId(row["entity_id"]),
                RecordKind.POSITION,
                SemanticKey(declaration["declaration_key"]),
            ).value,
            "position_identity",
            "positions",
        )
        require(
            (row["counterparty_id"] is None) == (row["position_kind"] == "property"),
            "position_counterparty",
            "positions",
        )
        require(
            (row["measurement_kind"] == "quantity")
            == (row["inventory_method"] == "quantity"),
            "quantity_inventory",
            "positions",
        )
        if row["measurement_kind"] == "reference":
            require(
                row["position_kind"] == "contract"
                and row["inventory_method"] == "individual"
                and row["terms_declaration_id"] is not None,
                "reference_position",
                "positions",
            )
    for row in tables["inventory_pools"]:
        position = _lookup(indexes, "positions", row, "position_id")
        declaration = _lookup(indexes, "declarations", row, "declaration_id")
        require(
            declaration["declaration_kind"] == "pool"
            and row["pool_id"]
            == declaration_id(
                EntityId(row["entity_id"]),
                RecordKind.POOL,
                SemanticKey(declaration["declaration_key"]),
            ).value,
            "pool_identity",
            "inventory_pools",
        )
        require(
            position["measurement_kind"] == "cost"
            and position["inventory_method"] == "pooled",
            "pool_position",
            "inventory_pools",
        )
    for row in tables["lots"]:
        require(
            (row["origin_posting_id"] is None) != (row["parent_lot_id"] is None),
            "lot_root_or_branch",
            "lots",
        )
        if row["parent_lot_id"] is not None:
            parent = _lookup(indexes, "lots", row, "parent_lot_id")
            require(
                row["acquisition_date"] == parent["acquisition_date"]
                and row["book_commodity_id"] == parent["book_commodity_id"],
                "lot_lineage_continuity",
                "lots",
            )
        else:
            posting = _lookup(indexes, "postings", row, "origin_posting_id")
            require(
                posting["step_id"] == row["creation_step_id"]
                and posting["commodity_id"] == row["commodity_id"],
                "lot_origin",
                "lots",
            )
    for row in tables["opening_lots"]:
        lot = _lookup(indexes, "lots", row, "lot_id")
        require(lot["origin_posting_id"] is not None, "opening_root", "opening_lots")
        posting = _lookup(indexes, "postings", lot, "origin_posting_id")
        event = _lookup(indexes, "transactions", posting, "txn_id")
        require(
            event["event_kind"] == "opening" and event["event_state"] == "recognized",
            "opening_event",
            "opening_lots",
        )


def _acyclic_lineage(tables: Tables, indexes: dict) -> None:
    for table, id_field, parent_field in (
        ("lots", "lot_id", "parent_lot_id"),
        ("categories", "category_id", "parent_category_id"),
    ):
        for row in tables[table]:
            seen = {row[id_field]}
            current = row
            while current[parent_field] is not None:
                parent = _lookup(indexes, table, current, parent_field)
                require(parent[id_field] not in seen, "lineage_cycle", table)
                if table == "categories":
                    require(
                        parent["category_type"] == row["category_type"],
                        "category_parent_type",
                        table,
                    )
                seen.add(parent[id_field])
                current = parent


def _lot_identities(tables: Tables, indexes: dict) -> None:
    for row in tables["lots"]:
        entity = EntityId(row["entity_id"])
        output = SemanticKey(row["origin_key"])
        if row["origin_posting_id"] is not None:
            canonical = child_id(
                RecordId(entity, RecordKind.LEG, row["origin_posting_id"]),
                RecordKind.LOT,
                output,
            )
        else:
            root = row
            while root["parent_lot_id"] is not None:
                root = _lookup(indexes, "lots", root, "parent_lot_id")
            step = _lookup(indexes, "book_steps", row, "creation_step_id")
            canonical = successor_lot_id(
                RecordId(entity, RecordKind.LOT, root["lot_id"]),
                RecordId(entity, RecordKind.EVENT, step["txn_id"]),
                output,
            )
        require(row["lot_id"] == canonical.value, "lot_identity", "lots")


def _inventory(tables: Tables, indexes: dict) -> dict:
    by_allocation = defaultdict(list)
    by_posting = defaultdict(Fraction)
    for row in tables["inventory_changes"]:
        by_allocation[(row["entity_id"], row["allocation_id"])].append(row)
        by_posting[(row["entity_id"], row["posting_id"])] += _required_number(
            row, "units_delta", "inventory_changes"
        )
        require(
            (row["lot_id"] is None) != (row["pool_id"] is None),
            "inventory_slice",
            "inventory_changes",
        )
        position = _lookup(indexes, "positions", row, "position_id")
        posting = _lookup(indexes, "postings", row, "posting_id")
        allocation = _lookup(indexes, "inventory_allocations", row, "allocation_id")
        require(
            posting["position_id"] == row["position_id"]
            and posting["step_id"] == allocation["step_id"],
            "inventory_posting_step",
            "inventory_changes",
        )
        require(
            position["measurement_kind"] != "quantity",
            "quantity_no_lots",
            "inventory_changes",
        )
        if row["lot_id"] is not None:
            lot = _lookup(indexes, "lots", row, "lot_id")
            require(
                lot["commodity_id"] == position["commodity_id"],
                "lot_commodity",
                "inventory_changes",
            )
            require(
                (lot["book_commodity_id"] is not None)
                == (position["measurement_kind"] == "cost"),
                "lot_denomination",
                "inventory_changes",
            )
        else:
            pool = _lookup(indexes, "inventory_pools", row, "pool_id")
            require(
                pool["position_id"] == row["position_id"],
                "pool_position",
                "inventory_changes",
            )
        if position["measurement_kind"] == "reference":
            require(
                all(
                    row[f"{field}_coefficient"] is None
                    for field in (
                        "principal_delta",
                        "capitalized_fee_delta",
                        "expensed_fee_delta",
                    )
                ),
                "reference_no_cost",
                "inventory_changes",
            )
    for posting in tables["postings"]:
        if posting["step_id"] is None or posting["position_id"] is None:
            continue
        position = _lookup(indexes, "positions", posting, "position_id")
        if position["measurement_kind"] != "quantity":
            key = (posting["entity_id"], posting["posting_id"])
            require(
                key in by_posting and by_posting[key] == number(posting, "amount"),
                "inventory_posting_units",
                "inventory_changes",
            )
    return by_allocation


def _allocation_shapes(tables: Tables, indexes: dict, by_allocation: dict) -> None:
    for allocation in tables["inventory_allocations"]:
        rows = by_allocation[(allocation["entity_id"], allocation["allocation_id"])]
        sources = [r for r in rows if r["change_role"] == "source"]
        targets = [r for r in rows if r["change_role"] == "target"]
        kind = allocation["allocation_kind"]
        counts = {
            "acquisition": (0, 1),
            "opening": (0, 1),
            "reduction": (1, 0),
            "transfer": (1, 1),
            "pool_entry": (1, 1),
            "pool_exit": (1, 1),
        }
        if kind == "transformation":
            require(
                len(sources) == 1 and len(targets) >= 1,
                "allocation_shape",
                "inventory_allocations",
            )
        else:
            require(
                (len(sources), len(targets)) == counts[kind],
                "allocation_shape",
                "inventory_allocations",
            )
        if kind in {"acquisition", "opening"}:
            target = targets[0]
            require(
                target["lot_id"] is not None,
                "acquisition_root",
                "inventory_allocations",
            )
            lot = _lookup(indexes, "lots", target, "lot_id")
            require(
                lot["parent_lot_id"] is None,
                "acquisition_root",
                "inventory_allocations",
            )
        if kind in {"transfer", "pool_entry", "pool_exit"}:
            require(
                sum(
                    _required_number(r, "units_delta", "inventory_changes")
                    for r in rows
                )
                == 0,
                "allocation_units_conservation",
                "inventory_allocations",
            )
            if kind != "pool_entry":
                for component in (
                    "principal_delta",
                    "capitalized_fee_delta",
                    "expensed_fee_delta",
                ):
                    values = [number(r, component) for r in rows]
                    require(
                        all(v is None for v in values)
                        or (
                            all(v is not None for v in values)
                            and sum(
                                _required_number(r, component, "inventory_changes")
                                for r in rows
                            )
                            == 0
                        ),
                        "allocation_component_conservation",
                        "inventory_allocations",
                    )
            if kind == "transfer":
                require(
                    sources[0]["lot_id"] == targets[0]["lot_id"]
                    and (
                        sources[0]["lot_id"] is not None
                        or (
                            sources[0]["pool_id"] is not None
                            and targets[0]["pool_id"] is not None
                        )
                    ),
                    "transfer_lineage",
                    "inventory_allocations",
                )
            if kind == "pool_entry":
                require(
                    sources[0]["lot_id"] is not None
                    and targets[0]["pool_id"] is not None
                    and sources[0]["position_id"] == targets[0]["position_id"],
                    "pool_entry_shape",
                    "inventory_allocations",
                )
            if kind == "pool_exit":
                require(
                    sources[0]["pool_id"] is not None
                    and targets[0]["pool_id"] is not None,
                    "pool_exit_shape",
                    "inventory_allocations",
                )


def _disposals(tables: Tables, indexes: dict) -> None:
    for disposal in tables["disposals"]:
        allocation = _lookup(
            indexes, "inventory_allocations", disposal, "allocation_id"
        )
        require(
            allocation["allocation_kind"] in {"reduction", "transformation"},
            "disposal_allocation",
            "disposals",
        )
        _together(
            disposal,
            ("proceeds_total_coefficient", "proceeds_commodity_id"),
            "disposals",
        )
        require(
            disposal["disposal_fees_total_coefficient"] is None
            or disposal["proceeds_commodity_id"] is not None,
            "disposal_fee_currency",
            "disposals",
        )
    disposed = {(r["entity_id"], r["allocation_id"]) for r in tables["disposals"]}
    for allocation in tables["inventory_allocations"]:
        if allocation["allocation_kind"] == "reduction":
            require(
                (allocation["entity_id"], allocation["allocation_id"]) in disposed,
                "reduction_disposal",
                "inventory_allocations",
            )


def _event_times(tables: Tables, indexes: dict) -> None:
    for row in tables["event_times"]:
        require(
            (row["entity_id"], row["record_id"]) in indexes[row["record_kind"]],
            "record_reference",
            "event_times",
        )
        target = indexes[row["record_kind"]][(row["entity_id"], row["record_id"])]
        require(
            (row["timestamp_date"] is not None) == (row["time_mode"] == "value"),
            "event_time_mode",
            "event_times",
        )
        if row["date_role"] != "occurred" and row["time_mode"] == "value":
            field = f"date_{row['date_role']}"
            effective = target[field]
            if (
                row["record_kind"] == "postings"
                and target[f"{field}_mode"] == "inherit"
            ):
                effective = _lookup(indexes, "transactions", target, "txn_id")[field]
            require(
                effective == row["timestamp_date"], "event_time_date", "event_times"
            )


def _complete_balances(tables: Tables, indexes: dict, observed: dict) -> None:
    for assertion in tables["balance_assertions"]:
        if not assertion["is_complete"]:
            continue
        scope = _lookup(indexes, "assertion_scopes", assertion, "scope_id")
        positions = [
            row
            for row in tables["positions"]
            if row["entity_id"] == scope["entity_id"]
            and row["account_id"] == scope["account_id"]
            and (
                scope["selection"] == "account_all"
                or row["position_id"] in scope["position_ids"]
            )
        ]
        if scope["scope_kind"] == "positions":
            expected = {row["position_id"] for row in positions}
        elif scope["measurement"] == "units":
            expected = {row["commodity_id"] for row in positions}
        else:
            declaration = _lookup(indexes, "declarations", scope, "declaration_id")
            payload = decode_declaration(declaration["payload_json"])
            quote = payload["quote_commodity_id"]
            expected = {quote} if positions else set()
        require(
            observed[(assertion["entity_id"], assertion["assertion_set_id"])]
            == expected,
            "complete_balance_scope",
            "balance_assertions",
        )


def _balances(tables: Tables, indexes: dict) -> None:
    for scope in tables["assertion_scopes"]:
        require(
            bool(scope["position_ids"]) == (scope["selection"] == "positions"),
            "assertion_selection",
            "assertion_scopes",
        )
        for position_id in scope["position_ids"]:
            position = indexes["positions"].get((scope["entity_id"], position_id))
            require(
                position is not None and position["account_id"] == scope["account_id"],
                "assertion_position_account",
                "assertion_scopes",
            )
    for assertion in tables["balance_assertions"]:
        if assertion["statement_id"] is not None:
            statement = _lookup(indexes, "statements", assertion, "statement_id")
            scope = _lookup(indexes, "assertion_scopes", assertion, "scope_id")
            require(
                statement["account_id"] == scope["account_id"]
                and statement["coverage_basis"] == scope["coverage_basis"],
                "assertion_statement_scope",
                "balance_assertions",
            )
    unique = set()
    observed = defaultdict(set)
    for row in tables["balances"]:
        assertion = _lookup(indexes, "balance_assertions", row, "assertion_set_id")
        scope = _lookup(indexes, "assertion_scopes", assertion, "scope_id")
        positions = scope["scope_kind"] == "positions"
        require(
            (row["position_id"] is not None) == positions, "balance_scope", "balances"
        )
        identity = row["position_id"] if positions else row["commodity_id"]
        key = (row["entity_id"], row["assertion_set_id"], identity)
        require(key not in unique, "balance_grain", "balances")
        unique.add(key)
        observed[(row["entity_id"], row["assertion_set_id"])].add(identity)
        if positions:
            position = _lookup(indexes, "positions", row, "position_id")
            require(
                position["account_id"] == scope["account_id"]
                and (
                    scope["selection"] == "account_all"
                    or row["position_id"] in scope["position_ids"]
                ),
                "balance_position_scope",
                "balances",
            )
            if scope["measurement"] == "units":
                require(
                    row["commodity_id"] == position["commodity_id"],
                    "balance_units_commodity",
                    "balances",
                )
    _complete_balances(tables, indexes, observed)


def _reference_shapes(tables: Tables, indexes: dict) -> None:
    for row in tables["reference_changes"]:
        position = _lookup(indexes, "positions", row, "position_id")
        require(
            position["measurement_kind"] == "reference",
            "reference_measurement",
            "reference_changes",
        )
        before, after = number(row, "reference_before"), number(row, "reference_after")
        require(
            (before is not None, after is not None)
            == {
                "entry": (False, True),
                "reset": (True, True),
                "close": (True, False),
            }[row["change_kind"]],
            "reference_transition",
            "reference_changes",
        )
    totals = defaultdict(Fraction)
    for row in tables["reference_settlements"]:
        posting = _lookup(indexes, "postings", row, "settlement_posting_id")
        require(
            posting["step_id"] is not None
            and posting["commodity_id"] == row["commodity_id"],
            "reference_settlement_posting",
            "reference_settlements",
        )
        totals[(row["entity_id"], row["settlement_posting_id"])] += _required_number(
            row, "amount", "reference_settlements"
        )
    for key, total in totals.items():
        require(
            total == number(indexes["postings"][key], "amount"),
            "reference_settlement_conservation",
            "reference_settlements",
        )


def _intervals(tables: Tables) -> None:
    for name, rows in tables.items():
        for row in rows:
            _time(row, name)
            if (
                "valid_from" in row
                and row["valid_from"] is not None
                and row["valid_to"] is not None
            ):
                require(
                    row["valid_from"] < row["valid_to"],
                    "validity_interval",
                    name,
                )
    for row in tables["accounts"]:
        if row["opened_date"] is not None and row["closed_date"] is not None:
            require(
                row["opened_date"] <= row["closed_date"],
                "account_lifecycle",
                "accounts",
            )
        if row["statements_expected"]:
            require(
                all(
                    row[field] is not None
                    for field in (
                        "statement_cadence",
                        "civil_zone",
                        "coverage_start_date",
                    )
                ),
                "statement_expectation",
                "accounts",
            )


def _symbol_resolution(tables: Tables) -> None:
    by_symbol = defaultdict(list)
    for row in tables["commodity_symbols"]:
        by_symbol[(row["entity_id"], row["symbol"])].append(row)
    for symbols in by_symbol.values():
        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                if left["commodity_id"] == right["commodity_id"]:
                    continue
                if any(
                    left[field] is not None
                    and right[field] is not None
                    and left[field] != right[field]
                    for field in ("source_scope_id", "exchange")
                ):
                    continue
                if (
                    left["valid_to"] is None or right["valid_from"] < left["valid_to"]
                ) and (
                    right["valid_to"] is None or left["valid_from"] < right["valid_to"]
                ):
                    code = "ambiguous_commodity_symbol"
                    raise ContractError(code, "commodity_symbols")


def _statements(tables: Tables) -> None:
    for row in tables["statements"]:
        if row["source_class"] == "statement":
            require(
                row["period_start"] is not None and row["period_end"] is not None,
                "statement_period",
                "statements",
            )
        if row["period_start"] is not None and row["period_end"] is not None:
            require(
                row["period_start"] <= row["period_end"],
                "statement_period",
                "statements",
            )
        if row["source_format"] == "authored":
            require(
                row["declaration_id"] is not None, "authored_statement", "statements"
            )
        else:
            require(
                any(
                    r["entity_id"] == row["entity_id"]
                    and r["statement_id"] == row["statement_id"]
                    for r in tables["source_records"]
                ),
                "acquired_statement_evidence",
                "statements",
            )


def _commodities_and_actions(tables: Tables, indexes: dict) -> None:
    for row in tables["commodities"]:
        if row["contract_multiplier_coefficient"] is not None:
            require(
                _required_number(row, "contract_multiplier", "commodities") > 0,
                "positive_multiplier",
                "commodities",
            )
        if row["commodity_kind"] == "contract":
            require(
                row["terms_declaration_id"] is not None, "contract_terms", "commodities"
            )
    for row in tables["corporate_actions"]:
        event = _lookup(indexes, "transactions", row, "txn_id")
        require(
            event["event_kind"] == "corporate_action",
            "action_event",
            "corporate_actions",
        )


def _action_effects(tables: Tables) -> None:
    effect_fields = {
        "units": {
            "resulting_commodity_id",
            "ratio_numerator_coefficient",
            "ratio_denominator_coefficient",
        },
        "cash": {
            "cash_per_unit_coefficient",
            "cash_total_coefficient",
            "cash_commodity_id",
        },
        "basis": {
            "basis_per_unit_coefficient",
            "basis_total_coefficient",
            "basis_commodity_id",
        },
        "symbol": {"symbol_id"},
    }
    all_fields = set().union(*effect_fields.values())
    for row in tables["corporate_action_effects"]:
        kind = row["effect_kind"]
        require(
            all(row[f] is None for f in all_fields - effect_fields[kind]),
            "effect_unrelated_fields",
            "corporate_action_effects",
        )
        if kind == "units":
            require(
                all(row[f] is not None for f in effect_fields[kind])
                and _required_number(row, "ratio_numerator", "corporate_action_effects")
                > 0
                and _required_number(
                    row, "ratio_denominator", "corporate_action_effects"
                )
                > 0,
                "effect_ratio",
                "corporate_action_effects",
            )
        elif kind in {"cash", "basis"}:
            require(
                row[f"{kind}_commodity_id"] is not None
                and (
                    row[f"{kind}_per_unit_coefficient"] is not None
                    or row[f"{kind}_total_coefficient"] is not None
                ),
                "effect_amount",
                "corporate_action_effects",
            )
            if row[f"{kind}_total_coefficient"] is not None:
                require(
                    row["scope_declaration_id"] is not None,
                    "effect_total_scope",
                    "corporate_action_effects",
                )
        else:
            require(
                row["symbol_id"] is not None,
                "effect_symbol",
                "corporate_action_effects",
            )
    for action in tables["corporate_actions"]:
        if action["action_kind"] == "redenomination":
            require(
                any(
                    effect["entity_id"] == action["entity_id"]
                    and effect["action_id"] == action["action_id"]
                    and effect["effect_kind"] == "units"
                    and effect["input_commodity_id"] == action["commodity_id"]
                    and effect["resulting_commodity_id"] != action["commodity_id"]
                    for effect in tables["corporate_action_effects"]
                ),
                "redenomination_units_effect",
                "corporate_actions",
            )


def _record_ref(row: Row, indexes: dict, specs: dict, location: str) -> Row:
    kind = row["record_kind"]
    require(
        kind in specs and kind not in {"provenance", "build"}, "record_kind", location
    )
    try:
        key = json.loads(row["record_id"])
    except (ValueError, TypeError) as error:
        msg = "record_key_encoding"
        raise ContractError(msg, location) from error
    require(
        type(key) is list and len(key) == len(specs[kind]["primary_key"]) - 1,
        "record_key_arity",
        location,
    )
    require(all(type(v) in {str, int, bool} for v in key), "record_key_type", location)
    require(
        json.dumps(key, ensure_ascii=False, separators=(",", ":")) == row["record_id"],
        "record_key_encoding",
        location,
    )
    require((row["entity_id"], *key) in indexes[kind], "record_reference", location)
    return indexes[kind][(row["entity_id"], *key)]


def _evidence(tables: Tables, indexes: dict, specs: dict) -> None:
    for row in tables["observations"]:
        _record_ref(row, indexes, specs, "observations")
        require(
            (row["source_record_id"] is None) != (row["declaration_id"] is None),
            "observation_evidence",
            "observations",
        )
        present = tuple(
            row[f] is not None
            for f in ("decimal_value_coefficient", "text_value", "date_value")
        )
        require(
            present
            == {
                "decimal": (True, False, False),
                "text": (False, True, False),
                "date": (False, False, True),
            }[row["value_type"]],
            "observation_value",
            "observations",
        )
        if row["observation_kind"] == "source_status":
            require(
                row["field_name"] == "source.status" and row["value_type"] == "text",
                "source_status",
                "observations",
            )
    for row in tables["provenance"]:
        _record_ref(row, indexes, specs, "provenance")
        require(
            row["field_name"] in specs[row["record_kind"]]["logical_fields"],
            "provenance_field",
            "provenance",
        )
        evidence_table = {
            "source_record": "source_records",
            "declaration": "declarations",
        }[row["evidence_kind"]]
        require(
            (row["entity_id"], row["evidence_id"]) in indexes[evidence_table],
            "provenance_evidence",
            "provenance",
        )
        if row["value_origin"] in {"rule", "booking", "action"}:
            require(
                row["derivation_id"] is not None, "provenance_derivation", "provenance"
            )
        if row["value_origin"] == "correction":
            require(
                row["evidence_kind"] == "declaration",
                "correction_evidence",
                "provenance",
            )
            require(
                indexes["declarations"][(row["entity_id"], row["evidence_id"])][
                    "declaration_kind"
                ]
                == "correction",
                "correction_evidence",
                "provenance",
            )
    _economic_identities(tables, indexes)


def _economic_identities(tables: Tables, indexes: dict) -> None:
    for row in tables["economic_identities"]:
        target = {
            "transaction": "transactions",
            "corporate_action": "corporate_actions",
        }[row["record_kind"]]
        require(
            (row["entity_id"], row["record_id"]) in indexes[target],
            "economic_identity_target",
            "economic_identities",
        )
        if row["anchor_kind"] == "source_record":
            require(
                (row["entity_id"], row["anchor_id"]) in indexes["source_records"],
                "identity_anchor",
                "economic_identities",
            )
            if row["record_kind"] == "transaction":
                source = indexes["source_records"][(row["entity_id"], row["anchor_id"])]
                canonical = event_id(
                    EntityId(row["entity_id"]),
                    Occurrence(
                        AcquiredAnchor(
                            source["source_blob_digest"],
                            SemanticKey(source["source_scope_id"]),
                            SemanticKey(source["source_section"]),
                            SemanticKey(source["record_locator"]),
                        ),
                        SemanticKey(row["event_key"]),
                    ),
                )
                require(
                    row["record_id"] == canonical.value,
                    "economic_identity_canonical",
                    "economic_identities",
                )
        else:
            require(
                any(
                    r["entity_id"] == row["entity_id"]
                    and r["declaration_key"] == row["anchor_id"]
                    for r in tables["declarations"]
                ),
                "identity_anchor",
                "economic_identities",
            )
            if row["record_kind"] == "transaction":
                canonical = event_id(
                    EntityId(row["entity_id"]),
                    Occurrence(
                        AuthoredAnchor(SemanticKey(row["anchor_id"])),
                        SemanticKey(row["event_key"]),
                    ),
                )
                require(
                    row["record_id"] == canonical.value,
                    "economic_identity_canonical",
                    "economic_identities",
                )
        if row["identity_state"] == "retired":
            event = indexes[target][(row["entity_id"], row["record_id"])]
            if target == "corporate_actions":
                event = _lookup(indexes, "transactions", event, "txn_id")
            require(
                event["event_state"] == "retracted",
                "retired_event",
                "economic_identities",
            )


def _fees(tables: Tables, indexes: dict) -> None:
    fees = defaultdict(Fraction)
    for row in tables["fee_allocations"]:
        require(
            (row["fee_posting_id"] is None) != (row["opening_declaration_id"] is None),
            "fee_source",
            "fee_allocations",
        )
        _together(
            row, ("book_amount_coefficient", "book_commodity_id"), "fee_allocations"
        )
        target = {"lot": "lots", "disposal": "disposals"}[row["target_kind"]]
        require(
            (row["entity_id"], row["target_id"]) in indexes[target],
            "fee_target",
            "fee_allocations",
        )
        if target == "lots":
            lot = indexes[target][(row["entity_id"], row["target_id"])]
            require(lot["parent_lot_id"] is None, "fee_root", "fee_allocations")
            require(
                lot["book_commodity_id"] is not None
                or row["treatment"] != "capitalized",
                "reference_fee_capitalization",
                "fee_allocations",
            )
        if row["fee_posting_id"] is not None:
            posting = _lookup(indexes, "postings", row, "fee_posting_id")
            require(
                posting["posting_role"] in {"commission", "fee", "borrow_fee"}
                and posting["step_id"] is not None
                and posting["commodity_id"] == row["commodity_id"],
                "fee_posting",
                "fee_allocations",
            )
            fees[(row["entity_id"], row["fee_posting_id"])] += _required_number(
                row, "amount", "fee_allocations"
            )
        else:
            declaration = _lookup(
                indexes, "declarations", row, "opening_declaration_id"
            )
            require(
                declaration["declaration_kind"] == "opening_lot",
                "opening_fee_source",
                "fee_allocations",
            )
    for key, total in fees.items():
        require(
            total == number(indexes["postings"][key], "amount"),
            "fee_conservation",
            "fee_allocations",
        )


def _inventory_fees(tables: Tables, indexes: dict) -> None:
    component_fees = defaultdict(Fraction)
    for row in tables["inventory_fee_changes"]:
        fee = _lookup(indexes, "fee_allocations", row, "fee_allocation_id")
        change = _lookup(indexes, "inventory_changes", row, "change_id")
        position = _lookup(indexes, "positions", change, "position_id")
        require(
            fee["target_kind"] == "lot", "inventory_fee_origin", "inventory_fee_changes"
        )
        if position["measurement_kind"] == "reference":
            require(
                row["book_amount_delta_coefficient"] is None,
                "reference_fee_book_amount",
                "inventory_fee_changes",
            )
        if row["book_amount_delta_coefficient"] is not None:
            component_fees[(row["entity_id"], row["change_id"], fee["treatment"])] += (
                _required_number(row, "book_amount_delta", "inventory_fee_changes")
            )
    for (entity, change_id, treatment), total in component_fees.items():
        if treatment in {"capitalized", "expensed"}:
            require(
                total
                == number(
                    indexes["inventory_changes"][(entity, change_id)],
                    f"{treatment}_fee_delta",
                ),
                "inventory_fee_component_conservation",
                "inventory_fee_changes",
            )


def _conversions(tables: Tables, indexes: dict) -> None:
    conversions = {
        (r["entity_id"], r["allocation_id"], r["component_kind"]): r
        for r in tables["component_conversions"]
    }
    for conversion in tables["component_conversions"]:
        allocation = _lookup(
            indexes, "inventory_allocations", conversion, "allocation_id"
        )
        require(
            allocation["allocation_kind"] == "pool_entry"
            and conversion["source_commodity_id"] != conversion["target_commodity_id"],
            "conversion_allocation",
            "component_conversions",
        )
    for allocation in tables["inventory_allocations"]:
        if allocation["allocation_kind"] != "pool_entry":
            continue
        changes = [
            r
            for r in tables["inventory_changes"]
            if r["entity_id"] == allocation["entity_id"]
            and r["allocation_id"] == allocation["allocation_id"]
        ]
        source = next(r for r in changes if r["change_role"] == "source")
        target = next(r for r in changes if r["change_role"] == "target")
        lot = _lookup(indexes, "lots", source, "lot_id")
        pool = _lookup(indexes, "inventory_pools", target, "pool_id")
        for component in ("principal", "capitalized_fee", "expensed_fee"):
            before, after = (
                number(source, f"{component}_delta"),
                number(target, f"{component}_delta"),
            )
            conversion = conversions.get(
                (allocation["entity_id"], allocation["allocation_id"], component)
            )
            if lot["book_commodity_id"] == pool["book_commodity_id"]:
                require(
                    conversion is None,
                    "same_currency_no_conversion",
                    "component_conversions",
                )
                require(
                    (before is None and after is None)
                    or (before is not None and after == -before),
                    "pool_component_conservation",
                    "inventory_allocations",
                )
            elif before is not None:
                if conversion is None:
                    code = "conversion_required"
                    raise ContractError(code, "component_conversions")
                require(
                    conversion["source_commodity_id"] == lot["book_commodity_id"]
                    and conversion["target_commodity_id"] == pool["book_commodity_id"]
                    and number(conversion, "source_amount") == -before
                    and number(conversion, "target_amount") == after,
                    "conversion_component_conservation",
                    "component_conversions",
                )
            else:
                require(
                    conversion is None and after is None,
                    "unknown_conversion",
                    "component_conversions",
                )


type InventoryKey = tuple[str, str, str | None, str | None]
type InventoryState = dict[InventoryKey, list[Fraction | None]]


def _inventory_key(row: Row) -> InventoryKey:
    return (row["entity_id"], row["position_id"], row["lot_id"], row["pool_id"])


def _held_units(state: InventoryState, key: InventoryKey) -> Fraction:
    values = state.get(key)
    if values is None:
        return Fraction(0)
    units = values[0]
    if units is None:
        code = "required_value"
        raise ContractError(code, "inventory_changes.units_delta")
    return units


def _replay_inventory_step(
    rows: list[Row], state: InventoryState, indexes: dict
) -> None:
    deltas: dict[InventoryKey, list[Fraction | None]] = defaultdict(
        lambda: [Fraction(0)] * 4
    )
    removals: dict[InventoryKey, Fraction] = defaultdict(Fraction)
    origins: dict[InventoryKey, Fraction] = defaultdict(Fraction)
    for row in rows:
        allocation = _lookup(indexes, "inventory_allocations", row, "allocation_id")
        if allocation["allocation_kind"] in {"acquisition", "opening"}:
            origins[_inventory_key(row)] += _required_number(
                row, "units_delta", "inventory_changes"
            )
    for row in rows:
        key = _inventory_key(row)
        units = _required_number(row, "units_delta", "inventory_changes")
        values = [
            units,
            number(row, "principal_delta"),
            number(row, "capitalized_fee_delta"),
            number(row, "expensed_fee_delta"),
        ]
        if row["change_role"] == "source":
            allocation = _lookup(indexes, "inventory_allocations", row, "allocation_id")
            held = _held_units(state, key)
            if allocation["allocation_kind"] == "pool_entry":
                held += origins[key]
            require(
                held != 0 and abs(units) <= abs(held),
                "inventory_source_available",
                "inventory_changes",
            )
            removals[key] += units
            require(
                units == 0 or held * units < 0,
                "inventory_source_direction",
                "inventory_changes",
            )
        for index, value in enumerate(values):
            prior = deltas[key][index]
            deltas[key][index] = (
                None if value is None or prior is None else prior + value
            )
    for key, removed in removals.items():
        require(
            abs(removed) <= abs(_held_units(state, key) + origins[key]),
            "inventory_source_available",
            "inventory_changes",
        )
    for key, values in deltas.items():
        previous = state.get(key, [Fraction(0)] * 4)
        updated = [
            None if a is None or b is None else a + b
            for a, b in zip(previous, values, strict=True)
        ]
        if updated[0] == 0:
            require(
                all(v is None or v == 0 for v in updated[1:]),
                "full_close_components",
                "inventory_changes",
            )
            state.pop(key, None)
        else:
            state[key] = updated


def _inventory_replay(tables: Tables, indexes: dict) -> None:
    state: InventoryState = {}
    changes: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in tables["inventory_changes"]:
        allocation = _lookup(indexes, "inventory_allocations", row, "allocation_id")
        changes[(row["entity_id"], allocation["step_id"])].append(row)
    for step in tables["book_steps"]:
        _replay_inventory_step(
            changes[(step["entity_id"], step["step_id"])], state, indexes
        )
        for key in state:
            position = indexes["positions"][(key[0], key[1])]
            require(
                position["inventory_method"] != "pooled" or key[2] is None,
                "pool_step_remainder",
                "inventory_changes",
            )


def _reference_replay(tables: Tables, indexes: dict) -> None:
    state = {}
    rows = sorted(
        tables["reference_changes"],
        key=lambda row: (
            row["entity_id"],
            _lookup(indexes, "book_steps", row, "step_id")["sequence"],
            row["reference_change_id"],
        ),
    )
    touched = set()
    for row in rows:
        key = (row["entity_id"], row["position_id"], row["lot_id"])
        touch = (*key, row["step_id"])
        require(touch not in touched, "reference_atomic_slice", "reference_changes")
        touched.add(touch)
        units = _required_number(row, "units", "reference_changes")
        require(units != 0, "reference_nonzero_units", "reference_changes")
        if row["change_kind"] == "entry":
            require(key not in state, "reference_existing_slice", "reference_changes")
            state[key] = (units, number(row, "reference_after"))
            continue
        require(key in state, "reference_missing_entry", "reference_changes")
        held, current = state[key]
        require(
            current == number(row, "reference_before"),
            "reference_continuity",
            "reference_changes",
        )
        require(
            held * units > 0 and abs(units) <= abs(held),
            "reference_units",
            "reference_changes",
        )
        if row["change_kind"] == "reset":
            require(units == held, "partial_reset_requires_branch", "reference_changes")
            state[key] = (held, number(row, "reference_after"))
        elif held == units:
            del state[key]
        else:
            state[key] = (held - units, current)


def _reference_transfer_continuity(tables: Tables, indexes: dict) -> None:
    references = defaultdict(list)
    for row in tables["reference_changes"]:
        references[
            (
                row["entity_id"],
                row["step_id"],
                row["position_id"],
                row["lot_id"],
                row["change_kind"],
                _required_number(row, "units", "reference_changes"),
            )
        ].append(row)
    changes = defaultdict(list)
    for row in tables["inventory_changes"]:
        changes[(row["entity_id"], row["allocation_id"])].append(row)
    for allocation in tables["inventory_allocations"]:
        if allocation["allocation_kind"] != "transfer":
            continue
        source, target = (
            next(
                row
                for row in changes[
                    (allocation["entity_id"], allocation["allocation_id"])
                ]
                if row["change_role"] == role
            )
            for role in ("source", "target")
        )
        source_position = _lookup(indexes, "positions", source, "position_id")
        target_position = _lookup(indexes, "positions", target, "position_id")
        if (
            source_position["measurement_kind"] != "reference"
            and target_position["measurement_kind"] != "reference"
        ):
            continue
        require(
            source_position["measurement_kind"]
            == target_position["measurement_kind"]
            == "reference"
            and source["lot_id"] is not None
            and target["lot_id"] is not None,
            "reference_transfer_pair",
            "inventory_allocations",
        )
        entity, step = allocation["entity_id"], allocation["step_id"]
        outgoing = references[
            (
                entity,
                step,
                source["position_id"],
                source["lot_id"],
                "close",
                -_required_number(source, "units_delta", "inventory_changes"),
            )
        ]
        incoming = references[
            (
                entity,
                step,
                target["position_id"],
                target["lot_id"],
                "entry",
                _required_number(target, "units_delta", "inventory_changes"),
            )
        ]
        require(
            len(outgoing) == len(incoming) == 1,
            "reference_transfer_pair",
            "reference_changes",
        )
        before, after = outgoing[0], incoming[0]
        require(
            before["reference_commodity_id"] == after["reference_commodity_id"]
            and before["terms_declaration_id"] == after["terms_declaration_id"]
            and number(before, "reference_before") == number(after, "reference_after")
            and before["settlement_reference_coefficient"] is None
            and after["settlement_reference_coefficient"] is None,
            "reference_transfer_continuity",
            "reference_changes",
        )


def validate_link_members(kind: str, members: list[dict], terms: str | None) -> None:
    """Enforce exact participant roles and cardinalities without booking."""
    rules = {
        "transfer": {
            "outgoing": ({"posting"}, 1, None),
            "incoming": ({"posting"}, 1, None),
        },
        "card_payment": {
            "payment": ({"posting"}, 1, None),
            "credit": ({"posting"}, 1, None),
        },
        "settlement": {
            "trade": ({"transaction"}, 1, None),
            "settlement": ({"transaction"}, 1, None),
        },
        "document": {
            "subject": ({"transaction", "posting", "lot", "commodity"}, 1, None),
            "evidence": ({"blob"}, 1, None),
        },
        "lot_transfer": {
            "source": ({"lot"}, 1, None),
            "destination": ({"lot"}, 1, None),
        },
        "trade_chain": {
            "open": ({"transaction"}, 1, 1),
            "roll": ({"transaction"}, 0, None),
            "close": ({"transaction"}, 0, None),
        },
        "lifecycle": {
            "predecessor": ({"transaction"}, 1, None),
            "successor": ({"transaction"}, 0, None),
            "cash": ({"posting"}, 0, None),
            "fee": ({"posting"}, 0, None),
        },
        "complete_set": {"outcome": ({"commodity"}, 2, None)},
    }
    require(kind in rules, "link_kind", "links")
    require((terms is not None) == (kind == "complete_set"), "link_terms", "links")
    counts = defaultdict(int)
    distinct = set()
    for member in members:
        role = member["role"]
        require(
            role in rules[kind] and member["kind"] in rules[kind][role][0],
            "link_member_role",
            "links",
        )
        counts[role] += 1
        identity = (member["kind"], member["id"])
        if kind in {"trade_chain", "complete_set"}:
            require(identity not in distinct, "link_distinct_members", "links")
        distinct.add(identity)
    for role, (_, minimum, maximum) in rules[kind].items():
        require(
            counts[role] >= minimum and (maximum is None or counts[role] <= maximum),
            "link_cardinality",
            "links",
        )
    if kind == "lifecycle":
        require(
            any(counts[role] for role in ("successor", "cash", "fee")),
            "lifecycle_effect",
            "links",
        )


def _links_and_applications(tables: Tables, indexes: dict) -> None:
    groups = defaultdict(list)
    targets = {
        "transaction": "transactions",
        "posting": "postings",
        "lot": "lots",
        "commodity": "commodities",
    }
    for row in tables["links"]:
        groups[(row["entity_id"], row["link_id"])].append(row)
        if row["member_kind"] != "blob":
            require(
                (row["entity_id"], row["member_id"])
                in indexes[targets[row["member_kind"]]],
                "link_member_reference",
                "links",
            )
    for rows in groups.values():
        first = rows[0]
        require(
            all(
                (r["link_kind"], r["origin"], r["terms_declaration_id"])
                == (first["link_kind"], first["origin"], first["terms_declaration_id"])
                for r in rows
            ),
            "link_shared_fields",
            "links",
        )
        validate_link_members(
            first["link_kind"],
            [
                {
                    "role": r["member_role"],
                    "kind": r["member_kind"],
                    "id": r["member_id"],
                }
                for r in rows
            ],
            first["terms_declaration_id"],
        )
    for row in tables["transactions"]:
        for link in row["links"]:
            require(
                (row["entity_id"], link) in groups, "transaction_link", "transactions"
            )


def _action_applications(tables: Tables, indexes: dict) -> None:
    for row in tables["action_applications"]:
        effect = _lookup(indexes, "corporate_action_effects", row, "effect_id")
        require(
            effect["effect_kind"] != "symbol"
            and (effect["effect_kind"] == "basis")
            == (row["target_kind"] == "inventory_change"),
            "action_application_target",
            "action_applications",
        )
        table = {
            "inventory_allocation": "inventory_allocations",
            "inventory_change": "inventory_changes",
            "disposal": "disposals",
            "posting": "postings",
        }[row["target_kind"]]
        target = indexes[table].get((row["entity_id"], row["target_id"]))
        require(
            target is not None, "action_application_reference", "action_applications"
        )
        if table == "inventory_changes":
            holding = (
                _lookup(indexes, "lots", target, "lot_id")
                if target["lot_id"] is not None
                else _lookup(indexes, "inventory_pools", target, "pool_id")
            )
            require(
                effect["basis_commodity_id"] == holding["book_commodity_id"],
                "basis_application_currency",
                "action_applications",
            )
            if effect["basis_total_coefficient"] is not None:
                require(
                    number(effect, "basis_total") == number(target, "principal_delta"),
                    "basis_application_amount",
                    "action_applications",
                )
            if effect["basis_per_unit_coefficient"] is not None:
                require(
                    _required_number(
                        effect, "basis_per_unit", "corporate_action_effects"
                    )
                    * _required_number(target, "units_delta", "inventory_changes")
                    == number(target, "principal_delta"),
                    "basis_application_amount",
                    "action_applications",
                )
            target = _lookup(indexes, "inventory_allocations", target, "allocation_id")
        elif table == "disposals":
            target = _lookup(indexes, "inventory_allocations", target, "allocation_id")
        require(
            target["step_id"] == row["step_id"],
            "action_application_step",
            "action_applications",
        )
        if table in {"inventory_allocations", "inventory_changes"}:
            require(
                target["action_id"] == effect["action_id"],
                "action_application_identity",
                "action_applications",
            )


def _provenance_completeness(tables: Tables, specs: dict) -> None:
    fields = {
        "transactions": (
            "event_kind",
            "event_state",
            "date_authorized",
            "date_traded",
            "date_posted",
            "date_settled",
            "date_value",
            "payee",
            "narration",
        ),
        "postings": (
            "amount",
            "posting_role",
            "leg_kind",
            "price_per_unit",
            "original_amount",
            "fx_rate_as_stated",
        ),
        "book_steps": ("sequence", "operation_kind", "effective_date"),
        "posting_weights": ("weight_kind", "amount"),
        "inventory_changes": (
            "change_role",
            "units_delta",
            "principal_delta",
            "capitalized_fee_delta",
            "expensed_fee_delta",
        ),
        "component_conversions": ("source_amount", "target_amount", "rate"),
        "disposals": ("proceeds_total", "disposal_fees_total"),
        "fee_allocations": ("amount", "book_amount", "treatment"),
        "inventory_fee_changes": ("amount_delta", "book_amount_delta"),
        "reference_changes": (
            "change_kind",
            "units",
            "reference_before",
            "reference_after",
            "settlement_reference",
        ),
        "reference_settlements": ("amount",),
        "balances": ("amount",),
        "prices": ("rate", "date", "basis"),
    }
    covered = {
        (row["entity_id"], row["record_kind"], row["record_id"], row["field_name"])
        for row in tables["provenance"]
    }
    for table, required in fields.items():
        for row in tables[table]:
            if table == "transactions" and row["event_state"] != "recognized":
                continue
            key = [row[field] for field in specs[table]["primary_key"][1:]]
            encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            for field in required:
                physical = field if field in row else f"{field}_coefficient"
                if row[physical] is not None:
                    require(
                        (row["entity_id"], table, encoded, field) in covered,
                        "provenance_completeness",
                        f"{table}.{field}",
                    )


def validate_tables(tables: Tables) -> None:
    """Validate a complete snapshot, including typed empty tables.

    External custody/registry references require manifest validation as well.
    This validates supplied decisions, never selecting inventory or prices.
    """
    specs = {t["name"]: t for t in schema_document()["tables"]}
    indexes = _structure(tables, specs)
    for declaration in tables["declarations"]:
        payload = decode_declaration(declaration["payload_json"])
        require(
            payload["kind"] == declaration["declaration_kind"],
            "declaration_kind",
            "declarations",
        )
        valid_from = declaration["valid_from"]
        valid_to = declaration["valid_to"]
        revision = revision_id(
            EntityId(declaration["entity_id"]),
            SemanticKey(declaration["declaration_key"]),
            effective_from=date.fromisoformat(valid_from) if valid_from else None,
            effective_to=date.fromisoformat(valid_to) if valid_to else None,
            payload=payload,
        ).value
        require(
            declaration["declaration_id"] == revision
            and declaration["revision_digest"] == revision,
            "declaration_revision_identity",
            "declarations",
        )
    _participation(tables, indexes)
    _positions_and_lineage(tables, indexes)
    _acyclic_lineage(tables, indexes)
    _lot_identities(tables, indexes)
    by_allocation = _inventory(tables, indexes)
    _allocation_shapes(tables, indexes, by_allocation)
    _disposals(tables, indexes)
    _event_times(tables, indexes)
    _balances(tables, indexes)
    _reference_shapes(tables, indexes)
    _intervals(tables)
    _symbol_resolution(tables)
    _statements(tables)
    _commodities_and_actions(tables, indexes)
    _action_effects(tables)
    _evidence(tables, indexes, specs)
    _fees(tables, indexes)
    _inventory_fees(tables, indexes)
    _conversions(tables, indexes)
    _inventory_replay(tables, indexes)
    _reference_replay(tables, indexes)
    _reference_transfer_continuity(tables, indexes)
    _links_and_applications(tables, indexes)
    _action_applications(tables, indexes)
    _provenance_completeness(tables, specs)
