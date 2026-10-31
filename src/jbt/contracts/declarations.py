"""Typed declaration semantics beyond closed structural JSON validation."""

import json
import re
from datetime import date
from fractions import Fraction
from functools import lru_cache

from jsonschema import ValidationError

from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import (
    ContractError,
    require,
    validate_identifier,
    validate_number,
)
from jbt.contracts.schemas import declaration_schema, validate_document
from jbt.domain.rules import RuleField, RuleMode


@lru_cache(maxsize=1)
def _validation_schema() -> dict:
    return declaration_schema()


def field_registry() -> tuple[dict, ...]:
    """Publish explicit assignment permissions for every logical model field."""
    result = []
    permitted_rules = set(RuleField)
    corrections = {*permitted_rules, "transactions.event_state"}
    for table in tabular_schema()["tables"]:
        for name, kind in table["logical_fields"].items():
            qualified = f"{table['name']}.{name}"
            if (
                table["name"] in {"transactions", "postings"}
                and name.startswith("date_")
                and not name.endswith("_mode")
            ):
                corrections.add(qualified)
            result.append(
                {
                    "field_name": qualified,
                    "target_table": table["name"],
                    "value_type": kind.removesuffix("?"),
                    "nullable": kind.endswith("?"),
                    "units": "commodity"
                    if kind.removesuffix("?") == "decimal"
                    else "none",
                    "rule_mode": RuleField(qualified).mode.value
                    if qualified in permitted_rules
                    else RuleMode.PROHIBITED.value,
                    "correction_allowed": qualified in corrections,
                }
            )
    return tuple(result)


def _exact(value: dict) -> Fraction:
    return Fraction(int(value["coefficient"]), 10 ** value["scale"])


def _walk(value: object, location: str) -> None:
    if isinstance(value, list):
        for item in value:
            _walk(item, location)
        if all(isinstance(item, str) for item in value) and not location.endswith(
            (".key", ".value")
        ):
            require(value == sorted(set(value)), "list_order_unique", location)
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk(item, f"{location}.{key}")


def _ordered(items: list, fields: tuple[str, ...], location: str) -> None:
    keys = [tuple(item[field] for field in fields) for item in items]
    require(keys == sorted(set(keys)), "child_order_unique", location)


def _rule_assignments(payload: dict) -> None:
    registry = {row["field_name"]: row for row in field_registry()}
    tables = {t["name"]: t for t in tabular_schema()["tables"]}
    for assignment in payload["assignments"]:
        field = assignment["field"]
        require(field in registry, "assignment_field", payload["kind"])
        definition = registry[field]
        require(definition["rule_mode"] != "prohibited", "rule_assignment", field)
        table_name, column_name = field.split(".", 1)
        column = next(
            (c for c in tables[table_name]["columns"] if c["name"] == column_name), None
        )
        value = assignment["value"]
        if value is None:
            require(definition["nullable"], "assignment_value", field)
            continue
        kind = definition["value_type"]
        if kind == "decimal":
            require(
                type(value) is dict
                and set(value) == {"coefficient", "scale", "source_scale"},
                "assignment_value",
                field,
            )
            validate_number(value, field)
        else:
            expected = {
                "string": str,
                "date": str,
                "integer": int,
                "list": list,
                "boolean": bool,
            }[kind]
            require(type(value) is expected, "assignment_value", field)
            if kind == "list":
                require(
                    all(type(item) is str for item in value)
                    and value == sorted(set(value)),
                    "assignment_value",
                    field,
                )
            if column and kind in {"string", "list"}:
                _field_text(value, column, field)
            if kind == "date":
                require(
                    re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
                    "assignment_value",
                    field,
                )
                try:
                    date.fromisoformat(value)
                except ValueError as error:
                    code = "assignment_value"
                    raise ContractError(code, field) from error
            if column and "enum" in column:
                require(value in column["enum"], "assignment_value", field)


def _field_text(value: object, column: dict, location: str) -> None:
    if value is None:
        require(column["nullable"], "required_value", location)
        return
    values = value if isinstance(value, list) else [value]
    require(all(isinstance(item, str) for item in values), "field_text_value", location)
    if column["text_semantics"] == "identifier":
        for item in values:
            validate_identifier(item, location)


