"""Closed JSON Schema 2020-12 documents with local-only references."""

from __future__ import annotations

import json
from functools import lru_cache, wraps
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, FormatChecker

if TYPE_CHECKING:
    from collections.abc import Callable

    from jsonschema.protocols import Validator

from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import (
    ContractError,
    validate_identifier,
    validate_json_values,
)
from jbt.domain.errors import TemporalError
from jbt.domain.time import validate_source_date

_FORMATS = FormatChecker()


def _isolated_schema(factory: Callable[[], dict]) -> Callable[[], dict]:
    """Cache construction while returning a fresh mutable document."""

    @lru_cache(maxsize=1)
    def encoded() -> str:
        return json.dumps(factory(), sort_keys=True, separators=(",", ":"))

    @wraps(factory)
    def fresh() -> dict:
        return json.loads(encoded())

    return fresh


@_FORMATS.checks("identifier", raises=ContractError)
def _identifier_format(value: object) -> bool:
    if isinstance(value, str):
        validate_identifier(value, "document.identifier")
    return True


@_FORMATS.checks("source-date", raises=TemporalError)
def _source_date_format(value: object) -> bool:
    if isinstance(value, str):
        validate_source_date(value)
    return True


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(item: dict, *, minimum: int = 0) -> dict:
    return {"type": "array", "items": item, "minItems": minimum}


def _enum(values: str) -> dict:
    return {"enum": values.split("/")}


def _ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(value: dict) -> dict:
    return {"anyOf": [value, {"type": "null"}]}


def _fields(expression: str) -> dict:
    result = {}
    for field in expression.split():
        name, kind = field.split(":")
        nullable = kind.endswith("?")
        kind = kind.removesuffix("?")
        value = _enum(kind) if "/" in kind else _ref(kind)
        result[name] = _nullable(value) if nullable else value
    return result


def _shape(expression: str) -> dict:
    return _object(_fields(expression))


def _table_fields(name: str, *, exclude: tuple[str, ...] = ()) -> dict:
    table = next(t for t in tabular_schema()["tables"] if t["name"] == name)
    kinds = {
        "string": "S",
        "integer": "I",
        "boolean": "B",
        "date": "D",
        "list": "L",
        "decimal": "N",
    }
    columns = {c["name"]: c for c in table["columns"]}
    result = {}
    for field, kind in table["logical_fields"].items():
        if field == "entity_id" or field in exclude:
            continue
        value = _ref(kinds[kind.removesuffix("?")])
        if field in columns and columns[field]["kind"] in {"string", "list"}:
            semantics = columns[field]["text_semantics"]
            value = _ref(
                {
                    ("string", "identifier"): "ID",
                    ("string", "descriptive"): "S",
                    ("list", "identifier"): "L",
                    ("list", "descriptive"): "TL",
                }[(columns[field]["kind"], semantics)]
            )
        if field in columns and "enum" in columns[field]:
            value = {"enum": columns[field]["enum"]}
        result[field] = _nullable(value) if kind.endswith("?") else value
    return result


@_isolated_schema
def _common_definitions() -> dict:
    definitions = {
        "S": {"type": "string", "text_semantics": "descriptive"},
        "ID": {
            "type": "string",
            "minLength": 1,
            "format": "identifier",
            "text_semantics": "identifier",
        },
        "I": {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1},
        "B": {"type": "boolean"},
        "D": {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "L": {"type": "array", "items": _ref("ID"), "uniqueItems": True},
        "TL": {"type": "array", "items": _ref("S"), "uniqueItems": True},
        "Digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "Path": {
            "type": "string",
            "minLength": 1,
            "format": "identifier",
            "text_semantics": "identifier",
            "pattern": r"^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))(?!.*\\)[^:]+$",
        },
        "N": _object(
            {
                "coefficient": {
                    "type": "string",
                    "pattern": r"^(0|-?[1-9][0-9]{0,95})$",
                },
                "scale": {"type": "integer", "minimum": 0, "maximum": 38},
                "source_scale": _nullable(
                    {"type": "integer", "minimum": 0, "maximum": 38}
                ),
            }
        ),
        "Money": _shape("amount:N commodity_id:ID"),
        "EvidenceRef": _shape("kind:source_record/declaration id:ID"),
        "StepRef": _shape("txn_id:ID step_key:ID"),
        "Binding": _shape(
            "source_scope_id:ID source_section:ID source_position_key:ID?"
        ),
        "Rounding": _object(
            {
                "scale": {"type": "integer", "minimum": 0, "maximum": 38},
                "mode": _enum("half_even/half_up/toward_zero"),
                "residual": {"const": "final_slice"},
            }
        ),
    }
    tables = tabular_schema()["tables"]
    ref_variants = []
    for table in tables:
        columns = {c["name"]: c for c in table["columns"]}
        key_types = [
            _ref({"string": "ID", "integer": "I", "date": "D"}[columns[k]["kind"]])
            for k in table["primary_key"]
            if k != "entity_id"
        ]
        ref_variants.append(
            _object(
                {
                    "table": {"const": table["name"]},
                    "key": {
                        "type": "array",
                        **({"prefixItems": key_types} if key_types else {}),
                        "items": False,
                        "minItems": len(key_types),
                        "maxItems": len(key_types),
                    },
                }
            )
        )
    definitions["RecordRef"] = {"oneOf": ref_variants}
    definitions["Guard"] = _object(
        {
            "target": _ref("RecordRef"),
            "fields": {**_ref("L"), "minItems": 1},
            "expected_digest": _ref("Digest"),
        }
    )
    definitions["TypedValue"] = {
        "oneOf": [
            _ref("S"),
            _ref("I"),
            _ref("B"),
            _ref("N"),
            _array({"oneOf": [_ref("S"), _ref("I"), _ref("B"), _ref("N")]}),
            {"type": "null"},
        ]
    }
    definitions["Assignment"] = _shape("field:ID value:TypedValue")
    definitions["RuleMatchValue"] = {
        "anyOf": [_ref("TypedValue"), _array(_ref("TypedValue"))]
    }
    return definitions


