"""Bind retained extraction documents to the shared cash record."""

# Exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.publication import validate_extraction
from jbt.domain.canonical import encode_json
from jbt.domain.cash import (
    CashCheck,
    CashEvent,
    CashObservation,
    CashStatement,
    Opening,
    model_checks,
    resolve_cash,
)
from jbt.domain.identity import AcquiredAnchor, child_id
from jbt.domain.ids import RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.importers.ofx import parse_boundary
from jbt.runtime.project import (
    Project,
    SourceRole,
    SourceSelection,
    document,
    objects,
    text,
)

DATE_ROLES = ("authorized", "traded", "posted", "settled", "value")
OPENING_LEG_COUNT = 2
type Evidence = tuple[str, str, str, str | None]

CASH_DERIVATIONS = {
    "cash-quantity-v1": {
        "version": 1,
        "position": "The declared quantity position receives the stated signed amount.",
        "boundary": "The selected external category receives the exact negated amount.",
        "weights": "Each leg has one consideration weight equal to its signed amount.",
        "opening": "Authored balanced legs initialize quantity before the first point.",
        "ordering": "Posted civil date, authored opening first, then stable event ID.",
        "commutativity": "External quantity additions commute within a civil day.",
        "identity": (
            "Retained source anchor and transaction component; "
            "authored declaration key and event key."
        ),
    },
    "cash-rule-v1": {
        "version": 1,
        "predicate": "Exact stated payee/memo equality; no coercion or fallback.",
        "selection": (
            "First matching rule in unique explicit integer order; "
            "all predicates must match."
        ),
        "effect": "Assign the declared category to the external boundary leg.",
        "failure": "A movement without matching external treatment is unsupported.",
    },
}


@dataclass(frozen=True, slots=True)
class ExtractInput:
    """One selected evidence context, decoded only at the boundary."""

    selection: SourceSelection
    payload: bytes


@dataclass(frozen=True, slots=True)
class FinancialRecord:
    """One resolved record and the findings required before publication."""

    tables: dict[str, list[dict[str, object]]]
    checks: tuple[CashCheck, ...]
    events: tuple[CashEvent, ...]
    opening: Opening
    statements: tuple[CashStatement, ...]
    derivations: tuple[str, ...]


def exact(value: object) -> ExactDecimal:
    """Decode a validated decimal group without float conversion."""
    row = document(value)
    scale, source = row["scale"], row["source_scale"]
    require(
        type(scale) is int and (source is None or type(source) is int),
        "decimal_scale",
        "record",
    )
    if not isinstance(scale, int) or not (source is None or isinstance(source, int)):
        raise ContractError("decimal_scale", "record")
    return ExactDecimal(text(row["coefficient"], "decimal"), scale, source)


def _key(*parts: object) -> str:
    return byte_digest(encode_json(list(parts), integer_strings=False))