def rule_match_fields(observation_fields: tuple[dict, ...]) -> dict[str, dict]:
    """Bind source operands to their manifest types, not inferred values."""
    fields = {}
    for table in tabular_schema()["tables"]:
        for name, kind in table["logical_fields"].items():
            column = next((c for c in table["columns"] if c["name"] == name), {})
            fields[f"{table['name']}.{name}"] = {
                "value_type": kind.removesuffix("?"),
                "nullable": kind.endswith("?"),
                "column": column,
            }
    for field in observation_fields:
        require(field["field_name"] not in fields, "rule_field_collision", "rule")
        fields[field["field_name"]] = {
            "value_type": {
                "text": "string",
                "json_text": "string",
                "decimal": "decimal",
                "date": "date",
            }[field["value_type"]],
            "nullable": True,
            "column": {},
        }
    return fields


def _match_value(value: object, definition: dict, field: str) -> None:
    if value is None:
        require(definition["nullable"], "rule_match_value", field)
        return
    kind = definition["value_type"]
    expected = {
        "string": str,
        "date": str,
        "decimal": dict,
        "integer": int,
        "boolean": bool,
        "list": list,
    }[kind]
    require(type(value) is expected, "rule_match_value", field)
    if kind == "decimal" and isinstance(value, dict):
        require(
            set(value) == {"coefficient", "scale", "source_scale"},
            "rule_match_value",
            field,
        )
        validate_number(value, field)
    elif kind == "date" and isinstance(value, str):
        require(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
            "rule_match_value",
            field,
        )
        try:
            date.fromisoformat(value)
        except ValueError as error:
            code = "rule_match_value"
            raise ContractError(code, field) from error
    elif kind == "list" and isinstance(value, list):
        require(
            all(type(item) is str for item in value) and value == sorted(set(value)),
            "rule_match_value",
            field,
        )
    column = definition["column"]
    if column and kind in {"string", "list"}:
        _field_text(value, column, field)
    if "enum" in column:
        require(value in column["enum"], "rule_match_value", field)


def validate_rule_matches(
    payload: dict, observation_fields: tuple[dict, ...] | None = None
) -> None:
    """Check model types alone, or the complete manifest-bound vocabulary."""
    fields = rule_match_fields(observation_fields or ())
    for predicate in payload["matches"]:
        field, operator, value = (
            predicate["field"],
            predicate["operator"],
            predicate["value"],
        )
        if operator == "in":
            require(
                type(value) is list
                and bool(value)
                and len({type(v) for v in value}) == 1,
                "in_homogeneous",
                field,
            )
        if field not in fields:
            require(observation_fields is None, "rule_match_field", field)
            continue
        definition = fields[field]
        if operator == "contains":
            require(
                definition["value_type"] == "string"
                and definition["column"].get("text_semantics") != "identifier"
                and type(value) is str,
                "contains_text",
                field,
            )
        for operand in value if operator == "in" else [value]:
            _match_value(operand, definition, field)


def _account(payload: dict) -> None:
    periods = payload["expected_periods"]
    opened, closed = payload["opened_date"], payload["closed_date"]
    start = payload["coverage_start_date"]
    require(
        opened is None or closed is None or opened <= closed,
        "account_lifecycle",
        "account",
    )
    require(
        start is None
        or (
            (opened is None or opened <= start) and (closed is None or start <= closed)
        ),
        "account_coverage_lifecycle",
        "account",
    )
    if not payload["statements_expected"]:
        require(
            payload["period_rule"] == "none" and not periods,
            "account_no_periods",
            "account",
        )
    else:
        require(
            payload["period_rule"] != "none"
            and all(
                payload[k] is not None
                for k in ("statement_cadence", "civil_zone", "coverage_start_date")
            ),
            "account_expected_periods",
            "account",
        )
        if payload["statement_cadence"] == "irregular":
            require(
                payload["period_rule"] == "explicit", "irregular_period_rule", "account"
            )
        elif payload["statement_cadence"] == "monthly_cycle":
            require(
                payload["period_rule"] == "monthly_cycle",
                "cycle_period_rule",
                "account",
            )
        else:
            require(
                payload["period_rule"] == "calendar", "calendar_period_rule", "account"
            )
        require(
            bool(payload["coverage_basis_ids"]), "coverage_basis_required", "account"
        )
    if payload["period_rule"] == "explicit":
        require(bool(periods), "explicit_periods", "account")
    else:
        require(not periods, "unused_periods", "account")
    previous = None
    for period in periods:
        require(
            period["start"] <= period["end"] <= period["due"], "period_dates", "account"
        )
        require(
            previous is None or previous < period["start"], "period_overlap", "account"
        )
        previous = period["end"]