@_isolated_schema
def _declaration_definitions() -> dict:
    defs: dict[str, dict] = {}

    def variant(kind: str, fields: dict) -> None:
        defs[kind] = _object(
            {
                "schema_version": {"const": 1},
                "kind": {"const": kind},
                **fields,
            }
        )

    variant(
        "account",
        {
            **_table_fields("accounts", exclude=("declaration_id",)),
            "bindings": _array(_ref("Binding")),
            "publication_lag_days": {"type": "integer", "minimum": 0},
            "period_rule": _enum("calendar/monthly_cycle/explicit/none"),
            "expected_periods": _array(_shape("start:D end:D due:D")),
            "coverage_basis_ids": _ref("L"),
        },
    )
    defs["account"]["properties"].update(
        {
            "cycle_end_day": {"type": "integer", "minimum": 1, "maximum": 31},
            "closing_tolerance_days": {"type": "integer", "minimum": 0, "maximum": 13},
        }
    )
    defs["account"]["required"] = [
        key
        for key in defs["account"]["properties"]
        if key not in {"cycle_end_day", "closing_tolerance_days"}
    ]
    defs["account"]["allOf"] = [
        {
            "if": {"properties": {"period_rule": {"const": "monthly_cycle"}}},
            "then": {"required": ["cycle_end_day", "closing_tolerance_days"]},
            "else": {
                "not": {
                    "anyOf": [
                        {"required": ["cycle_end_day"]},
                        {"required": ["closing_tolerance_days"]},
                    ]
                }
            },
        }
    ]
    variant("category", _table_fields("categories", exclude=("declaration_id",)))
    variant(
        "commodity",
        {
            **_table_fields("commodities"),
            "symbols": _array(
                _object(_table_fields("commodity_symbols", exclude=("commodity_id",)))
            ),
        },
    )
    variant("counterparty", _fields("counterparty_id:ID name:S"))
    variant(
        "position",
        {
            **_table_fields("positions", exclude=("declaration_id",)),
            "bindings": _array(_ref("Binding")),
            "booking_declaration_id": _nullable(_ref("ID")),
        },
    )
    variant(
        "assertion_scope",
        {
            **_table_fields("assertion_scopes", exclude=("declaration_id",)),
            "quote_commodity_id": _nullable(_ref("ID")),
            "valuation_evidence": _array(_ref("EvidenceRef")),
            "tolerance": _ref("N"),
        },
    )
    variant(
        "action_scope",
        _fields(
            "commodity_id:ID selection:instrument_all/explicit "
            "account_ids:L position_ids:L lot_ids:L pool_ids:L"
        ),
    )
    variant(
        "opening_lot",
        {
            **_fields(
                "opening_txn_id:ID posting_key:ID origin_key:ID position_id:ID "
                "units:N acquisition_date:D? principal:Money? fees_complete:B "
                "broker_lot_id:S? reference:N? reference_commodity_id:ID?"
            ),
            "fees": _array(
                _shape(
                    "fee_key:ID amount:Money "
                    "treatment:capitalized/expensed/attribution_only"
                )
            ),
            "evidence": _array(_ref("EvidenceRef")),
        },
    )
    variant(
        "corporate_action",
        {
            **_table_fields("corporate_actions", exclude=("action_id", "txn_id")),
            **_fields(
                "event_key:ID event_state:pending/recognized/cancelled/retracted "
                "review_state:reviewed/unreviewed/ignored"
            ),
            "effects": _array(
                _object(
                    _table_fields(
                        "corporate_action_effects",
                        exclude=("action_id", "effect_id", "effect_index"),
                    )
                )
            ),
            "evidence": _array(_ref("EvidenceRef")),
        },
    )
    variant(
        "booking",
        {
            **_fields(
                "method:STRICT/FIFO/LIFO/AVERAGE/NONE "
                "acquisition_date_role:traded/posted/settled/value "
                "fee_attribution_declaration_id:ID rounding:Rounding"
            ),
            "position_ids": {**_ref("L"), "minItems": 1},
            "tie_break": {"const": "origin_key"},
        },
    )
    variant(
        "pool",
        {
            **_fields(
                "pool_id:ID position_id:ID book_commodity_id:ID "
                "booking_declaration_id:ID fee_treatment:capitalized/expensed "
                "rounding:Rounding"
            ),
            "eligibility": _shape("mode:all_origins/listed_origins lot_ids:L"),
            "conversions": _array(
                _object(
                    {
                        **_fields(
                            "lot_id:ID "
                            "component_kind:principal/capitalized_fee/expensed_fee "
                            "source_commodity_id:ID target_commodity_id:ID "
                            "rate:N rounding:Rounding"
                        ),
                        "evidence": _array(_ref("EvidenceRef")),
                    }
                )
            ),
        },
    )
    variant(
        "fee_attribution",
        {
            **_fields(
                "method:source/pro_rata_units/explicit/none "
                "acquisition_treatment:capitalized/expensed/attribution_only "
                "rounding:Rounding"
            ),
            "slices": _array(
                _shape("fee_source:RecordRef target:RecordRef amount:Money")
            ),
        },
    )
    variant(
        "ordering",
        {
            **_fields("before:StepRef after:StepRef reason:S"),
            "guards": _array(_ref("Guard"), minimum=1),
            "evidence": _array(_ref("EvidenceRef")),
        },
    )
    variant(
        "rule",
        {
            "order": {"type": "integer", "minimum": 0},
            "matches": _array(
                _shape("field:ID operator:equals/in/contains value:RuleMatchValue")
            ),
            "assignments": _array(_ref("Assignment")),
            "split": _array(_shape("child_key:ID amount:N category_id:ID?")),
        },
    )
    defs.update(_terms_definitions())
    variant(
        "contract_terms",
        {
            **_fields(
                "commodity_id:ID? payoff:Payoff settlement:Settlement "
                "collateral:Collateral expiry:D?"
            ),
            "deliverables": _array(_ref("Deliverable")),
            "evidence": _array(_ref("EvidenceRef"), minimum=1),
        },
    )
    transaction = _table_fields("transactions", exclude=("txn_id",))
    transaction["legs"] = _array(
        _object(
            {
                "posting_key": _ref("ID"),
                **_table_fields(
                    "postings",
                    exclude=("posting_id", "posting_index", "txn_id", "step_id"),
                ),
            }
        )
    )
    assertion = _table_fields(
        "balance_assertions", exclude=("assertion_set_id", "statement_id")
    )
    assertion["observations"] = _array(
        _object(
            {
                "observation_key": _ref("ID"),
                **_table_fields("balances", exclude=("balance_id", "assertion_set_id")),
            }
        )
    )
    note = {"narration": _ref("S"), "date": _ref("D")}
    note.update(
        {k: v for k, v in _table_fields("prices").items() if k.startswith("timestamp_")}
    )
    defs["authored_fact"] = {
        "oneOf": [
            _object(
                {
                    "schema_version": {"const": 1},
                    "kind": {"const": "authored_fact"},
                    "event_key": _ref("ID"),
                    "fact_kind": {"const": kind},
                    "record": _object(fields),
                    "evidence": _array(_ref("EvidenceRef")),
                }
            )
            for kind, fields in {
                "transaction": transaction,
                "price": _table_fields("prices", exclude=("price_id", "origin")),
                "assertion": assertion,
                "note": note,
            }.items()
        ]
    }
    return defs