class Rows:
    """Serialize explicit logical fields into the shared physical catalog."""

    def __init__(self, entity_id: str) -> None:
        """Create an empty complete table set for one declared entity."""
        self.entity = entity_id
        self.specs = {item["name"]: item for item in tabular_schema()["tables"]}
        self.tables: dict[str, list[dict[str, object]]] = {
            name: [] for name in self.specs if name != "build"
        }

    def add(
        self,
        table: str,
        fields: Mapping[str, object],
        *,
        evidence: Evidence | Mapping[str, Evidence] | None,
    ) -> dict[str, object]:
        """Fill only absent nullable cells; required fields never default."""
        spec = self.specs[table]
        logical = {"entity_id": self.entity, **fields}
        require(
            set(logical) <= set(spec["logical_fields"]),
            "unknown_projection_field",
            table,
        )
        row: dict[str, object] = {}
        for name, encoding in spec["logical_fields"].items():
            if name not in logical:
                require(
                    encoding.endswith("?"),
                    "missing_projection_field",
                    f"{table}.{name}",
                )
                value = None
            else:
                value = logical[name]
            if encoding.removesuffix("?") == "decimal":
                require(
                    value is None or isinstance(value, ExactDecimal),
                    "projection_decimal",
                    table,
                )
                for suffix in ("coefficient", "scale", "source_scale"):
                    row[f"{name}_{suffix}"] = (
                        getattr(value, suffix) if value is not None else None
                    )
            else:
                row[name] = value
        self.tables[table].append(row)
        if evidence is not None:
            key = [logical[field] for field in spec["primary_key"][1:]]
            for field, value in fields.items():
                if field in spec["primary_key"] or value is None:
                    continue
                kind, identifier, origin, derivation = (
                    evidence[field] if isinstance(evidence, Mapping) else evidence
                )
                self.add(
                    "provenance",
                    {
                        "provenance_id": _key(table, key, field, kind, identifier),
                        "record_kind": table,
                        "record_id": encode_json(key, integer_strings=False)
                        .decode()
                        .strip(),
                        "field_name": field,
                        "value_origin": origin,
                        "evidence_kind": kind,
                        "evidence_id": identifier,
                        "derivation_id": derivation,
                    },
                    evidence=None,
                )
        return row

    def finish(self) -> dict[str, list[dict[str, object]]]:
        """Order by the catalog, preserving duplicate rows for validation."""
        for name, rows in self.tables.items():
            keys = self.specs[name]["sort_key"]
            rows.sort(
                key=lambda row: tuple((row[key] is not None, row[key]) for key in keys)
            )
        return self.tables


