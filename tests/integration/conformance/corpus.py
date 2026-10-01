"""Hand-authored contract specimens, not an event booking implementation.

JSON supplies every financial determination. This loader only expands
record scaffolding, exact decimal spelling, identities, and provenance.
Nullable omissions mean inapplicable fields; financial change components
must always be supplied explicitly, including unknown components.
"""

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from jbt.contracts.catalog import Table, catalog, tabular_schema
from jbt.domain.identity import (
    AcquiredAnchor,
    Occurrence,
    child_id,
    declaration_id,
    event_id,
    revision_id,
    successor_lot_id,
)
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey

FIXTURES = Path(__file__).parent / "fixtures"
ROUNDING = {"scale": 2, "mode": "half_even", "residual": "final_slice"}
DATE_ROLES = ("authorized", "traded", "posted", "settled", "value")


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    tables: dict[str, list[dict[str, object]]]
    expected_answers: dict
    evidence: dict
    oracle_notes: tuple[str, ...]
    domain_records: tuple[dict, ...]
    aliases: dict[str, str]
    negative_cases: tuple[dict, ...]


def exact(text: str | None, source_scale: int | None = None) -> dict | None:
    """Encode a literal, without computing any expected financial result."""
    if text is None:
        return None
    negative = text.startswith("-")
    integer, dot, fraction = text.removeprefix("-").partition(".")
    coefficient = (integer + fraction).lstrip("0") or "0"
    scale = len(fraction) if dot else 0
    while coefficient.endswith("0") and scale and coefficient != "0":
        coefficient = coefficient[:-1]
        scale -= 1
    if coefficient == "0":
        scale = 0
    elif negative:
        coefficient = "-" + coefficient
    return {
        "coefficient": coefficient,
        "scale": scale,
        "source_scale": source_scale,
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ref(value: str) -> str:
    return "@" + value


class _Bundle:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.entity = spec.get("entity", "synthetic-owner")
        self.rows: dict[str, list[dict]] = {table.name: [] for table in catalog()}
        self.aliases: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.payloads: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.lot_roots: dict[str, str] = {}
        self.step_events: dict[str, str] = {}
        self.sequence = 0

    def add(self, table: str, **fields: object) -> dict:
        if table == "lots":
            lot_id = fields["lot_id"]
            origin_key = fields["origin_key"]
            if (
                not isinstance(lot_id, str)
                or not lot_id.startswith("@")
                or not isinstance(origin_key, str)
            ):
                message = "Lot identity must be a reference"
                raise ValueError(message)
            if fields.get("origin_posting_id"):
                origin_id = fields["origin_posting_id"]
                if not isinstance(origin_id, str) or not origin_id.startswith("@"):
                    message = "Lot origin must be a posting reference"
                    raise ValueError(message)
                root = child_id(
                    RecordId(
                        EntityId(self.entity),
                        RecordKind.LEG,
                        self.aliases[origin_id[1:]],
                    ),
                    RecordKind.LOT,
                    SemanticKey(origin_key),
                ).value
                self.aliases[lot_id[1:]] = root
            else:
                parent = fields.get("parent_lot_id")
                step = fields.get("creation_step_id")
                if (
                    not isinstance(parent, str)
                    or not parent.startswith("@")
                    or not isinstance(step, str)
                    or not step.startswith("@")
                ):
                    message = "Successor lot requires retained parent and step"
                    raise ValueError(message)
                root = self.lot_roots[parent[1:]]
                self.aliases[lot_id[1:]] = successor_lot_id(
                    RecordId(EntityId(self.entity), RecordKind.LOT, root),
                    RecordId(
                        EntityId(self.entity),
                        RecordKind.EVENT,
                        self.step_events[step[1:]],
                    ),
                    SemanticKey(origin_key),
                ).value
            self.lot_roots[lot_id[1:]] = root
        if table == "inventory_pools":
            pool_id = fields["pool_id"]
            declaration = fields["declaration_id"]
            if (
                not isinstance(pool_id, str)
                or not pool_id.startswith("@")
                or not isinstance(declaration, str)
                or not declaration.startswith("@declaration:")
            ):
                message = "Pool identity requires a stable declaration key"
                raise ValueError(message)
            self.aliases[pool_id[1:]] = declaration_id(
                EntityId(self.entity),
                RecordKind.POOL,
                SemanticKey(declaration.removeprefix("@declaration:")),
            ).value
        row = {"entity_id": self.entity, **fields}
        self.rows[table].append(row)
        return row

    def declaration(self, key: str, kind: str, **payload: object) -> str:
        alias = "declaration:" + key
        self.payloads[alias] = {"schema_version": 1, "kind": kind, **payload}
        self.add(
            "declarations",
            declaration_id=_ref(alias),
            declaration_key=key,
            declaration_kind=kind,
            revision_digest="revision:" + alias,
            document_path="fixtures/" + self.spec["case_id"] + ".json",
            document_locator="json:declarations/" + key,
            payload_json="payload:" + alias,
        )
        return _ref(alias)

    def dimensions(self) -> None:
        for commodity in self.spec["commodities"]:
            self.add(
                "commodities",
                commodity_id=_ref(commodity["id"]),
                name=commodity.get("name", commodity["id"]),
                commodity_kind=commodity["kind"],
                **commodity.get("fields", {}),
            )
        for account in self.spec["accounts"]:
            if not isinstance(account, dict) or not isinstance(account.get("id"), str):
                message = "Account fixture requires an object with an ID"
                raise TypeError(message)
            if account.keys() - {
                "id",
                "fields",
                "expected_periods",
                "publication_lag_days",
                "period_rule",
            }:
                message = "Unknown account fixture field"
                raise ValueError(message)
            account_id = account["id"]
            fields = {
                "account_id": _ref(account_id),
                "institution": "Invented Fixture Institution",
                "opened_date": None,
                "closed_date": None,
                "operating_commodity_id": None,
                "statements_expected": True,
                "statement_cadence": "monthly",
                "civil_zone": "Etc/UTC",
                "coverage_start_date": "2026-01-01",
            }
            overrides = account.get("fields", {})
            if not isinstance(overrides, dict) or overrides.keys() - (
                fields.keys() - {"account_id"}
            ):
                message = "Unknown account row override"
                raise ValueError(message)
            fields.update(overrides)
            declaration = self.declaration(
                account_id,
                "account",
                **fields,
                bindings=[],
                publication_lag_days=account.get("publication_lag_days", 7),
                period_rule=account.get("period_rule", "calendar"),
                expected_periods=account.get("expected_periods", []),
                coverage_basis_ids=["settled-units"],
            )
            self.add("accounts", **fields, declaration_id=declaration)
        for category, kind in self.spec["categories"].items():
            fields = {
                "category_id": _ref(category),
                "name": category,
                "category_type": kind,
                "parent_category_id": None,
            }
            declaration = self.declaration(category, "category", **fields)
            self.add("categories", **fields, declaration_id=declaration)
        for name in self.spec.get("counterparties", []):
            declaration = self.declaration(
                name, "counterparty", counterparty_id=_ref(name), name=name
            )
            self.add(
                "counterparties",
                counterparty_id=_ref(name),
                name=name,
                declaration_id=declaration,
            )
        for policy in self.spec["policies"]:
            self.declaration(policy["id"], policy["kind"], **policy["payload"])
        for position in self.spec["positions"]:
            self.aliases[position["id"]] = declaration_id(
                EntityId(self.entity),
                RecordKind.POSITION,
                SemanticKey("position:" + position["id"]),
            ).value
            fields = {
                "position_id": _ref(position["id"]),
                "account_id": _ref(position["account"]),
                "commodity_id": _ref(position["commodity"]),
                "position_kind": position["kind"],
                "counterparty_id": (
                    _ref(position["counterparty"])
                    if position.get("counterparty")
                    else None
                ),
                "measurement_kind": position["measurement"],
                "inventory_method": position["inventory"],
                "purpose": position.get("purpose", "general"),
                "terms_declaration_id": position.get("terms"),
            }
            declaration = self.declaration(
                "position:" + position["id"],
                "position",
                **fields,
                bindings=[],
                booking_declaration_id=position.get("booking"),
            )
            self.add("positions", **fields, declaration_id=declaration)
            self.positions[position["id"]] = fields

    def event(self, event: dict) -> None:
        alias = event["id"]
        source_alias = "source:" + alias
        source_bytes = (event["evidence"] + "\n").encode()
        digest = sha256(source_bytes).hexdigest()
        self.sources[digest] = source_bytes.decode()
        anchor = AcquiredAnchor(
            digest,
            SemanticKey("synthetic-fixture"),
            SemanticKey("events"),
            SemanticKey("json:" + alias),
        )
        identity = event_id(
            EntityId(self.entity), Occurrence(anchor, SemanticKey(alias))
        )
        self.aliases[alias] = identity.value
        self.aliases["step:" + alias] = child_id(
            identity, RecordKind.STEP, SemanticKey(event["operation"])
        ).value
        self.step_events["step:" + alias] = identity.value
        self.add(
            "source_records",
            source_record_id=_ref(source_alias),
            source_blob_digest=digest,
            source_scope_id="synthetic-fixture",
            source_section="events",
            record_locator="json:" + alias,
            input_kind="brokerage_statement",
            statement_id=event.get("statement"),
        )
        self.add(
            "economic_identities",
            record_kind="transaction",
            record_id=_ref(alias),
            anchor_kind="source_record",
            anchor_id=_ref(source_alias),
            event_key=alias,
            identity_state=event.get("identity_state", "active"),
            decision_declaration_id=event.get("identity_decision"),
        )
        recognized = event.get("state", "recognized") == "recognized"
        self.add(
            "transactions",
            txn_id=_ref(alias),
            event_kind=event.get("kind", "transaction"),
            event_state=event.get("state", "recognized"),
            review_state=event.get("review", "reviewed"),
            description_as_stated=event["evidence"],
            date_posted=event["date"],
            **event.get("dates", {}),
        )
        if recognized:
            self.add(
                "book_steps",
                step_id=_ref("step:" + alias),
                txn_id=_ref(alias),
                step_key=event["operation"],
                sequence=self.sequence,
                operation_kind=event["operation"],
                effective_date=event["date"],
            )
            self.sequence += 1
        for index, leg in enumerate(event["legs"]):
            self.leg(event, leg, index, recognized=recognized)
            self.aliases[leg["id"]] = child_id(
                identity, RecordKind.LEG, SemanticKey(leg["id"])
            ).value
        for allocation in event.get("allocations", []):
            self.allocation(event, allocation)
        for table, rows in event.get("rows", {}).items():
            for row in rows:
                self.add(table, **row)
        self._provenance(source_alias)

    def leg(self, event: dict, leg: dict, index: int, *, recognized: bool) -> None:
        position = self.positions.get(leg.get("position"))
        self.add(
            "postings",
            posting_id=_ref(leg["id"]),
            posting_index=index,
            txn_id=_ref(event["id"]),
            commodity_id=_ref(leg["commodity"]),
            amount=exact(leg["amount"], leg.get("source_scale")),
            account_id=position["account_id"] if position else None,
            position_id=position["position_id"] if position else None,
            category_id=_ref(leg["category"]) if leg.get("category") else None,
            leg_kind="position" if position else "boundary",
            posting_role=leg["role"],
            step_id=_ref("step:" + event["id"]) if recognized else None,
            **{
                **{f"date_{role}_mode": "inherit" for role in DATE_ROLES},
                **leg.get("fields", {}),
            },
        )
        for weight in leg.get("weights", []):
            self.add(
                "posting_weights",
                posting_id=_ref(leg["id"]),
                component_key=weight["kind"],
                weight_kind=weight["kind"],
                commodity_id=_ref(weight["commodity"]),
                amount=exact(weight["amount"]),
            )

    def allocation(self, event: dict, allocation: dict) -> None:
        self.add(
            "inventory_allocations",
            allocation_id=_ref(allocation["id"]),
            step_id=_ref("step:" + event["id"]),
            allocation_kind=allocation["kind"],
            method_declaration_id=allocation["method"],
            action_id=allocation.get("action"),
        )
        for change in allocation["changes"]:
            self.add(
                "inventory_changes",
                change_id=_ref(change["id"]),
                allocation_id=_ref(allocation["id"]),
                change_role=change["role"],
                posting_id=_ref(change["posting"]),
                position_id=_ref(change["position"]),
                lot_id=_ref(change["lot"]) if change.get("lot") else None,
                pool_id=_ref(change["pool"]) if change.get("pool") else None,
                units_delta=exact(change["units"]),
                principal_delta=exact(change["principal"]),
                capitalized_fee_delta=exact(change["capitalized_fee"]),
                expensed_fee_delta=exact(change["expensed_fee"]),
            )

    def _provenance(self, source_alias: str) -> None:
        determinations = {
            "posting_weights",
            "book_steps",
            "inventory_allocations",
            "inventory_changes",
            "inventory_fee_changes",
            "component_conversions",
            "disposals",
            "reference_changes",
            "reference_settlements",
            "action_applications",
            "fee_allocations",
        }
        for table in tabular_schema()["tables"]:
            if table["name"] in {"provenance", "build", "declarations"}:
                continue
            for row in self.rows[table["name"]]:
                if row.get("_provenanced"):
                    continue
                key = [row[name] for name in table["primary_key"][1:]]
                declaration = row.get("declaration_id")
                derived = table["name"] in determinations
                for name in row.keys() - set(table["primary_key"]):
                    self.add(
                        "provenance",
                        provenance_id=_ref(
                            "provenance:" + table["name"] + _json(key) + name
                        ),
                        record_kind=table["name"],
                        record_id=key,
                        field_name=name,
                        value_origin=(
                            "declaration"
                            if declaration
                            else "booking"
                            if derived
                            else "extracted"
                        ),
                        evidence_kind="declaration" if declaration else "source_record",
                        evidence_id=declaration or _ref(source_alias),
                        derivation_id=(
                            "fixture-supplied-determinations-v1" if derived else None
                        ),
                    )
                row["_provenanced"] = True

    def resolve(self, value: object) -> object:
        if isinstance(value, str) and value.startswith("blob:"):
            text = self.spec["source_texts"][value.removeprefix("blob:")]
            digest = sha256((text + "\n").encode()).hexdigest()
            self.sources[digest] = text + "\n"
            return digest
        if isinstance(value, str) and value.startswith("@"):
            alias = value[1:]
            if alias in self.payloads and alias not in self.aliases:
                payload = self.resolve(self.payloads[alias])
                if not isinstance(payload, dict):
                    message = "Declaration payload must be an object"
                    raise TypeError(message)
                self.aliases[alias] = revision_id(
                    EntityId(self.entity),
                    SemanticKey(alias.removeprefix("declaration:")),
                    effective_from=None,
                    effective_to=None,
                    payload=payload,
                ).value
            return self.aliases.setdefault(
                alias, sha256((self.entity + ":" + alias).encode()).hexdigest()
            )
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            return self._resolve_mapping(value)
        return value

    def _resolve_mapping(self, value: dict) -> dict:
        result = {key: self.resolve(item) for key, item in value.items()}
        for key, item in result.items():
            if isinstance(item, list) and (
                key.endswith("_ids") or key in {"tags", "links", "authored_keys"}
            ):
                if not all(isinstance(part, str) for part in item):
                    message = "Fixture identifiers must be text"
                    raise ValueError(message)
                result[key] = sorted(part for part in item if isinstance(part, str))
            if key == "members" and isinstance(item, list) and item:
                if not all(isinstance(member, dict) for member in item):
                    message = "Fixture members must be objects"
                    raise ValueError(message)
                members = [member for member in item if isinstance(member, dict)]
                if "source_record_id" in members[0]:
                    result[key] = sorted(
                        members,
                        key=lambda member: (
                            member["source_record_id"],
                            member["event_key"],
                        ),
                    )
        return result

    def _expand_numerics(self) -> None:
        for table in tabular_schema()["tables"]:
            for row in self.rows[table["name"]]:
                row.pop("_provenanced", None)
                for group in table["numeric_groups"]:
                    value = row.pop(group["name"], None)
                    if value is not None and not isinstance(value, dict):
                        message = "Numeric fixture group must be an object"
                        raise ValueError(message)
                    for suffix in ("coefficient", "scale", "source_scale"):
                        row[group["name"] + "_" + suffix] = (
                            value[suffix] if value is not None else None
                        )

    def _complete_row(self, table: Table, logical: dict) -> dict:
        row = self.resolve(logical)
        if not isinstance(row, dict):
            message = "Resolved fixture row must be an object"
            raise TypeError(message)
        if table.name == "declarations":
            alias = logical["payload_json"].removeprefix("payload:")
            row["payload_json"] = _json(self.resolve(self.payloads[alias]))
            row["revision_digest"] = self.aliases[alias]
        if table.name in {"provenance", "observations"}:
            row["record_id"] = _json(row["record_id"])
        for column in table.columns:
            if column.name not in row:
                if column.nullable:
                    row[column.name] = None
                elif column.kind == "list":
                    row[column.name] = []
                else:
                    message = f"Missing {table.name}.{column.name}"
                    raise ValueError(message)
            if column.kind == "list":
                row[column.name] = sorted(row[column.name])
        return row

    def finish(self) -> FixtureCase:
        self.add(
            "build",
            manifest_digest=sha256(
                ("fixture:" + self.spec["case_id"]).encode()
            ).hexdigest(),
            producer_version="conformance-specimen",
            schema_version=1,
            schema_digest=sha256(_json(tabular_schema()).encode()).hexdigest(),
            as_of="2026-12-31",
            execution_fingerprint=sha256(b"fixture-expansion-v1").hexdigest(),
            is_dirty=False,
        )
        for table, rows in self.spec.get("rows", {}).items():
            for row in rows:
                self.add(table, **row)
        if self.spec["events"]:
            self._provenance("source:" + self.spec["events"][-1]["id"])
        self._expand_numerics()
        for alias in self.payloads:
            self.resolve(_ref(alias))
        physical: dict[str, list[dict]] = {}
        for table in catalog():
            complete = [self._complete_row(table, row) for row in self.rows[table.name]]
            physical[table.name] = sorted(
                complete,
                key=lambda row: tuple(
                    (row[key] is not None, row[key]) for key in table.sort_key
                ),
            )
        return FixtureCase(
            self.spec["case_id"],
            physical,
            self.spec["expected_answers"],
            {
                "blobs": self.sources,
                "bindings": [{"source_scope_id": "synthetic-fixture"}],
                "consumer": self.spec.get("consumer", {}),
            },
            tuple(self.spec["oracle_notes"]),
            tuple(self.spec["events"]),
            self.aliases,
            tuple(self.spec.get("negative_cases", [])),
        )


def _read_spec(filename: str) -> dict:
    spec = json.loads((FIXTURES / filename).read_text())
    if "extends" in spec:
        base = _read_spec(spec.pop("extends"))
        for key, value in spec.items():
            if isinstance(value, list) and key in base:
                base[key] = [*base[key], *value]
            elif isinstance(value, dict) and key in base:
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        spec = base
    for replacement in spec.pop("replacements", []):
        target = spec
        for key in replacement["path"][:-1]:
            target = target[key]
        last = replacement["path"][-1]
        if isinstance(target, list) and last == len(target):
            target.append(replacement["value"])
        else:
            target[last] = replacement["value"]
    return spec


def _literals(value: object) -> object:
    if isinstance(value, dict):
        if set(value) == {"n"}:
            return exact(value["n"])
        return {key: _literals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_literals(item) for item in value]
    return value


def load_case(case_id: str) -> FixtureCase:
    spec = _literals(_read_spec(case_id + ".json"))
    if not isinstance(spec, dict):
        message = "Fixture must be an object"
        raise TypeError(message)
    if spec["case_id"] != case_id:
        message = "Fixture filename and identity disagree"
        raise ValueError(message)
    bundle = _Bundle(spec)
    bundle.dimensions()
    events = spec["events"]
    if "event_order" in spec:
        ordered = spec["event_order"]
        if len(ordered) != len(events) or set(ordered) != {
            event["id"] for event in events
        }:
            message = "Event order must name every retained occurrence exactly once"
            raise ValueError(message)
        events = sorted(events, key=lambda event: ordered.index(event["id"]))
    for event in events:
        bundle.event(event)
    return bundle.finish()


def fixture_cases() -> tuple[FixtureCase, ...]:
    index = json.loads((FIXTURES / "index.json").read_text())
    return tuple(load_case(case_id) for case_id in index["cases"])


def coverage_index() -> tuple[dict, ...]:
    return tuple(json.loads((FIXTURES / "index.json").read_text())["coverage"])