@_isolated_schema
def _terms_definitions() -> dict:
    payoff = [
        _object(
            {
                "kind": {"const": kind},
                **_fields(
                    "reference_commodity_id:ID settlement_commodity_id:ID multiplier:N"
                ),
            }
        )
        for kind in ("linear_difference", "inverse_difference")
    ]
    payoff.extend(
        [
            _object(
                {
                    "kind": {"const": "option"},
                    **_fields(
                        "right:call/put underlying_id:ID strike:Money multiplier:N "
                        "exercise_style:european/american/bermudan exercise_start:D?"
                    ),
                    "exercise_dates": _array(_ref("D")),
                }
            ),
            _object(
                {
                    "kind": {"const": "stated_cashflows"},
                    "flows": _array(
                        _shape(
                            "flow_key:ID date:D "
                            "role:principal/interest/redemption amount:Money"
                        )
                    ),
                }
            ),
            _object(
                {
                    "kind": {"const": "conditional"},
                    "underlying_ids": _ref("L"),
                    "conditions": _array(_ref("Condition")),
                    "cashflows": _array(
                        _shape("flow_key:ID condition_key:ID date:D? amount:Money?")
                    ),
                }
            ),
            _object(
                {
                    "kind": {"const": "complete_set"},
                    "outcome_ids": {**_ref("L"), "minItems": 2},
                    "units_per_outcome": _ref("N"),
                    "redemption": _ref("Money"),
                    "resolution_evidence_required": {"const": True},
                }
            ),
        ]
    )
    return {
        "Payoff": {"oneOf": payoff},
        "Settlement": _shape(
            "mode:cash/physical/mixed variation:final/collateral_only/none "
            "reference_reset:on_final_variation/none rounding:Rounding"
        ),
        "Collateral": {
            "oneOf": [
                _object({"mode": {"const": "none"}}),
                _object(
                    {
                        "mode": {"const": "separate_position"},
                        "position_ids": {**_ref("L"), "minItems": 1},
                        "commodity_ids": {**_ref("L"), "minItems": 1},
                    }
                ),
            ]
        },
        "Deliverable": _shape(
            "deliverable_key:ID commodity_id:ID units_per_contract:N "
            "role:underlying/strike_cash/redemption condition_key:ID?"
        ),
        "Condition": _object(
            {
                **_fields(
                    "condition_key:ID kind:barrier/autocall/default/conversion "
                    "underlying_id:ID? level:Money? "
                    "monitoring:continuous/dated/source_event"
                ),
                "comparison": _nullable(_enum("gte/lte")),
                "observation_dates": _array(_ref("D")),
                "event_evidence_required": {"const": True},
            }
        ),
    }


@_isolated_schema
def declaration_schema() -> dict:
    """Return the closed union of all supported declaration payloads."""
    defs = {**_common_definitions(), **_declaration_definitions()}
    custody = journal_entry_schema()
    for kind in (
        "assign",
        "split",
        "identity",
        "statement_revision",
        "transfer",
        "link",
    ):
        defs[kind] = {
            key: value
            for key, value in custody.items()
            if key not in {"$schema", "$defs"}
        }
        defs[kind]["properties"] = {**custody["properties"], "kind": {"const": kind}}
    kinds = next(
        c["enum"]
        for t in tabular_schema()["tables"]
        if t["name"] == "declarations"
        for c in t["columns"]
        if c["name"] == "declaration_kind"
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        "oneOf": [_ref(kind) for kind in kinds],
    }


@_isolated_schema
def journal_entry_schema() -> dict:
    """Describe immutable custody metadata for a retained decision body."""
    definitions = _common_definitions()
    digest = _ref("Digest")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            **{name: definitions[name] for name in ("ID", "S")},
            "Digest": {
                **definitions["Digest"],
                "minLength": 64,
                "maxLength": 64,
            },
        },
        **_object(
            {
                "schema_version": {"type": "integer", "const": 1},
                "sequence": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2**63 - 1,
                },
                "previous_entry_digest": _nullable(digest),
                "entity_id": _ref("ID"),
                "declaration_key": _ref("ID"),
                "kind": _enum("assign/split/identity/statement_revision/transfer/link"),
                "operation": _enum("record/supersede/retract"),
                "prior_entry_digest": _nullable(digest),
                "reviewed_attempt_digest": digest,
                "reason": {**_ref("S"), "minLength": 1},
                "payload_digest": _nullable(digest),
            }
        ),
        "allOf": [
            {
                "if": {"properties": {"sequence": {"const": 1}}},
                "then": {"properties": {"previous_entry_digest": {"type": "null"}}},
                "else": {"properties": {"previous_entry_digest": digest}},
            },
            {
                "if": {"properties": {"operation": {"const": "record"}}},
                "then": {"properties": {"prior_entry_digest": {"type": "null"}}},
                "else": {"properties": {"prior_entry_digest": digest}},
            },
            {
                "if": {"properties": {"operation": {"const": "retract"}}},
                "then": {"properties": {"payload_digest": {"type": "null"}}},
                "else": {"properties": {"payload_digest": digest}},
            },
        ],
    }


@_isolated_schema
def journal_head_schema() -> dict:
    """Describe an empty journal or its exact committed sequence and digest."""
    digest = {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
        "minLength": 64,
        "maxLength": 64,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_object(
            {
                "schema_version": {"type": "integer", "const": 1},
                "sequence": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2**63 - 1,
                },
                "entry_digest": _nullable(digest),
            }
        ),
        "if": {"properties": {"sequence": {"const": 0}}},
        "then": {"properties": {"entry_digest": {"type": "null"}}},
        "else": {"properties": {"entry_digest": digest}},
    }


