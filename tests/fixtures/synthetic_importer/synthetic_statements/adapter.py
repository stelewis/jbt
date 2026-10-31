"""Synthetic dates denote UTC midnight; quantities are whole USD."""

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from jbt.domain.identity import AcquiredAnchor, source_record_digest
from jbt.domain.ids import SemanticKey
from jbt.extraction import ExtractionError

if TYPE_CHECKING:
    from jbt.extraction import ExtractionContext

SECTION = "synthetic-account"


@dataclass(frozen=True)
class Entry:
    key: str
    date: str
    units: int
    payee: str


@dataclass(frozen=True)
class Statement:
    start: str
    end: str
    balance: int
    entries: tuple[Entry, ...]
    complete: bool = field(kw_only=True)
    replaces: str | None = field(kw_only=True)

    def encode(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True).encode()


def _number(units: int) -> dict:
    if type(units) is not int:
        code = "synthetic_integer_required"
        raise ExtractionError(code, "amount")
    return {"coefficient": str(units), "scale": 0, "source_scale": 0}


def _time(day: str) -> dict:
    return {
        "timestamp_date": day,
        "timestamp_local": "00:00:00",
        "timestamp_offset_seconds": 0,
        "timestamp_zone": None,
        "timestamp_precision": "second",
        "timestamp_fraction_digits": None,
    }


class StatementImporter:
    contract_version = 1
    profile = "synthetic-statements"
    profile_version = 1
    maximum_bytes = 16_384

    def extract(self, data: bytes, context: ExtractionContext) -> dict:
        value = json.loads(data)
        entries = tuple(Entry(**row) for row in value.pop("entries"))
        statement = Statement(**value, entries=entries)
        if type(statement.complete) is not bool:
            code = "synthetic_completeness_required"
            raise ExtractionError(code, "statement")

        def record(kind: str, locator: str, key: str | None, payload: dict) -> dict:
            anchor = AcquiredAnchor(
                context.source_blob_digest,
                SemanticKey(context.source_scope_id),
                SemanticKey(SECTION),
                SemanticKey(locator),
            )
            return {
                "kind": kind,
                "source_record_id": source_record_digest(anchor),
                "source_section": SECTION,
                "record_locator": locator,
                "external_record_id": key,
                "event_key": kind,
                "source_account_identifier": "SYNTHETIC-ACCOUNT",
                "source_key_domain": "synthetic-key" if key is not None else None,
                "source_key": key,
                "source_correction": None,
                "payload": payload,
            }

        records = []
        for entry in entries:
            dates = {
                f"date_{role}": entry.date if role == "posted" else None
                for role in ("authorized", "traded", "posted", "settled", "value")
            }
            records.append(
                record(
                    "transaction",
                    f"entry:{entry.key}",
                    entry.key,
                    {
                        **dates,
                        "description_as_stated": entry.payee,
                        "payee_as_stated": entry.payee,
                        "status_token": "posted",
                        "times": [{"date_role": "posted", **_time(entry.date)}],
                        "legs": [
                            {
                                "posting_key": "cash",
                                "source_account_identifier": "SYNTHETIC-ACCOUNT",
                                "source_position_key": None,
                                "commodity_token": "USD",
                                "amount": _number(entry.units),
                                "posting_role": "principal",
                                "symbol_as_stated": None,
                                "broker_lot_id": None,
                                "price_per_unit": None,
                                "price_commodity_token": None,
                                "original_amount": None,
                                "original_commodity_token": None,
                                "fx_rate_as_stated": None,
                                "fx_base_commodity_token": None,
                                "fx_quote_commodity_token": None,
                                **dates,
                                **{
                                    f"{name}_mode": "unknown"
                                    if day is None
                                    else "value"
                                    for name, day in dates.items()
                                },
                                "components": [],
                            }
                        ],
                    },
                )
            )
            status = record(
                "observation",
                f"entry:{entry.key}:status",
                entry.key,
                {
                    "subject_source_record_id": records[-1]["source_record_id"],
                    "field_name": "source.status",
                    "observation_kind": "source_status",
                    "value_type": "text",
                    "text_value": "posted",
                    "decimal_value": None,
                    "date_value": None,
                    "commodity_token": None,
                },
            )
            status["event_key"] = "transaction"
            records.append(status)
        records.append(
            record(
                "assertion",
                "synthetic:balance",
                None,
                {
                    **_time(statement.end),
                    "date": statement.end,
                    "assertion_kind": "point",
                    "measurement": "units",
                    "is_complete": False,
                    "coverage_basis": "posted",
                    "scope_kind": "account_net",
                    "quote_commodity_token": None,
                    "observations": [
                        {
                            "observation_key": "balance",
                            "source_position_key": None,
                            "commodity_token": "USD",
                            "amount": _number(statement.balance),
                        }
                    ],
                },
            )
        )
        return {
            "schema_version": 1,
            "source_scope_id": context.source_scope_id,
            "source_blob_digest": context.source_blob_digest,
            "importer_id": context.importer_id,
            "status_mappings": [
                {
                    "source_field": "source.status",
                    "source_token": "posted",
                    "event_key": "transaction",
                    "candidate_state": "recognized",
                }
            ],
            "source_scope": {
                "sections": [
                    {
                        "section_id": SECTION,
                        "source_class": "statement",
                        "institution": "Synthetic Institution",
                        "source_account_identifier": "SYNTHETIC-ACCOUNT",
                        "stated_currency": "USD",
                        "account_context": [],
                        "revisions": [
                            {
                                "relationship": "replaces",
                                "source_blob_digest": statement.replaces,
                            }
                        ]
                        if statement.replaces is not None
                        else [],
                        "evidence_parts": [
                            {
                                "part_key": "posted",
                                "kind": "movement_interval",
                                "date_role": "posted",
                                "start": {
                                    "raw_value": statement.start,
                                    **_time(statement.start),
                                },
                                "end": {
                                    "raw_value": statement.end,
                                    **_time(statement.end),
                                },
                                "completeness": "complete"
                                if statement.complete
                                else "partial",
                                "record_locators": [
                                    f"entry:{entry.key}" for entry in entries
                                ],
                            },
                            {
                                "part_key": "balance",
                                "kind": "balance_point",
                                "meaning": "settled_position",
                                "point": {
                                    "raw_value": statement.end,
                                    **_time(statement.end),
                                },
                                "completeness": "unstated",
                                "record_locators": ["synthetic:balance"],
                            },
                        ],
                    }
                ]
            },
            "records": records,
            "unsupported_content": [],
        }


importer = StatementImporter()