def _terms(payload: dict) -> None:
    payoff, settlement = payload["payoff"], payload["settlement"]
    require(
        (settlement["variation"] == "final")
        == (settlement["reference_reset"] == "on_final_variation"),
        "variation_reset",
        "contract_terms",
    )
    require(
        (payload["commodity_id"] is None) == (payoff["kind"] == "complete_set"),
        "terms_commodity",
        "contract_terms",
    )
    if settlement["mode"] in {"physical", "mixed"}:
        require(
            bool(payload["deliverables"]), "physical_deliverables", "contract_terms"
        )
    _ordered(
        payload["deliverables"], ("deliverable_key",), "contract_terms.deliverables"
    )
    if "multiplier" in payoff:
        require(
            _exact(payoff["multiplier"]) > 0, "positive_multiplier", "contract_terms"
        )
    if payoff["kind"] == "complete_set":
        require(
            _exact(payoff["units_per_outcome"]) > 0,
            "positive_outcome_units",
            "contract_terms",
        )
    if payoff["kind"] == "option":
        require(payload["expiry"] is not None, "option_expiry", "contract_terms")
        style = payoff["exercise_style"]
        require(
            (payoff["exercise_start"] is not None) == (style == "american"),
            "option_exercise_start",
            "contract_terms",
        )
        require(
            bool(payoff["exercise_dates"]) == (style == "bermudan"),
            "option_exercise_dates",
            "contract_terms",
        )
        require(
            all(day <= payload["expiry"] for day in payoff["exercise_dates"]),
            "option_expiry_dates",
            "contract_terms",
        )
        if payoff["exercise_start"] is not None:
            require(
                payoff["exercise_start"] <= payload["expiry"],
                "option_expiry_dates",
                "contract_terms",
            )
    if payoff["kind"] == "stated_cashflows":
        _ordered(payoff["flows"], ("flow_key",), "contract_terms.flows")
    _conditional_terms(payload)


def _conditional_terms(payload: dict) -> None:
    payoff = payload["payoff"]
    if payoff["kind"] == "conditional":
        _ordered(payoff["conditions"], ("condition_key",), "contract_terms.conditions")
        _ordered(payoff["cashflows"], ("flow_key",), "contract_terms.cashflows")
        conditions = {condition["condition_key"] for condition in payoff["conditions"]}
        for flow in payoff["cashflows"]:
            require(
                flow["condition_key"] in conditions,
                "cashflow_condition",
                "contract_terms",
            )
        for condition in payoff["conditions"]:
            required = condition["kind"] in {"barrier", "autocall"}
            present = [
                condition[k] is not None
                for k in ("underlying_id", "comparison", "level")
            ]
            require(
                all(present) if required else (all(present) or not any(present)),
                "condition_threshold",
                "contract_terms",
            )
            require(
                bool(condition["observation_dates"])
                == (condition["monitoring"] == "dated"),
                "condition_monitoring",
                "contract_terms",
            )
        for deliverable in payload["deliverables"]:
            require(
                deliverable["condition_key"] is None
                or deliverable["condition_key"] in conditions,
                "deliverable_condition",
                "contract_terms",
            )
    else:
        require(
            all(d["condition_key"] is None for d in payload["deliverables"]),
            "deliverable_condition",
            "contract_terms",
        )


def _simple_semantics(payload: dict) -> None:
    kind = payload["kind"]
    if kind == "position":
        require(
            (payload["counterparty_id"] is None)
            == (payload["position_kind"] == "property"),
            "position_counterparty",
            kind,
        )
        require(
            (payload["measurement_kind"] == "quantity")
            == (payload["inventory_method"] == "quantity"),
            "quantity_inventory",
            kind,
        )
        if payload["measurement_kind"] != "quantity":
            require(
                payload["booking_declaration_id"] is not None, "position_booking", kind
            )
        if payload["measurement_kind"] == "reference":
            require(
                payload["position_kind"] == "contract"
                and payload["inventory_method"] == "individual"
                and payload["terms_declaration_id"] is not None,
                "reference_position",
                kind,
            )
    elif kind == "assertion_scope":
        require(_exact(payload["tolerance"]) >= 0, "nonnegative_tolerance", kind)
        require(
            bool(payload["position_ids"]) == (payload["selection"] == "positions"),
            "assertion_selection",
            kind,
        )
        if payload["measurement"] == "units":
            require(
                payload["quote_commodity_id"] is None
                and not payload["valuation_evidence"],
                "unit_assertion_valuation",
                kind,
            )
        else:
            require(
                payload["quote_commodity_id"] is not None
                and bool(payload["valuation_evidence"]),
                "value_assertion_valuation",
                kind,
            )
    elif kind == "action_scope":
        selected = any(
            payload[name]
            for name in ("account_ids", "position_ids", "lot_ids", "pool_ids")
        )
        require(
            selected == (payload["selection"] == "explicit"),
            "action_scope_selection",
            kind,
        )