@_isolated_schema
def decision_body_schema() -> dict:
    """Describe closed reviewed operations, without caller-selected guards."""
    source = _common_definitions()
    definitions = {
        name: source[name]
        for name in (
            "S",
            "ID",
            "D",
            "L",
            "Digest",
            "N",
        )
    }
    definitions["TL"] = _common_definitions()["TL"]
    definitions["ReviewBasisRef"] = _shape("attempt_digest:Digest basis_digest:Digest")
    definitions["EventRef"] = _shape("entity_id:ID event_id:Digest")
    definitions["LegRef"] = _shape("entity_id:ID leg_id:Digest")
    definitions["TargetRef"] = {"oneOf": [_ref("EventRef"), _ref("LegRef")]}
    definitions["DateRole"] = _enum("authorized/traded/posted/settled/value")
    definitions["LegDate"] = {
        **_object(
            {"mode": _enum("inherit/value/unknown"), "value": _nullable(_ref("D"))}
        ),
        "if": {"properties": {"mode": {"const": "value"}}},
        "then": {"properties": {"value": _ref("D")}},
        "else": {"properties": {"value": {"type": "null"}}},
    }

    def action(kind: str, fields: dict) -> dict:
        return _object({"kind": {"const": kind}, **fields})

    event_assignments = [
        action(
            "event_text",
            {"field": _enum("payee/narration"), "value": _nullable(_ref("S"))},
        ),
        action("event_tags", {"tags": _ref("TL")}),
        action("event_review", {"state": _enum("reviewed/unreviewed/ignored")}),
        action("event_state", {"state": {"const": "cancelled"}}),
        action("event_date", {"role": _ref("DateRole"), "value": _nullable(_ref("D"))}),
    ]
    leg_assignments = [
        action("leg_category", {"category_id": _nullable(_ref("ID"))}),
        action("leg_date", {"role": _ref("DateRole"), "date": _ref("LegDate")}),
    ]
    definitions["Assignment"] = {"oneOf": [*event_assignments, *leg_assignments]}
    definitions["SplitChild"] = _shape("child_key:ID amount:N")

    def body(kind: str, fields: dict) -> dict:
        return _object(
            {
                "schema_version": {"type": "integer", "const": 1},
                "kind": {"const": kind},
                **_fields("entity_id:ID review:ReviewBasisRef"),
                **fields,
            }
        )

    assign = {
        **body(
            "assign",
            {
                "target": _ref("TargetRef"),
                "assignments": _array(_ref("Assignment"), minimum=1),
            },
        ),
        "if": {"properties": {"target": {"required": ["event_id"]}}},
        "then": {
            "properties": {"assignments": {"items": {"oneOf": event_assignments}}}
        },
        "else": {"properties": {"assignments": {"items": {"oneOf": leg_assignments}}}},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
        "oneOf": [
            assign,
            body(
                "split",
                {
                    "parent": _ref("LegRef"),
                    "children": _array(_ref("SplitChild"), minimum=2),
                },
            ),
            body(
                "identity",
                {
                    "mode": _enum("key_family/occurrence"),
                    "canonical": _ref("Digest"),
                    "members": _array(_ref("Digest"), minimum=1),
                },
            ),
            body(
                "statement_revision",
                {
                    "replaced": _array(_ref("Digest"), minimum=1),
                    "replacement": _array(_ref("Digest"), minimum=1),
                    **_fields(
                        "account:ID fact_kind:movement/balance basis:ID start:D end:D"
                    ),
                },
            ),
            {
                **body(
                    "transfer",
                    {
                        "outgoing": {**_array(_ref("Digest")), "uniqueItems": True},
                        "incoming": {**_array(_ref("Digest")), "uniqueItems": True},
                        "commodity": _ref("ID"),
                    },
                ),
                "anyOf": [
                    {"properties": {"outgoing": {"minItems": 1}}},
                    {"properties": {"incoming": {"minItems": 1}}},
                ],
            },
            body(
                "link",
                {
                    "link_kind": {"const": "document"},
                    "target": _ref("TargetRef"),
                    "documents": {
                        **_array(_ref("Digest"), minimum=1),
                        "uniqueItems": True,
                    },
                },
            ),
            body(
                "link",
                {
                    "link_kind": {"const": "lifecycle"},
                    "attempt": _ref("EventRef"),
                    "fee": _ref("EventRef"),
                },
            ),
        ],
    }


@_isolated_schema
def registry_schema() -> dict:
    """Return source registries without unchecked extension dictionaries."""
    defs = _common_definitions()
    fields = {
        "schema_version": {"const": 1},
        "observation_fields": _array(
            _shape(
                "field_name:ID value_type:decimal/text/date/json_text "
                "units:commodity/ordinal/none payload_schema:ID?"
            )
        ),
        "status_mappings": _array(
            _shape(
                "source_scope_id:ID source_field:ID source_token:S event_key:ID "
                "candidate_state:pending/recognized/cancelled/retracted"
            )
        ),
        "source_sequences": _array(
            _shape(
                "source_scope_id:ID sequence_domain:ID field_name:ID "
                "direction:ascending/descending"
            )
        ),
        "coverage_bases": _array(
            _shape(
                "basis_id:ID date_role:authorized/traded/posted/settled/value "
                "opening_boundary:before_first/after_last "
                "closing_boundary:before_first/after_last "
                "measurement:units/value balance_view:settled/available/held "
                "quote_commodity_id:ID?"
            )
        ),
        "field_registry": _array(
            _shape(
                "field_name:ID target_table:ID "
                "value_type:string/integer/boolean/date/decimal/list "
                "nullable:B units:commodity/ordinal/none "
                "rule_mode:first/union/prohibited correction_allowed:B"
            )
        ),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(fields),
    }


@_isolated_schema
def configuration_schema() -> dict:
    """Return explicit source precedence, rendering, and output requirements."""
    defs = _common_definitions()
    schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(
            {
                "schema_version": {"const": 1},
                "as_of": _ref("D"),
                "source_authority": _array(
                    _object(
                        {
                            **_fields(
                                "account_id:ID source_scope_id:ID "
                                "period_start:D period_end:D "
                                "fact_kind:movement/balance "
                                "coverage_basis_id:ID "
                                "mode:confirming/complete_replacement"
                            ),
                        }
                    )
                ),
                "rendering": _array(
                    _shape(
                        "entity_id:ID sink:beancount/hledger "
                        "target:RecordRef positive_name:ID negative_name:ID? "
                        "primary_date_role:authorized/traded/posted/settled/value"
                    )
                ),
                "outputs": _array(
                    _shape(
                        "sink:beancount/hledger/tabular/summary/evidence "
                        "required_checks:L"
                    )
                ),
                "declaration_source_order": _ref("L"),
                "ignored_sections": _array(
                    _shape("source_scope_id:ID source_section:ID reason:S")
                ),
            }
        ),
    }
    schema["required"].remove("ignored_sections")
    return schema