def _declarations(rows: Rows, project: Project) -> None:
    dimensions = {
        "account": "accounts",
        "category": "categories",
        "commodity": "commodities",
        "counterparty": "counterparties",
        "position": "positions",
        "assertion_scope": "assertion_scopes",
    }
    for item in project.declarations:
        rows.add(
            "declarations",
            {
                "declaration_id": item.revision,
                "declaration_key": item.key,
                "declaration_kind": item.kind,
                "revision_digest": item.revision,
                "document_path": "project.json",
                "document_locator": f"json:declarations/{item.key}",
                "payload_json": item.payload.decode().strip(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            },
            evidence=None,
        )
        if item.kind not in dimensions:
            continue
        payload = item.decode()
        table = dimensions[item.kind]
        fields: dict[str, object] = {
            key: value
            for key, value in payload.items()
            if key in rows.specs[table]["logical_fields"]
        }
        if "declaration_id" in rows.specs[table]["logical_fields"]:
            fields["declaration_id"] = item.revision
        for group in rows.specs[table]["numeric_groups"]:
            if fields.get(group["name"]) is not None:
                fields[group["name"]] = exact(fields[group["name"]])
        rows.add(
            table, fields, evidence=("declaration", item.revision, "declaration", None)
        )
        if item.kind == "commodity":
            for symbol in objects(payload["symbols"], item.key):
                rows.add(
                    "commodity_symbols",
                    {
                        **symbol,
                        "commodity_id": payload["commodity_id"],
                    },
                    evidence=("declaration", item.revision, "declaration", None),
                )


def _interval(extract: Mapping[str, object]) -> tuple[date, date]:
    scope = document(extract["source_scope"])
    boundaries = []
    records = objects(extract["records"], "extract.records")
    for field, role in (
        ("source.dtstart", "period_start"),
        ("source.dtend", "period_end"),
    ):
        values = [
            document(record["payload"])["text_value"]
            for record in records
            if record["kind"] == "observation"
            and document(record["payload"])["field_name"] == field
        ]
        require(len(values) == 1, "source_interval_evidence", field)
        civil, instant = parse_boundary(text(values[0], field))
        require(
            instant is not None
            and instant.hour == instant.minute == instant.second == 0
            and civil.isoformat() == scope[role],
            "source_interval_boundary_unsupported",
            field,
        )
        boundaries.append(civil)
    start, end = boundaries
    require(start < end, "source_interval_empty", "extract")
    return start, end


def _source_rows(
    rows: Rows,
    project: Project,
    selected: ExtractInput,
) -> tuple[CashStatement | None, tuple[str, date, ExactDecimal] | None]:
    extract = document(decode_document(selected.payload, "extract"))
    validate_extraction(extract)
    require(
        extract["unsupported_content"] == [], "unsupported_source_content", "extract"
    )
    binding = selected.selection
    require(
        extract["source_scope_id"] == binding.source_scope_id
        and extract["importer_id"] == "ofx",
        "source_scope_binding",
        "extract",
    )
    scope = document(extract["source_scope"])
    require(scope["revisions"] == [], "source_revisions_unsupported", "extract")
    require(scope["completeness"] == "unstated", "ofx_coverage_unstated", "extract")
    require(
        scope["source_accounts"] == [binding.source_account_identifier],
        "source_account_binding",
        "extract",
    )
    account = project.one("account").decode()
    require(binding.account_id == account["account_id"], "account_binding", "extract")
    require(account["civil_zone"] == "Etc/UTC", "cash_zone_unsupported", "account")
    commodity = project.one("commodity").decode()
    require(
        len(objects(commodity["symbols"], "symbols")) == 1,
        "single_cash_symbol_required",
        "record",
    )
    tokens = {
        text(symbol["symbol"], "symbol")
        for symbol in objects(commodity["symbols"], "symbols")
        if symbol["source_scope_id"] in {None, binding.source_scope_id}
        and symbol["exchange"] is None
    }
    require(len(tokens) == 1, "currency_binding", "extract")
    assertion_scope = project.one("assertion_scope").decode()
    blob = text(extract["source_blob_digest"], "extract")
    statement_id = _key("statement", blob, binding.source_scope_id)
    is_statement = binding.role == SourceRole.STATEMENT
    start, end = _interval(extract)
    rows.add(
        "statements",
        {
            "statement_id": statement_id,
            "account_id": binding.account_id,
            "institution": scope["institution"],
            "source_class": scope["source_class"],
            "period_start": start.isoformat(),
            "period_end": (end - timedelta(days=1)).isoformat(),
            "stated_currency": next(iter(tokens)),
            "source_format": "ofx",
            "is_eclipsed": False,
            "coverage_basis": assertion_scope["coverage_basis"],
        },
        evidence=None,
    )
    movements = []
    balances = []
    records = objects(extract["records"], "extract.records")
    for record in records:
        identifier = text(record["source_record_id"], "source_record_id")
        require(
            record["source_account_identifier"] == binding.source_account_identifier,
            "source_record_account_binding",
            identifier,
        )
        rows.add(
            "source_records",
            {
                "source_record_id": identifier,
                "source_blob_digest": blob,
                "source_scope_id": binding.source_scope_id,
                "source_section": record["source_section"],
                "record_locator": record["record_locator"],
                "statement_id": statement_id,
                "input_kind": "bank_statement",
                "external_record_id": record["external_record_id"],
            },
            evidence=None,
        )
        if record["kind"] == "transaction":
            movements.append(_movement(record, records, blob, binding, tokens))
        elif record["kind"] == "assertion":
            balances.append(_balance_rows(rows, project, record, statement_id, tokens))
        elif record["kind"] == "observation":
            _observation(rows, project, record)
        else:
            raise ContractError("extract_record_unsupported", identifier)
    require(len(balances) == 1, "single_balance_required", statement_id)
    balance_id, balance_date, balance = balances[0]
    if not is_statement:
        require(not movements, "opening_evidence_movements", statement_id)
        return None, balances[0]
    return CashStatement(
        balance_id,
        start,
        end,
        balance_date,
        balance,
        scope["completeness"] == "complete",
        tuple(movements),
    ), None


def _balance_rows(
    rows: Rows,
    project: Project,
    record: Mapping[str, object],
    statement_id: str,
    tokens: set[str],
) -> tuple[str, date, ExactDecimal]:
    identifier = text(record["source_record_id"], "source_record")
    payload = document(record["payload"])
    observations = objects(payload["observations"], identifier)
    require(len(observations) == 1, "cash_single_balance", identifier)
    observation = observations[0]
    require(observation["commodity_token"] in tokens, "currency_binding", identifier)
    amount = exact(observation["amount"])
    require(
        payload["measurement"] == "units"
        and payload["scope_kind"] == "account_net"
        and payload["is_complete"] is False
        and payload["quote_commodity_token"] is None
        and payload["assertion_kind"] == "point"
        and payload["timestamp_local"] == "00:00:00"
        and payload["timestamp_offset_minutes"] == 0
        and payload["timestamp_precision"] == "second"
        and payload["timestamp_fraction_digits"] is None
        and payload["timestamp_date"] == payload["date"],
        "source_balance_basis_unsupported",
        identifier,
    )
    when = date.fromisoformat(text(payload["date"], identifier))
    assertion_id = _key("assertion", identifier)
    scope = project.one("assertion_scope")
    position = project.one("position")
    extracted: Evidence = ("source_record", identifier, "extracted", None)
    assertion_fields = {
        "assertion_set_id": assertion_id,
        "scope_id": scope.decode()["scope_id"],
        "date": when.isoformat(),
        "assertion_kind": payload["assertion_kind"],
        "is_complete": payload["is_complete"],
        "statement_id": statement_id,
        **{
            key: value for key, value in payload.items() if key.startswith("timestamp_")
        },
    }
    rows.add(
        "balance_assertions",
        assertion_fields,
        evidence={
            key: ("declaration", scope.revision, "declaration", None)
            if key == "scope_id"
            else extracted
            for key in assertion_fields
        },
    )
    rows.add(
        "balances",
        {
            "balance_id": _key("balance", identifier),
            "assertion_set_id": assertion_id,
            "position_id": position.decode()["position_id"],
            "commodity_id": position.decode()["commodity_id"],
            "amount": amount,
        },
        evidence={
            "assertion_set_id": extracted,
            "amount": extracted,
            "position_id": ("declaration", position.revision, "declaration", None),
            "commodity_id": (
                "declaration",
                project.one("commodity").revision,
                "declaration",
                None,
            ),
        },
    )
    return identifier, when, amount


def _observation(rows: Rows, project: Project, record: Mapping[str, object]) -> None:
    payload = document(record["payload"])
    identifier = text(record["source_record_id"], "source_record")
    fields = objects(
        document(decode_document(project.registries, "registries"))[
            "observation_fields"
        ],
        "observation_fields",
    )
    require(
        any(
            field["field_name"] == payload["field_name"]
            and field["value_type"] == payload["value_type"]
            for field in fields
        )
        and payload["commodity_token"] is None,
        "source_observation_registry",
        identifier,
    )
    rows.add(
        "observations",
        {
            "observation_id": _key("observation", identifier),
            "record_kind": "source_records",
            "record_id": encode_json([identifier], integer_strings=False)
            .decode()
            .strip(),
            "field_name": payload["field_name"],
            "observation_kind": payload["observation_kind"],
            "value_type": payload["value_type"],
            "decimal_value": exact(payload["decimal_value"])
            if payload["decimal_value"] is not None
            else None,
            "text_value": payload["text_value"],
            "date_value": payload["date_value"],
            "source_record_id": identifier,
        },
        evidence=None,
    )


def _movement(
    record: Mapping[str, object],
    records: tuple[dict, ...],
    blob: str,
    binding: SourceSelection,
    tokens: set[str],
) -> CashObservation:
    identifier = text(record["source_record_id"], "source_record")
    payload = document(record["payload"])
    legs = objects(payload["legs"], "extract.legs")
    require(len(legs) == 1, "cash_single_leg", identifier)
    leg = legs[0]
    require(leg["commodity_token"] in tokens, "currency_binding", identifier)
    require(
        leg["source_account_identifier"] == binding.source_account_identifier
        and leg["source_position_key"] is None
        and leg["posting_role"] == "principal",
        "source_leg_binding",
        identifier,
    )
    require(leg["components"] == [], "cash_components_unsupported", identifier)
    require(payload["status_token"] is None, "source_status_unsupported", identifier)
    require(
        all(payload[f"date_{role}"] is None for role in DATE_ROLES if role != "posted"),
        "source_date_unsupported",
        identifier,
    )
    for when in objects(payload["times"], "times"):
        require(
            when["date_role"] == "posted"
            and when["timestamp_date"] == payload["date_posted"]
            and when["timestamp_precision"] == "date"
            and all(
                when[field] is None
                for field in (
                    "timestamp_local",
                    "timestamp_offset_minutes",
                    "timestamp_zone",
                    "timestamp_fraction_digits",
                )
            ),
            "source_time_unsupported",
            identifier,
        )
    require(
        all(
            (leg[f"date_{role}_mode"] == "inherit" and leg[f"date_{role}"] is None)
            or (
                leg[f"date_{role}_mode"] == "value"
                and leg[f"date_{role}"] == payload[f"date_{role}"]
            )
            or (
                leg[f"date_{role}_mode"] == "unknown"
                and leg[f"date_{role}"] is None
                and payload[f"date_{role}"] is None
            )
            for role in DATE_ROLES
        ),
        "source_leg_date_unsupported",
        identifier,
    )
    require(
        all(
            leg[field] is None
            for field in (
                "broker_lot_id",
                "price_per_unit",
                "price_commodity_token",
                "original_amount",
                "original_commodity_token",
                "fx_rate_as_stated",
                "fx_base_commodity_token",
                "fx_quote_commodity_token",
            )
        ),
        "source_component_unsupported",
        identifier,
    )
    observations = [
        document(item["payload"])
        for item in records
        if item["kind"] == "observation"
        and item["external_record_id"] == record["external_record_id"]
    ]
    require(
        len({item["field_name"] for item in observations}) == len(observations),
        "source_field_ambiguous",
        identifier,
    )
    extra = {item["field_name"]: item["text_value"] for item in observations}
    require("source.trntype" in extra, "source_transaction_type_required", identifier)
    return CashObservation(
        AcquiredAnchor(
            blob,
            SemanticKey(binding.source_scope_id),
            SemanticKey(text(record["source_section"], identifier)),
            SemanticKey(text(record["record_locator"], identifier)),
        ),
        text(record["external_record_id"], identifier),
        date.fromisoformat(text(payload["date_posted"], identifier)),
        exact(leg["amount"]),
        text(extra["source.trntype"], identifier),
        text(leg["symbol_as_stated"], identifier)
        if leg["symbol_as_stated"] is not None
        else None,
        text(payload["payee_as_stated"], identifier)
        if payload["payee_as_stated"] is not None
        else None,
        text(extra["source.memo"], identifier)
        if extra.get("source.memo") is not None
        else None,
    )


def _opening(project: Project, evidence: tuple[str, date, ExactDecimal]) -> Opening:
    declaration = project.one("authored_fact")
    payload = declaration.decode()
    require(
        payload["fact_kind"] == "transaction",
        "authored_kind_unsupported",
        declaration.key,
    )
    record = document(payload["record"])
    require(
        record["event_kind"] == "opening" and record["event_state"] == "recognized",
        "authored_opening_required",
        declaration.key,
    )
    position = project.one("position").decode()
    legs = objects(record["legs"], declaration.key)
    require(len(legs) == OPENING_LEG_COUNT, "opening_legs", declaration.key)
    require(
        all(record[f"date_{role}"] is None for role in DATE_ROLES if role != "posted")
        and all(
            all(
                leg[f"date_{role}"] is None and leg[f"date_{role}_mode"] == "inherit"
                for role in DATE_ROLES
            )
            and all(
                leg[field] is None
                for field in (
                    "broker_lot_id",
                    "price_per_unit",
                    "price_commodity_id",
                    "original_amount",
                    "original_commodity_id",
                    "fx_rate_as_stated",
                    "fx_base_commodity_id",
                    "fx_quote_commodity_id",
                )
            )
            for leg in legs
        ),
        "opening_components_unsupported",
        declaration.key,
    )
    cash = tuple(leg for leg in legs if leg["leg_kind"] == "position")
    boundary = tuple(leg for leg in legs if leg["leg_kind"] == "boundary")
    require(len(cash) == len(boundary) == 1, "opening_legs", declaration.key)
    require(
        cash[0]["position_id"] == position["position_id"]
        and cash[0]["account_id"] == position["account_id"]
        and all(leg["commodity_id"] == position["commodity_id"] for leg in legs),
        "opening_position",
        declaration.key,
    )
    amount = exact(cash[0]["amount"])
    require(
        amount.rational().add(exact(boundary[0]["amount"]).rational()).numerator == 0,
        "opening_conservation",
        declaration.key,
    )
    posted = date.fromisoformat(text(record["date_posted"], declaration.key))
    require(
        posted < date.max and posted + timedelta(days=1) == evidence[1],
        "opening_boundary",
        declaration.key,
    )
    require(
        {"kind": "source_record", "id": evidence[0]}
        in objects(payload["evidence"], declaration.key),
        "opening_evidence_reference",
        declaration.key,
    )
    return Opening(
        declaration.key,
        declaration.revision,
        text(payload["event_key"], declaration.key),
        posted,
        amount,
        text(boundary[0]["category_id"], declaration.key),
        evidence[0],
        evidence[1],
        evidence[2],
    )


def _events(
    rows: Rows, project: Project, events: tuple[CashEvent, ...], opening: Opening
) -> None:
    position = project.one("position").decode()
    authored = document(project.one("authored_fact").decode()["record"])
    authored_legs = {
        leg["leg_kind"]: leg for leg in objects(authored["legs"], "opening")
    }
    for sequence, event in enumerate(events):
        identifier = event.identity.value
        is_opening = event.observation is None
        operation = "opening" if is_opening else "quantity"
        step_id = child_id(
            event.identity, RecordKind.STEP, SemanticKey(operation)
        ).value
        source_id = None
        if event.observation is not None:
            anchor = event.observation.anchor
            source_id = next(
                text(row["source_record_id"], "source")
                for row in rows.tables["source_records"]
                if row["source_blob_digest"] == anchor.blob_digest
                and row["source_scope_id"] == anchor.namespace.value
                and row["source_section"] == anchor.section.value
                and row["record_locator"] == anchor.locator.value
            )
        evidence = (
            ("declaration", opening.declaration_id, "declaration", None)
            if is_opening
            else (
                "source_record",
                text(source_id, "source"),
                "extracted",
                None,
            )
        )
        rows.add(
            "economic_identities",
            {
                "record_kind": "transaction",
                "record_id": identifier,
                "anchor_kind": "declaration" if is_opening else "source_record",
                "anchor_id": opening.declaration_key if is_opening else source_id,
                "event_key": opening.event_key if is_opening else "transaction",
                "identity_state": "active",
            },
            evidence=None,
        )
        transaction_fields = (
            {key: value for key, value in authored.items() if key != "legs"}
            if is_opening
            else {
                "event_kind": "transaction",
                "event_state": "recognized",
                "date_posted": event.posted.isoformat(),
                "payee": event.observation.name if event.observation else None,
                "narration": event.observation.memo if event.observation else None,
                "description_as_stated": event.observation.name
                if event.observation
                else None,
                "review_state": "unreviewed",
                "tags": [],
                "links": [],
            }
        )
        derived: Evidence = (evidence[0], evidence[1], "booking", "cash-quantity-v1")
        rows.add(
            "transactions",
            {"txn_id": identifier, **transaction_fields},
            evidence={
                key: evidence
                if is_opening
                or key in {"date_posted", "payee", "narration", "description_as_stated"}
                else derived
                for key in transaction_fields
            },
        )
        rows.add(
            "book_steps",
            {
                "step_id": step_id,
                "txn_id": identifier,
                "step_key": operation,
                "sequence": sequence,
                "operation_kind": operation,
                "effective_date": event.posted.isoformat(),
            },
            evidence=derived,
        )
        for index, leg_kind in enumerate(("position", "boundary")):
            cash = leg_kind == "position"
            posting_id = child_id(
                event.identity,
                RecordKind.LEG,
                SemanticKey(
                    text(authored_legs[leg_kind]["posting_key"], "opening")
                    if is_opening
                    else "cash"
                    if cash
                    else "boundary"
                ),
            ).value
            amount = (
                event.amount
                if cash
                else ExactDecimal(
                    str(-int(event.amount.coefficient)), event.amount.scale, None
                )
            )
            row = {
                "posting_id": posting_id,
                "posting_index": index,
                "txn_id": identifier,
                "commodity_id": position["commodity_id"],
                "amount": amount,
                "account_id": position["account_id"] if cash else None,
                "category_id": None if cash else event.category_id,
                "step_id": step_id,
                "posting_role": "cash" if cash else "principal",
                "leg_kind": leg_kind,
                "position_id": position["position_id"] if cash else None,
                "symbol_as_stated": event.observation.symbol_as_stated
                if cash and event.observation is not None
                else None,
                **{f"date_{role}_mode": "inherit" for role in DATE_ROLES},
            }
            if is_opening:
                original = authored_legs[leg_kind]
                row.update(
                    {
                        key: value
                        for key, value in original.items()
                        if key not in {"posting_key", "amount"}
                    }
                )
                row["amount"] = exact(original["amount"])
            posting_evidence = dict.fromkeys(row, evidence if is_opening else derived)
            if not is_opening:
                posting_evidence["amount"] = evidence if cash else derived
                posting_evidence["symbol_as_stated"] = evidence
                for key, kind in (
                    ("account_id", "account"),
                    ("position_id", "position"),
                    ("commodity_id", "commodity"),
                ):
                    posting_evidence[key] = (
                        "declaration",
                        project.one(kind).revision,
                        "declaration",
                        None,
                    )
                if not cash:
                    posting_evidence["category_id"] = (
                        "declaration",
                        event.policy_id,
                        "rule",
                        "cash-rule-v1",
                    )
            rows.add("postings", row, evidence=posting_evidence)
            rows.add(
                "posting_weights",
                {
                    "posting_id": posting_id,
                    "component_key": "consideration",
                    "weight_kind": "consideration",
                    "commodity_id": position["commodity_id"],
                    "amount": amount,
                },
                evidence=derived,
            )


def bind_sources(
    project: Project, extracts: tuple[ExtractInput, ...]
) -> tuple[Rows, Opening, tuple[CashStatement, ...]]:
    """Resolve source/account conventions without executing model operations."""
    require(len(extracts) == len(project.sources), "extract_selection", "record")
    require(
        tuple(item.selection for item in extracts) == project.sources,
        "extract_binding_order",
        "record",
    )
    account = project.one("account").decode()
    position = project.one("position").decode()
    scope = project.one("assertion_scope").decode()
    registries = document(decode_document(project.registries, "registries"))
    bases = [
        basis
        for basis in objects(registries["coverage_bases"], "coverage_bases")
        if basis["basis_id"] == scope["coverage_basis"]
    ]
    require(
        registries["source_sequences"] == registries["status_mappings"] == []
        and account["coverage_basis_ids"] == [scope["coverage_basis"]]
        and len(bases) == 1
        and bases[0]
        == {
            "basis_id": scope["coverage_basis"],
            "date_role": "posted",
            "opening_boundary": "before_first",
            "closing_boundary": "after_last",
            "measurement": "units",
            "balance_view": "settled",
            "quote_commodity_id": None,
        },
        "cash_source_policy_unsupported",
        "record",
    )
    require(
        account["bindings"] == position["bindings"] == []
        and position["account_id"] == account["account_id"]
        and account["closed_date"] is None,
        "cash_binding_policy_unsupported",
        "record",
    )
    require(
        scope["account_id"] == account["account_id"]
        and scope["measurement"] == "units"
        and scope["scope_kind"] == "positions"
        and scope["selection"] == "positions"
        and scope["position_ids"] == [position["position_id"]]
        and scope["quote_commodity_id"] is None
        and exact(scope["tolerance"]).coefficient == "0",
        "cash_assertion_scope_unsupported",
        "record",
    )
    rows = Rows(project.entity.value)
    _declarations(rows, project)
    statements = []
    evidence = []
    seen: dict[bytes, SourceSelection] = {}
    for selected in extracts:
        if selected.payload in seen:
            previous = seen[selected.payload]
            require(
                previous.role == selected.selection.role
                and previous.account_id == selected.selection.account_id
                and previous.source_scope_id == selected.selection.source_scope_id
                and previous.source_account_identifier
                == selected.selection.source_account_identifier,
                "source_binding_conflict",
                "record",
            )
            continue
        seen[selected.payload] = selected.selection
        statement, opening = _source_rows(rows, project, selected)
        if statement is not None:
            statements.append(statement)
        if opening is not None:
            evidence.append(opening)
    require(len(evidence) == 1, "opening_evidence_required", "record")
    opening = _opening(project, evidence[0])
    require(
        account["coverage_start_date"] == opening.evidence_date.isoformat()
        and account["opened_date"] is not None
        and date.fromisoformat(text(account["opened_date"], "account"))
        <= opening.posted,
        "cash_initialization_boundary",
        "record",
    )
    return rows, opening, tuple(statements)


def make_record(
    project: Project, extracts: tuple[ExtractInput, ...]
) -> FinancialRecord:
    """Calculate model quantities without claiming to run source checks."""
    rows, opening, sources = bind_sources(project, extracts)
    account = project.one("account").decode()
    position = project.one("position").decode()
    require(
        position["measurement_kind"] == position["inventory_method"] == "quantity",
        "cash_position_required",
        "record",
    )
    require(account["period_rule"] == "explicit", "explicit_periods_required", "record")
    configuration = project.configuration_document()
    as_of = date.fromisoformat(text(configuration["as_of"], "as_of"))
    require(
        all(source.end - timedelta(days=1) <= as_of for source in sources),
        "source_after_build_horizon",
        "record",
    )
    periods = tuple(
        (
            date.fromisoformat(text(period["start"], "period")),
            date.fromisoformat(text(period["end"], "period")),
        )
        for period in objects(account["expected_periods"], "periods")
        if date.fromisoformat(text(period["due"], "period")) <= as_of
    )
    for declaration in project.declarations:
        require(
            declaration.valid_from is None or declaration.valid_from <= opening.posted,
            "policy_not_effective",
            declaration.key,
        )
    for symbol in objects(project.one("commodity").decode()["symbols"], "symbols"):
        require(
            (
                symbol["valid_from"] is None
                or date.fromisoformat(text(symbol["valid_from"], "symbol"))
                <= opening.posted
            )
            and symbol["valid_to"] is None,
            "symbol_interval_unsupported",
            "record",
        )
    events = resolve_cash(project.entity, opening, sources, project.category_rules())
    checks = model_checks(
        opening,
        sources,
        events,
        expected_periods=periods,
        as_of=as_of,
    )
    _events(rows, project, events, opening)
    derivations = ("cash-quantity-v1", "cash-rule-v1")
    return FinancialRecord(rows.finish(), checks, events, opening, sources, derivations)