def _policy_semantics(payload: dict) -> None:
    kind = payload["kind"]
    if kind == "pool":
        require(
            bool(payload["eligibility"]["lot_ids"])
            == (payload["eligibility"]["mode"] == "listed_origins"),
            "pool_eligibility",
            kind,
        )
        _ordered(
            payload["conversions"], ("lot_id", "component_kind"), "pool.conversions"
        )
    elif kind == "fee_attribution":
        require(
            bool(payload["slices"]) == (payload["method"] in {"source", "explicit"}),
            "fee_slices",
            kind,
        )
        for item in payload["slices"]:
            require(
                item["fee_source"]["table"] in {"postings", "declarations"}
                and item["target"]["table"] in {"lots", "disposals"},
                "fee_slice_target",
                kind,
            )
    elif kind == "ordering":
        require(payload["before"] != payload["after"], "ordering_distinct", kind)
        guarded = {
            tuple(g["target"]["key"])
            for g in payload["guards"]
            if g["target"]["table"] == "transactions"
        }
        require(
            (payload["before"]["txn_id"],) in guarded
            and (payload["after"]["txn_id"],) in guarded,
            "ordering_event_guards",
            kind,
        )
    elif kind == "commodity":
        _ordered(payload["symbols"], ("symbol_id",), "commodity.symbols")
        for symbol in payload["symbols"]:
            require(
                symbol["valid_to"] is None or symbol["valid_from"] < symbol["valid_to"],
                "validity_interval",
                "commodity.symbols",
            )
    elif kind == "corporate_action":
        _ordered(payload["effects"], ("effect_key",), "corporate_action.effects")
    elif kind == "opening_lot":
        _ordered(payload["fees"], ("fee_key",), "opening_lot.fees")
        require(
            (payload["reference"] is None)
            == (payload["reference_commodity_id"] is None),
            "opening_reference",
            kind,
        )


def validate_declaration(payload: object) -> None:
    """Validate one payload; snapshot-dependent references are checked later."""
    if not isinstance(payload, dict):
        code = "declaration_type"
        raise ContractError(code, "declaration")
    require(payload.get("schema_version") == 1, "schema_version", "declaration")
    if payload.get("kind") in {
        "assign",
        "split",
        "identity",
        "statement_revision",
        "transfer",
        "link",
    }:
        from jbt.contracts.schemas import journal_entry_schema  # noqa: PLC0415

        try:
            validate_document(payload, journal_entry_schema())
        except ValidationError as error:
            code = "journal_declaration_schema"
            raise ContractError(code, "declaration") from error
        return
    schema = _validation_schema()
    kinds = {variant["$ref"].split("/")[-1] for variant in schema["oneOf"]}
    require(payload.get("kind") in kinds, "declaration_kind", "declaration")
    try:
        validate_document(
            payload, schema["$defs"][payload["kind"]], reference_scope=schema
        )
    except ValidationError as error:
        msg = "declaration_schema"
        raise ContractError(msg, "payload") from error
    _walk(payload, "payload")
    kind = payload["kind"]
    handlers = {
        "account": _account,
        "contract_terms": _terms,
    }
    if kind in handlers:
        handlers[kind](payload)
    else:
        _simple_semantics(payload)
        _policy_semantics(payload)
    if kind == "rule":
        _rule_assignments(payload)
        _ordered(payload["assignments"], ("field",), "rule.assignments")
        _ordered(payload["split"], ("child_key",), "rule.split")
        validate_rule_matches(payload)


@lru_cache(maxsize=256)
def _check_declaration(text: str) -> None:

    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            require(key not in result, "json_duplicate_key", "payload")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as error:
        if isinstance(error, ContractError):
            raise
        msg = "json_encoding"
        raise ContractError(msg, "payload") from error
    validate_declaration(payload)
    require(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        == text,
        "canonical_payload_json",
        "payload",
    )


def decode_declaration(text: str) -> dict:
    """Validate retained bytes once and return an isolated mutable payload."""
    _check_declaration(text)
    return json.loads(text)