@_isolated_schema
def broker_match_schema() -> dict:
    """Return the closed retained broker allocation observation."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _common_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                **_fields(
                    "acquisition_source_record_id:ID disposal_source_record_id:ID "
                    "quantity:N commodity_id:ID broker_lot_id:ID?"
                ),
            }
        ),
    }


@_isolated_schema
def extraction_schema() -> dict:
    """Return source-scoped observations, never booked determinations."""
    defs = _common_definitions()
    defs["SourceDate"] = {
        "type": "string",
        "format": "source-date",
        "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    }
    time = {
        **_fields(
            "timestamp_date:SourceDate timestamp_local:S? timestamp_offset_seconds:I? "
            "timestamp_zone:ID? "
            "timestamp_precision:date/minute/second/fractional_second "
            "timestamp_fraction_digits:I?"
        ),
    }
    roles = ("authorized", "traded", "posted", "settled", "value")
    dates = {f"date_{role}": _nullable(_ref("SourceDate")) for role in roles}
    leg = _object(
        {
            **_fields(
                "posting_key:ID source_account_identifier:S? source_position_key:ID? "
                "commodity_token:S amount:N symbol_as_stated:S? broker_lot_id:S? "
                "price_per_unit:N? price_commodity_token:S? original_amount:N? "
                "original_commodity_token:S? fx_rate_as_stated:N? "
                "fx_base_commodity_token:S? fx_quote_commodity_token:S?"
            ),
            **dates,
            **{f"date_{role}_mode": _enum("inherit/value/unknown") for role in roles},
            "posting_role": _table_fields("postings")["posting_role"],
            "components": _array(
                _shape(
                    "component_key:ID "
                    "component_kind:principal/commission/fee/"
                    "tax_withheld/accrued_interest "
                    "amount:Money basis:total/per_unit"
                )
            ),
        }
    )
    variants = {
        "transaction": _object(
            {
                **dates,
                **_fields(
                    "description_as_stated:S? payee_as_stated:S? status_token:S?"
                ),
                "legs": _array(leg),
                "times": _array(
                    _object(
                        {
                            "date_role": _enum(
                                "authorized/traded/posted/settled/value/occurred/"
                                "initiated/expires"
                            ),
                            **time,
                        }
                    )
                ),
            }
        ),
        "assertion": _object(
            {
                **time,
                **_fields(
                    "date:SourceDate assertion_kind:opening/closing/point "
                    "measurement:units/value is_complete:B coverage_basis:ID "
                    "scope_kind:positions/account_net quote_commodity_token:S?"
                ),
                "observations": _array(
                    _shape(
                        "observation_key:ID source_position_key:ID? "
                        "commodity_token:S amount:N"
                    )
                ),
            }
        ),
        "price": _object(
            {
                **time,
                **_fields(
                    "commodity_token:S quote_commodity_token:S date:SourceDate rate:N "
                    "basis_as_stated:S? market:S?"
                ),
            }
        ),
        "corporate_action": _object(
            {
                **time,
                **_fields(
                    "commodity_token:S effective_date:SourceDate "
                    "announcement_date:SourceDate? "
                    "record_date:SourceDate? payment_date:SourceDate? status_token:S?"
                ),
                "action_kind": _table_fields("corporate_actions")["action_kind"],
                "effects": _array(
                    _object(
                        _table_fields(
                            "corporate_action_effects",
                            exclude=("effect_id", "effect_index", "action_id"),
                        )
                    )
                ),
            }
        ),
        "observation": _shape(
            "subject_source_record_id:ID? field_name:ID "
            "observation_kind:description/source_status/broker_lot/"
            "broker_match/broker_basis/broker_gain/notional/source_field "
            "value_type:decimal/text/date decimal_value:N? text_value:S? "
            "date_value:SourceDate? commodity_token:S?"
        ),
    }
    records = []
    for kind, payload in variants.items():
        records.append(
            _object(
                {
                    "kind": {"const": kind},
                    **_fields(
                        "source_record_id:ID source_section:ID record_locator:ID "
                        "external_record_id:S? event_key:ID "
                        "source_account_identifier:S? source_key_domain:ID? "
                        "source_key:ID?"
                    ),
                    "source_correction": _nullable(
                        _shape(
                            "source_key_domain:ID target_source_key:ID "
                            "action:replace/delete"
                        )
                    ),
                    "payload": payload,
                }
            )
        )
    defs["ExtractionTime"] = _object({"raw_value": _ref("S"), **time})
    defs["ExtractionEvidencePart"] = {
        "oneOf": [
            _object(
                {
                    **_fields(
                        "part_key:ID date_role:posted/traded/settled/value/authorized "
                        "completeness:complete/partial/unstated"
                    ),
                    "kind": {"const": "movement_interval"},
                    "start": _ref("ExtractionTime"),
                    "end": _ref("ExtractionTime"),
                    "record_locators": _ref("L"),
                }
            ),
            _object(
                {
                    **_fields(
                        "part_key:ID kind:balance_point/available_balance/"
                        "pending_snapshot "
                        "meaning:settled_position/available_funds/"
                        "available_credit/pending "
                        "completeness:complete/partial/unstated"
                    ),
                    "point": _ref("ExtractionTime"),
                    "record_locators": _ref("L"),
                }
            ),
            _object(
                {
                    **_fields("part_key:ID"),
                    "kind": {"const": "observation_set"},
                    "record_locators": _ref("L"),
                }
            ),
        ]
    }
    defs["ExtractionSection"] = _object(
        {
            **_fields(
                "section_id:ID source_class:statement/rolling/document institution:S? "
                "source_account_identifier:S? stated_currency:S?"
            ),
            "account_context": _array(_shape("field_name:ID text_value:S")),
            "revisions": _array(
                _shape("relationship:replaces/confirms source_blob_digest:ID")
            ),
            "evidence_parts": _array(_ref("ExtractionEvidencePart")),
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(
            {
                "schema_version": {"const": 1},
                **_fields("source_scope_id:ID source_blob_digest:ID importer_id:ID"),
                "status_mappings": _array(
                    _shape(
                        "source_field:ID source_token:S event_key:ID "
                        "candidate_state:pending/recognized/cancelled/retracted"
                    )
                ),
                "source_scope": _object(
                    {"sections": _array(_ref("ExtractionSection"))}
                ),
                "records": _array({"oneOf": records}),
                "unsupported_content": _array(
                    _shape(
                        "source_section:ID record_locator:ID event_family:ID reason:ID"
                    )
                ),
            }
        ),
    }


@_isolated_schema
def _publication_definitions() -> dict:
    defs = _common_definitions()
    defs.update(
        {
            "EntityRef": _shape("entity_id:ID target:RecordRef"),
            "InputRef": _object(
                {
                    **_fields("role:ID kind:ID digest:Digest"),
                    "ordinal": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                }
            ),
            "File": _shape("path:Path byte_digest:Digest"),
            "CheckValue": {
                "oneOf": [
                    _object({"kind": {"const": "unavailable"}, "reason": _ref("ID")}),
                    _object(
                        {
                            "kind": {"const": "decimal"},
                            "value": _ref("N"),
                            "commodity_id": _nullable(_ref("ID")),
                        }
                    ),
                    _object({"kind": {"const": "integer"}, "value": _ref("I")}),
                    _object({"kind": {"const": "boolean"}, "value": _ref("B")}),
                    _object({"kind": {"const": "text"}, "value": _ref("S")}),
                    _object({"kind": {"const": "identifiers"}, "value": _ref("L")}),
                ]
            },
            "Check": {
                **_object(
                    {
                        **_fields(
                            "check_id:ID check_kind:ID entity_id:ID "
                            "status:pass/fail/not_evaluable "
                            "severity:info/warning/error "
                            "expected:CheckValue observed:CheckValue"
                        ),
                        "scope": _array(_ref("RecordRef")),
                        "evidence": _array(_ref("EvidenceRef")),
                        "policy_declaration_ids": _ref("L"),
                        "exception_declaration_id": _nullable(_ref("ID")),
                    }
                ),
                "description": (
                    "Retained financial/model finding. Physical byte checks, "
                    "file inventory checks, and reader integrity verification "
                    "are performed against the descriptor and reader boundary, "
                    "not retained in manifest.checks."
                ),
            },
            "RetainedObject": _shape(
                "object_id:ID path:Path digest_algorithm:sha256/sha512 "
                "digest:S size_bytes:I"
            ),
            "BindingRecord": _object(
                {
                    **_fields("entity_id:ID source_scope_id:ID importer_id:ID"),
                    "account_mappings": _array(
                        _shape(
                            "source_section:ID source_account_identifier:S "
                            "account_id:ID "
                            "source_position_key:ID? position_id:ID?"
                        )
                    ),
                }
            ),
            "Accession": _shape(
                "accession_id:ID object_id:ID acquired_at:S original_name:S "
                "source_scope_id:ID? acquisition_method:manual/local_copy"
            ),
            "RecoverySet": _object(
                {
                    "vault_objects": _array(_ref("RetainedObject")),
                    "accession_records": _array(_ref("Accession")),
                    "authored_history": _array(
                        _shape("path:Path byte_digest:Digest revision_id:ID")
                    ),
                    "dependencies": _array(
                        _shape("name:ID version:ID path:Path byte_digest:Digest")
                    ),
                    "external_assets": _array(
                        _shape("asset_id:ID path:Path byte_digest:Digest")
                    ),
                    "runtime": _array(
                        _shape(
                            "runtime_id:ID version:ID platform:ID "
                            "path:Path byte_digest:Digest"
                        ),
                    ),
                }
            ),
            "SchemaResource": _shape(
                "schema_id:ID schema_version:I path:Path byte_digest:Digest"
            ),
            "RegistryResource": _shape(
                "registry_id:ID schema_id:ID path:Path "
                "byte_digest:Digest content_digest:Digest"
            ),
            "Derivation": _shape(
                "derivation_id:ID algorithm:ID version:ID declaration_ids:L "
                "units:ID content_digest:Digest"
            ),
            "Capability": _shape(
                "capability_id:ID stage:extraction/model/sink "
                "status:supported/unsupported "
                "event_families:L required_checks:L"
            ),
            "WriterSettings": _shape(
                "library:ID version:ID compression:none/snappy/gzip/brotli/zstd/lz4 "
                "dictionary:B statistics:B row_group_size:I data_page_version:S"
            ),
            "Toolchain": _shape("component:ID version:ID content_digest:Digest"),
        }
    )
    return defs


@_isolated_schema
def manifest_schema() -> dict:
    """Return the complete logical manifest shape before canonical encoding."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _publication_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "entities": _array(
                    _shape(
                        "entity_id:ID producer_version:ID schema_version:I "
                        "schema_digest:Digest "
                        "as_of:D execution_fingerprint:Digest"
                    ),
                    minimum=1,
                ),
                "tables": _array(
                    _shape(
                        "name:ID path:Path byte_digest:Digest "
                        "logical_digest:Digest row_count:I"
                    )
                ),
                "artifacts": _array(
                    {
                        **_shape(
                            "kind:extract/source_checks/model/model_checks/checks/ledger/summary/evidence/"
                            "inputs/envelope/project/execution "
                            "path:Path byte_digest:Digest "
                            "logical_digest:Digest schema_id:ID?"
                        ),
                        "if": {"properties": {"kind": {"const": "execution"}}},
                        "then": {
                            "properties": {
                                "schema_id": {"const": "execution_record_schema"}
                            }
                        },
                    }
                ),
                "schemas": _array(_ref("SchemaResource"), minimum=1),
                "registries": _array(_ref("RegistryResource")),
                "configuration": _shape("path:Path byte_digest:Digest"),
                "derivation_definitions": _shape("path:Path byte_digest:Digest"),
                "effective_declarations": _array(
                    _shape("entity_id:ID declaration_id:ID")
                ),
                "bindings": _array(_ref("BindingRecord")),
                "recovery_set": _ref("RecoverySet"),
                "inputs": _array(_ref("InputRef")),
                "writer_settings": _ref("WriterSettings"),
                "toolchain": _array(_ref("Toolchain"), minimum=1),
                "derivations": _array(_ref("Derivation")),
                "capabilities": _array(_ref("Capability")),
                "outputs": _array(
                    _shape(
                        "sink:beancount/hledger/tabular/summary/evidence "
                        "required_checks:L"
                    )
                ),
                "checks": _array(_ref("Check")),
                "logical_content_digest": _ref("Digest"),
                "numeric_limits": _object(
                    {
                        "coefficient_digits": {"const": 96},
                        "scale": {"const": 38},
                        "source_scale": {"const": 38},
                        "intermediate_digits": {"const": 384},
                        "intermediate_scale": {"const": 152},
                    }
                ),
            }
        ),
    }


