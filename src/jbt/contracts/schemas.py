"""Closed JSON Schema 2020-12 documents with local-only references."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, FormatChecker

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

from jbt.contracts.catalog import schema_document
from jbt.contracts.primitives import validate_json_values


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
    table = next(t for t in schema_document()["tables"] if t["name"] == name)
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
        if field in columns and "enum" in columns[field]:
            value = {"enum": columns[field]["enum"]}
        result[field] = _nullable(value) if kind.endswith("?") else value
    return result


def _common_definitions() -> dict:
    definitions = {
        "S": {"type": "string"},
        "ID": {"type": "string", "minLength": 1},
        "I": {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1},
        "B": {"type": "boolean"},
        "D": {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "L": {"type": "array", "items": _ref("ID"), "uniqueItems": True},
        "Digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "Path": {
            "type": "string",
            "minLength": 1,
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
            "source_scope_id:ID source_section:S source_position_key:ID?"
        ),
        "Rounding": _object(
            {
                "scale": {"type": "integer", "minimum": 0, "maximum": 38},
                "mode": _enum("half_even/half_up/toward_zero"),
                "residual": {"const": "final_slice"},
            }
        ),
    }
    tables = schema_document()["tables"]
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
    definitions["Selection"] = _shape("position_id:ID lot_id:ID units:N")
    return definitions


def _declaration_definitions() -> dict:
    defs = {}

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
            "period_rule": _enum("calendar/explicit/none"),
            "expected_periods": _array(_shape("start:D end:D due:D")),
            "coverage_basis_ids": _ref("L"),
        },
    )
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
        "link",
        {
            **_fields("link_id:ID terms_declaration_id:ID?"),
            "link_kind": {
                "enum": next(
                    c["enum"]
                    for t in schema_document()["tables"]
                    if t["name"] == "links"
                    for c in t["columns"]
                    if c["name"] == "link_kind"
                )
            },
            "members": _array(
                _shape("role:ID kind:transaction/posting/blob/lot/commodity id:ID")
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
        "identity",
        {
            **_fields(
                "record_kind:transaction/corporate_action event_key:ID "
                "authored_keys:L retired_ids:L successor_ids:L reason:S"
            ),
            "anchor": _shape("kind:source_record/authored_key id:ID"),
            "members": _array(_shape("source_record_id:ID event_key:ID")),
            "guards": _array(_ref("Guard")),
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
                _shape("field:ID operator:equals/in/contains value:TypedValue")
            ),
            "assignments": _array(_ref("Assignment")),
            "split": _array(_shape("child_key:ID amount:N category_id:ID?")),
        },
    )
    variant(
        "correction",
        {
            **_fields(
                "operation:assign/select_lots/supersede/retract target:RecordRef? "
                "prior_declaration_id:ID? reason:S"
            ),
            "journal_sequence": {"type": "integer", "minimum": 0},
            "guards": _array(_ref("Guard")),
            "assignments": _array(_ref("Assignment")),
            "selection": _array(_ref("Selection")),
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


def declaration_schema() -> dict:
    """Return the closed union of all supported declaration payloads."""
    defs = {**_common_definitions(), **_declaration_definitions()}
    kinds = next(
        c["enum"]
        for t in schema_document()["tables"]
        if t["name"] == "declarations"
        for c in t["columns"]
        if c["name"] == "declaration_kind"
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        "oneOf": [_ref(kind) for kind in kinds],
    }


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


def configuration_schema() -> dict:
    """Return explicit source precedence, rendering, and output requirements."""
    defs = _common_definitions()
    return {
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
                                "entity_id:ID scope:RecordRef "
                                "period_start:D period_end:D "
                                "fact_kind:movement/balance/action "
                                "coverage_basis_id:ID? "
                                "mode:confirming/complete_replacement"
                            ),
                            "authoritative_sources": _array(
                                _ref("RecordRef"), minimum=1
                            ),
                            "evidence": _array(_ref("EvidenceRef"), minimum=1),
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
            }
        ),
    }


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


def extraction_schema() -> dict:
    """Return source-scoped observations, never booked determinations."""
    defs = _common_definitions()
    time = {
        k: v for k, v in _table_fields("prices").items() if k.startswith("timestamp_")
    }
    dates = {
        k: v for k, v in _table_fields("transactions").items() if k.startswith("date_")
    }
    leg = _object(
        {
            **_fields(
                "posting_key:ID source_account_identifier:S? source_position_key:S? "
                "commodity_token:S amount:N symbol_as_stated:S? broker_lot_id:S? "
                "price_per_unit:N? price_commodity_token:S? original_amount:N? "
                "original_commodity_token:S? fx_rate_as_stated:N? "
                "fx_base_commodity_token:S? fx_quote_commodity_token:S?"
            ),
            **{
                k: v
                for k, v in _table_fields("postings").items()
                if k.startswith("date_") or k == "posting_role"
            },
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
                                "authorized/traded/posted/settled/value/occurred"
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
                    "date:D assertion_kind:opening/closing/point "
                    "measurement:units/value is_complete:B coverage_basis:ID "
                    "scope_kind:positions/account_net quote_commodity_token:S?"
                ),
                "observations": _array(
                    _shape(
                        "observation_key:ID source_position_key:S? "
                        "commodity_token:S amount:N"
                    )
                ),
            }
        ),
        "price": _object(
            {
                **time,
                **_fields(
                    "commodity_token:S quote_commodity_token:S date:D rate:N "
                    "basis_as_stated:S? market:S?"
                ),
            }
        ),
        "corporate_action": _object(
            {
                **time,
                **_fields(
                    "commodity_token:S effective_date:D announcement_date:D? "
                    "record_date:D? payment_date:D? status_token:S?"
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
            "field_name:ID observation_kind:description/source_status/broker_lot/"
            "broker_match/broker_basis/broker_gain/notional/source_field "
            "value_type:decimal/text/date decimal_value:N? text_value:S? "
            "date_value:D? commodity_token:S?"
        ),
    }
    records = []
    for kind, payload in variants.items():
        records.append(
            _object(
                {
                    "kind": {"const": kind},
                    **_fields(
                        "source_record_id:ID source_section:S record_locator:ID "
                        "external_record_id:S? event_key:ID "
                        "source_account_identifier:S?"
                    ),
                    "payload": payload,
                }
            )
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(
            {
                "schema_version": {"const": 1},
                **_fields("source_scope_id:ID source_blob_digest:ID importer_id:ID"),
                "source_scope": _object(
                    {
                        **_fields(
                            "source_class:statement/rolling/document institution:S? "
                            "period_start:D? period_end:D? "
                            "completeness:complete/partial/unstated"
                        ),
                        "source_accounts": _ref("L"),
                        "revisions": _array(
                            _shape(
                                "relationship:replaces/confirms source_blob_digest:ID"
                            )
                        ),
                    }
                ),
                "records": _array({"oneOf": records}),
                "unsupported_content": _array(
                    _shape(
                        "source_section:S record_locator:ID event_family:ID reason:ID"
                    )
                ),
            }
        ),
    }


def _publication_definitions() -> dict:
    defs = _common_definitions()
    defs.update(
        {
            "EntityRef": _shape("entity_id:ID target:RecordRef"),
            "InputRef": _shape("role:ID ordinal:I kind:ID digest:Digest"),
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
                            "source_section:S source_account_identifier:S "
                            "account_id:ID "
                            "source_position_key:S? position_id:ID?"
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
                        minimum=1,
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
                        "as_of:D execution_fingerprint:Digest is_dirty:B"
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
                    _shape(
                        "kind:extract/model/checks/ledger/summary/evidence "
                        "path:Path byte_digest:Digest "
                        "logical_digest:Digest schema_id:ID?"
                    )
                ),
                "schemas": _array(_ref("SchemaResource"), minimum=1),
                "registries": _array(_ref("RegistryResource")),
                "configuration": _shape("path:Path byte_digest:Digest"),
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


def envelope_schema() -> dict:
    """Return named, ordered input edges and explicitly typed artifact payloads."""
    defs = _publication_definitions()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        **_object(
            {
                "schema_version": {"const": 1},
                "kind": _enum("extract/model/checks/ledger/tabular"),
                "producer": _shape("name:ID version:ID content_digest:Digest"),
                "configuration_digest": _ref("Digest"),
                "inputs": _array(_ref("InputRef")),
                "payload": _shape(
                    "schema_id:ID path:Path byte_digest:Digest logical_digest:Digest"
                ),
            }
        ),
    }


def validate_document(document: object, schema: dict) -> None:
    """Validate without remote schema retrieval or implicit coercion."""
    _validator(json.dumps(schema, sort_keys=True)).validate(document)
    validate_json_values(document, "document")


@lru_cache(maxsize=16)
def _validator(encoded_schema: str) -> Validator:
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
    return Draft202012Validator(schema, format_checker=FormatChecker())