@_isolated_schema
def derivation_definitions_schema() -> dict:
    """Describe retained semantic rules independently of the runtime."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _common_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "definitions": {
                    "type": "object",
                    "propertyNames": _ref("ID"),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"version": _ref("I")},
                        "required": ["version"],
                        "additionalProperties": {"type": "string", "minLength": 1},
                        "minProperties": 2,
                    },
                },
            }
        ),
    }


def _artifact_array(item: dict) -> dict:
    return {"type": "array", "items": item}


@_isolated_schema
def checks_artifact_schema() -> dict:
    """Describe the published execution-check document."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _publication_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "checks": _artifact_array(_ref("Check")),
            }
        ),
    }


def _artifact_column_schema(column: dict) -> dict:
    kind = column["kind"]
    if kind in {"string", "list"}:
        meaning = column["text_semantics"]
        text = {
            "type": "string",
            "text_semantics": meaning,
            **(
                {"format": "identifier", "minLength": 1}
                if meaning == "identifier"
                else {}
            ),
        }
        definition = (
            {**_artifact_array(text), "uniqueItems": True} if kind == "list" else text
        )
    elif kind == "date":
        definition = {"type": "string", "format": "date"}
    else:
        definition = {"type": kind}
    if "enum" in column:
        definition["enum"] = column["enum"]
    return _nullable(definition) if column["nullable"] else definition


@_isolated_schema
def model_artifact_schema() -> dict:
    """Describe the complete shared-table execution model wrapper."""
    tables = {
        table["name"]: _artifact_array(
            _object(
                {
                    column["name"]: _artifact_column_schema(column)
                    for column in table["columns"]
                }
            )
        )
        for table in tabular_schema()["tables"]
        if table["name"] != "build"
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _publication_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "tables": _object(tables),
                "findings": _artifact_array(
                    _shape("code:ID scope:ID blocking:B severity:info/warning/error")
                ),
                "determinations": _artifact_array(
                    _shape("record_id:ID status:resolved/blocked")
                ),
                "derivations": {
                    **_artifact_array(_ref("ID")),
                    "uniqueItems": True,
                },
                "review": review_evidence_schema(),
            }
        ),
    }


@_isolated_schema
def review_evidence_schema() -> dict:
    """Describe native boundaries, applicability and rule observations."""
    match_value = _nullable(
        {
            "anyOf": [
                {"type": "string"},
                {"type": "integer"},
                {"type": "boolean"},
                _object(
                    {
                        "coefficient": {"type": "string"},
                        "scale": {"type": "integer"},
                        "source_scale": _nullable({"type": "integer"}),
                    }
                ),
                _array({"type": "string"}),
            ]
        }
    )
    target = _object(
        {
            "entity_id": _ref("ID"),
            "table": _enum(
                "review_events/review_legs/review_sources/review_statements/"
                "review_merge/review_documents"
            ),
            "key": {"type": "array", "items": _ref("ID"), "minItems": 1, "maxItems": 1},
            "active": {"type": "boolean"},
            "fields": _array(
                _object(
                    {
                        "name": _ref("ID"),
                        "kind": _enum("string/integer/boolean/date/list"),
                        "nullable": {"type": "boolean"},
                        "value": {
                            "type": ["string", "integer", "boolean", "array", "null"],
                            "items": {"type": "string"},
                        },
                    }
                )
            ),
        }
    )
    return _object(
        {
            "tail": _array(target),
            "counts": _shape("pending_events:I blocked_records:I"),
            "proposals": _array(
                {
                    "oneOf": [
                        _object(
                            {
                                **_fields(
                                    "proposal_id:Digest mode:key_family/occurrence "
                                    "canonical:Digest members:L reason:S"
                                ),
                                "kind": {"const": "identity"},
                            }
                        ),
                        _object(
                            {
                                **_fields("proposal_id:Digest commodity:ID reason:S"),
                                "kind": {"const": "transfer"},
                                "outgoing": {
                                    **_array(_ref("Digest"), minimum=1),
                                    "uniqueItems": True,
                                },
                                "incoming": {
                                    **_array(_ref("Digest"), minimum=1),
                                    "uniqueItems": True,
                                },
                            }
                        ),
                    ]
                }
            ),
            "receipts": _array(
                _object(
                    {
                        "decision_key": _ref("ID"),
                        "entry_digest": _ref("Digest"),
                        "expected": target,
                        "candidates": _array(target),
                        "restore": _array(
                            _object(
                                {
                                    "entity_id": _ref("ID"),
                                    "table": _enum(
                                        "review_events/review_legs/review_sources/"
                                        "review_statements/review_merge/review_documents"
                                    ),
                                    "key": {
                                        "type": "array",
                                        "items": _ref("ID"),
                                        "minItems": 1,
                                        "maxItems": 1,
                                    },
                                    "candidates": _array(target),
                                }
                            )
                        ),
                        "outcome": _enum("applied/missing/ambiguous/inactive/changed"),
                        "expected_digest": _ref("Digest"),
                        "observed_digest": _nullable(_ref("Digest")),
                    }
                )
            ),
            "rules": _array(
                _object(
                    {
                        "txn_id": _ref("ID"),
                        "posting_id": _ref("ID"),
                        "target_posting_ids": {
                            **_array(_ref("ID")),
                            "uniqueItems": True,
                        },
                        "rule_id": _ref("ID"),
                        "rule_key": _ref("ID"),
                        "eligible": {"type": "boolean"},
                        "matched": {"type": "boolean"},
                        "predicates": _array(
                            _object(
                                {
                                    "field": _ref("ID"),
                                    "operator": _enum("equals/in/contains"),
                                    "value_type": _enum(
                                        "string/identifier/date/decimal/integer/boolean/list"
                                    ),
                                    "expected": _array(match_value, minimum=1),
                                    "observed": match_value,
                                    "matches": {"type": "boolean"},
                                }
                            )
                        ),
                        "assignments": _array(
                            _object(
                                {
                                    "field": _ref("ID"),
                                    "mode": _enum("first/union"),
                                    "value": match_value,
                                    "outcome": _enum("applied/shadowed/redundant"),
                                    "selected": {"type": "boolean"},
                                }
                            )
                        ),
                    }
                )
            ),
        }
    )


@_isolated_schema
def review_report_schema() -> dict:
    """Seal attempt lineage independently of financial output publication."""
    model = model_artifact_schema()
    head = journal_head_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": model["$defs"],
        **_object(
            {
                "schema_version": {"type": "integer", "const": 1},
                "attempt_digest": _ref("Digest"),
                "selection": _object(
                    {
                        "input_digest": _ref("Digest"),
                        "journal_head": _nullable(
                            {
                                key: value
                                for key, value in head.items()
                                if key not in {"$defs", "$schema"}
                            }
                        ),
                        "journal_headers": _array(_ref("Digest")),
                        "journal_bases": _array(_ref("Digest")),
                        "uncommitted_sequences": _array(
                            {"type": "integer", "minimum": 1}
                        ),
                    }
                ),
                "model": _nullable(
                    {
                        key: value
                        for key, value in model.items()
                        if key not in {"$defs", "$schema"}
                    }
                ),
                "checks": _array(_ref("Check")),
                "failure": _nullable(_shape("code:ID location:S")),
            }
        ),
    }


@_isolated_schema
def summary_artifact_schema() -> dict:
    """Describe the published summary sink document."""
    number = _ref("N")
    monthly_quantity = _object(
        {
            "month": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
            "account_id": _ref("ID"),
            "position_id": _ref("ID"),
            "commodity_id": _ref("ID"),
            "opening": number,
            "credits": number,
            "debits": number,
            "closing": number,
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _publication_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "entity_id": _ref("ID"),
                "as_of": _ref("D"),
                "comparison_baseline": _nullable(_ref("Digest")),
                "journal": _nullable(
                    _object(
                        {
                            "sequence": _ref("I"),
                            "entry_digest": _nullable(_ref("Digest")),
                            "active": _array(
                                _shape(
                                    "decision_key:ID "
                                    "kind:assign/split/identity/statement_revision/"
                                    "transfer/link "
                                    "logical_position:I"
                                )
                            ),
                            "uncommitted_sequences": _array(_ref("I")),
                        }
                    )
                ),
                "uncategorized_work": _artifact_array(
                    _object(
                        {
                            "commodity_id": _ref("ID"),
                            "account_id": _nullable(_ref("ID")),
                            "attribution": _enum("account/unattributed"),
                            "leg_count": _ref("I"),
                            "amount": number,
                        }
                    )
                ),
                "monthly_quantities": _artifact_array(monthly_quantity),
                "transit_balances": _artifact_array(monthly_quantity),
                "category_flows": _artifact_array(
                    _object(
                        {
                            "month": {
                                "type": "string",
                                "pattern": r"^\d{4}-\d{2}$",
                            },
                            "category_id": _ref("ID"),
                            "commodity_id": _ref("ID"),
                            "amount": number,
                        }
                    )
                ),
                "changes": _artifact_array(
                    _object(
                        {
                            "position_id": _ref("ID"),
                            "before": _nullable(number),
                            "after": number,
                            "change": _nullable(number),
                        }
                    )
                ),
                "review": _object(
                    {
                        "unreviewed_events": _ref("I"),
                        "uncategorized_events": _ref("I"),
                        "pending_events": _ref("I"),
                        "blocked_records": _ref("I"),
                    }
                ),
                "checks": _artifact_array(_ref("Check")),
            }
        ),
    }


@_isolated_schema
def descriptor_schema() -> dict:
    """Return the terminal checksum inventory with no self-checksum edge."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _publication_definitions(),
        **_object(
            {
                "schema_version": {"const": 1},
                "manifest_path": _ref("Path"),
                "manifest_digest": _ref("Digest"),
                "payloads": _array(_ref("File"), minimum=1),
            }
        ),
    }


@_isolated_schema
def execution_record_schema() -> dict:
    """Describe observed execution facts, not a restorable environment."""
    string = _common_definitions()["ID"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_object(
            {
                "schema_version": {"type": "integer", "const": 1},
                "producer": _object(
                    {
                        "kind": _enum("release/development"),
                        "name": {"const": "jbt"},
                        "version": string,
                        "source_commit": _nullable(string),
                    }
                ),
                "runtime": _object(
                    dict.fromkeys(
                        ("implementation", "version", "platform", "architecture"),
                        string,
                    )
                ),
                "distributions": _array(_object({"name": string, "version": string})),
                "importers": _array(
                    _object(
                        {
                            "importer_id": string,
                            "distribution": string,
                            "version": string,
                            "kind": _enum("release/development"),
                            "profile": string,
                            "profile_version": {"type": "integer", "minimum": 1},
                            "contract_version": {"type": "integer", "const": 1},
                            "maximum_bytes": {"type": "integer", "minimum": 1},
                        }
                    )
                ),
            }
        ),
    }


@_isolated_schema
def envelope_schema() -> dict:
    """Return named, ordered input edges and explicitly typed artifact payloads."""
    defs = _publication_definitions()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(
            {
                "schema_version": {"const": 1},
                "kind": _enum(
                    "extract/source_checks/model/model_checks/checks/ledger/summary/tabular"
                ),
                "producer": _shape("name:ID version:ID content_digest:Digest"),
                "execution_digest": _ref("Digest"),
                "configuration_digest": _ref("Digest"),
                "inputs": _array(_ref("InputRef")),
                "payload": _shape(
                    "schema_id:ID path:Path byte_digest:Digest logical_digest:Digest"
                ),
            }
        ),
    }


def validate_document(
    document: object, schema: dict, *, reference_scope: dict | None = None
) -> None:
    """Validate locally with an optional enclosing reference scope."""
    encoded = json.dumps(schema, sort_keys=True)
    if reference_scope is None:
        validator = _validator(encoded)
    else:
        root = _validator(json.dumps(reference_scope, sort_keys=True))
        validator = root.evolve(schema=_checked_schema(encoded))
    validator.validate(document)
    validate_json_values(document, "document")


@lru_cache(maxsize=128)
def _validator(encoded_schema: str) -> Validator:
    return Draft202012Validator(
        _checked_schema(encoded_schema), format_checker=_FORMATS
    )


def _checked_schema(encoded_schema: str) -> dict:
    _check_schema(encoded_schema)
    return json.loads(encoded_schema)


@lru_cache(maxsize=128)
def _check_schema(encoded_schema: str) -> None:
    schema = json.loads(encoded_schema)

    def local_references(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {
                    "$ref",
                    "$dynamicRef",
                    "$recursiveRef",
                } and not item.startswith("#/"):
                    message = "Only local schema references are permitted"
                    raise ValueError(message)
                local_references(item)
        elif isinstance(value, list):
            for item in value:
                local_references(item)

    local_references(schema)
    Draft202012Validator.check_schema(schema)
