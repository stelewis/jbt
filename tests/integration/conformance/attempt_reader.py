"""Independent retained native-model review and applicability consumer."""

import hashlib
import json
import re
import unicodedata
from itertools import product
from typing import TYPE_CHECKING, Any

from tests.integration.conformance.consumer_arithmetic import read_number
from tests.integration.conformance.decision_bodies import read_decision_body
from tests.integration.conformance.journal_reader import read_entry, read_journal
from tests.integration.conformance.reader import ReaderError, canonical_bytes

if TYPE_CHECKING:
    from pathlib import Path

EVENT_FIELDS = {
    "accounts",
    "dates",
    "effective_date",
    "effective_role",
    "event_kind",
    "event_state",
    "source_identity",
    "legs",
}
LEG_FIELDS = {
    "accounts",
    "dates",
    "effective_date",
    "effective_role",
    "event_kind",
    "event_state",
    "source_identity",
    "parent",
    "leg",
}


def _require(valid: bool, message: str) -> None:  # noqa: FBT001
    if not valid:
        raise ReaderError(message)


def _identity_value(value: str | tuple) -> bytes:
    tag, payload = (
        (b"s", value.encode("utf-8"))
        if isinstance(value, str)
        else (b"l", b"".join(_identity_value(item) for item in value))
    )
    return tag + str(len(payload)).encode("ascii") + b":" + payload


def _identity_digest(domain: str, fields: tuple) -> str:
    payload = _identity_value(domain) + b"i1:1" + _identity_value(fields)
    return hashlib.sha256(
        b"JBT\x00l" + str(len(payload)).encode("ascii") + b":" + payload
    ).hexdigest()


def _event_id(entity: str, occurrence: dict) -> str:
    anchor = occurrence["anchor"]
    kind = anchor["kind"]
    names = {
        "acquired": ("blob_digest", "namespace", "section", "locator"),
        "authored": ("record_key",),
        "source_key": ("namespace", "section", "key_domain", "source_key"),
    }[kind]
    for name, value in (
        *((name, anchor[name]) for name in names),
        ("event_key", occurrence["event_key"]),
    ):
        _require(type(value) is str, "identity text required")
        if name == "blob_digest":
            _require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, "SHA256 ID")
        else:
            _require(
                bool(value)
                and unicodedata.normalize("NFC", value) == value
                and not any(unicodedata.category(char) == "Cc" for char in value),
                "invalid NFC identifier",
            )
    if kind == "acquired":
        qualifier, separator, position = anchor["locator"].partition(":")
        _require(bool(qualifier and separator and position), "qualified source locator")
    return _identity_digest(
        "economic-identity",
        (
            ("entity", entity),
            ("kind", "event"),
            ("anchor", (kind, *(anchor[name] for name in names))),
            ("event_key", occurrence["event_key"]),
        ),
    )


def _leg_id(entity: str, parent: str, key: str) -> str:
    return _identity_digest(
        "semantic-child",
        (
            ("entity", entity),
            ("parent_kind", "event"),
            ("parent", parent),
            ("kind", "leg"),
            ("key", key),
        ),
    )


def _retained(root: Path, digest: str) -> bytes:
    payload = (root / "inputs" / digest).read_bytes()
    _require(hashlib.sha256(payload).hexdigest() == digest, "retained seal")
    return payload


def _document(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload)
    _require(
        type(value) is dict
        and canonical_bytes(value, integer_strings=False) == payload,
        "canonical review document",
    )
    return value


def _review_report(root: Path, attempt: str, digest: str) -> dict:
    report = _document(_retained(root, digest))
    _require(
        set(report)
        == {
            "schema_version",
            "attempt_digest",
            "selection",
            "model",
            "checks",
            "failure",
        }
        and type(report["schema_version"]) is int
        and report["schema_version"] == 1,
        "closed review report",
    )
    witness = _document(_retained(root, attempt))
    _require(
        report["attempt_digest"] == attempt
        and report["selection"]["input_digest"] == witness["input_digest"],
        "reviewed report attempt binding",
    )
    if report["model"] is not None:
        model = report["model"]
        _require(
            set(model)
            == {
                "schema_version",
                "tables",
                "findings",
                "determinations",
                "derivations",
                "review",
            },
            "closed native report model",
        )
        review = model["review"]
        _require(
            set(review) == {"tail", "receipts", "rules", "counts", "proposals"},
            "closed review evidence",
        )
        for target in review["tail"]:
            _fields(target)
        for receipt in review["receipts"]:
            for target in receipt["candidates"]:
                _fields(target)
    return report


def _occurrence(value: str) -> dict:
    raw = json.loads(value)
    _require(set(raw) == {"anchor", "event_key"}, "native occurrence closure")
    anchor = raw["anchor"]
    names = set(anchor)
    if names == {"record_key"}:
        kind = "authored"
    elif names == {"namespace", "section", "key_domain", "source_key"}:
        kind = "source_key"
    else:
        _require(
            names == {"namespace", "section", "blob_digest", "locator"},
            "native anchor closure",
        )
        kind = "acquired"
    decoded = {"kind": kind}
    for name, item in anchor.items():
        if name == "blob_digest":
            decoded[name] = item
        else:
            _require(set(item) == {"value"}, "typed anchor key")
            decoded[name] = item["value"]
    return {"anchor": decoded, "event_key": raw["event_key"]["value"]}


def _leg(value: str | dict, *, native: bool = False) -> dict:
    leg = json.loads(value) if isinstance(value, str) else value
    names = {
        "key",
        "kind",
        "account",
        "position",
        "commodity",
        "amount",
        "dates",
        "source_leg",
        "transfer",
        "original_commodity",
        "fx_rate",
        "fx_base_commodity",
        "fx_quote_commodity",
    }
    _require(
        set(leg) == names | ({"balancing"} if native else set()),
        "economic leg closure, without rule output",
    )
    _require(leg["kind"] in {"position", "boundary"}, "leg kind")
    for name in (
        "source_leg",
        "transfer",
        "original_commodity",
        "fx_base_commodity",
        "fx_quote_commodity",
    ):
        value = leg[name]
        _require(
            value is None
            or (
                type(value) is str
                and bool(value)
                and unicodedata.normalize("NFC", value) == value
                and not any(unicodedata.category(char) == "Cc" for char in value)
            ),
            "nullable semantic leg ownership",
        )
    if native:
        _require(type(leg["balancing"]) is list, "paired balancing legs")
        balancing = [_leg(item) for item in leg["balancing"]]
        _require(
            all(item["source_leg"] == leg["key"] for item in balancing)
            and len({item["key"] for item in balancing}) == len(balancing),
            "source leg balancing ownership",
        )
    amount = leg["amount"]
    _require(set(amount) == {"coefficient", "scale"}, "amount closure")
    _require(
        read_number(amount["coefficient"], amount["scale"], None) is not None,
        "signed quantity",
    )
    if leg["fx_rate"] is not None:
        rate = leg["fx_rate"]
        _require(set(rate) == {"coefficient", "scale"}, "stated rate closure")
        _require(
            read_number(rate["coefficient"], rate["scale"], None) is not None,
            "stated exchange rate",
        )
    _require(len(leg["dates"]) == 5, "date-role closure")
    for choice in leg["dates"]:
        _require(set(choice) == {"date", "mode"}, "date mode closure")
        _require(choice["mode"] in {"value", "inherit", "unknown"}, "date mode")
        _require(
            (choice["mode"] == "value") == (choice["date"] is not None), "explicit date"
        )
    return leg


def _compound_fields(target: dict) -> dict:
    fields = target["fields"]
    _require(
        len(fields) == 1
        and fields[0]["name"] == "members"
        and fields[0]["kind"] == "string"
        and fields[0]["nullable"] is False,
        "compound profile",
    )
    members = json.loads(fields[0]["value"])
    _require(bool(members), "compound members")
    addresses = []
    for member in members:
        _require(set(member) == {"table", "key", "fields"}, "compound member shape")
        _require(
            member["table"]
            in {
                "review_sources",
                "review_statements",
                "review_events",
                "review_legs",
                "review_documents",
            },
            "compound operand kind",
        )
        _fields({"entity_id": target["entity_id"], "active": True, **member})
        addresses.append([member["table"], member["key"]])
    _require(addresses == sorted(addresses), "sorted compound operands")
    _require(
        len({(table, tuple(key)) for table, key in addresses}) == len(addresses),
        "unique compound operands",
    )
    seal = hashlib.sha256(canonical_bytes(addresses, integer_strings=False)).hexdigest()
    _require(target["key"] == [seal], "compound address")
    return {"members": fields[0]["value"]}


def _document_fields(target: dict) -> dict:
    fields = target["fields"]
    _require(
        len(target["key"]) == 1
        and type(target["key"][0]) is str
        and re.fullmatch(r"[0-9a-f]{64}", target["key"][0]) is not None
        and target["active"] is True,
        "active archive blob address",
    )
    _require(
        fields
        == [
            {
                "name": "archive_only",
                "kind": "boolean",
                "nullable": False,
                "value": True,
            },
            {
                "name": "blob_digest",
                "kind": "string",
                "nullable": False,
                "value": target["key"][0],
            },
        ],
        "archive-only document profile",
    )
    return {field["name"]: field["value"] for field in fields}


def _fields(target: dict) -> dict:
    _require(
        set(target) == {"entity_id", "table", "key", "active", "fields"},
        "native target shape",
    )
    profiles = {"review_merge": _compound_fields, "review_documents": _document_fields}
    if target["table"] in profiles:
        return profiles[target["table"]](target)
    if target["table"] in {"review_sources", "review_statements"}:
        fields = target["fields"]
        _require(
            [field["name"] for field in fields] == ["facts", "membership"]
            and all(
                set(field) == {"kind", "name", "nullable", "value"}
                and field["kind"] == "string"
                and field["nullable"] is False
                for field in fields
            ),
            "native merge profile",
        )
        values = {field["name"]: json.loads(field["value"]) for field in fields}
        _require(
            len(target["key"]) == 1
            and re.fullmatch(r"[0-9a-f]{64}", target["key"][0]) is not None,
            "merge address",
        )
        facts, membership = values["facts"], values["membership"]
        if target["table"] == "review_sources":
            _require(
                set(facts)
                == {
                    "source_record",
                    "source_anchor",
                    "source_identity",
                    "state",
                    "effective_date",
                    "effective_role",
                    "dates",
                    "legs",
                }
                and set(membership) == {"canonical", "identity", "disposition"},
                "source reviewed-fact closure",
            )
            _require(facts["source_record"] == target["key"][0], "source guard address")
            canonical = _occurrence(
                canonical_bytes(membership["canonical"], integer_strings=False).decode()
            )
            _require(
                _event_id(target["entity_id"], canonical) == membership["identity"],
                "canonical membership identity",
            )
            anchor = _occurrence(
                canonical_bytes(
                    {
                        "anchor": facts["source_anchor"],
                        "event_key": facts["source_identity"]["event_key"],
                    },
                    integer_strings=False,
                ).decode()
            )["anchor"]
            source = _identity_digest(
                "source-record",
                tuple(
                    (name, anchor[name])
                    for name in ("blob_digest", "namespace", "section", "locator")
                ),
            )
            _require(source == facts["source_record"], "source occurrence anchor")
            _require(
                facts["state"] in {"pending", "recognized", "cancelled", "retracted"}
                and facts["effective_role"] in {"posted", "authorized", "initiated"}
                and membership["disposition"] in {"live", "eclipsed", "replaced"}
                and len(facts["dates"]) == 5,
                "source participation and date roles",
            )
            for leg in facts["legs"]:
                _leg(leg)
        else:
            _require(
                set(facts)
                == {"statement", "scope", "section", "blob_digest", "account", "parts"}
                and facts["statement"] == target["key"][0],
                "statement reviewed-fact closure",
            )
            for part in facts["parts"]:
                _require(
                    set(part)
                    == {
                        "key",
                        "fact_kind",
                        "basis",
                        "date_role",
                        "period",
                        "complete",
                        "point",
                        "source_records",
                        "movements",
                        "balances",
                    }
                    and part["fact_kind"] in {"movement", "balance"}
                    and type(part["complete"]) is bool,
                    "statement part closure",
                )
                for balance in part["balances"]:
                    _require(
                        set(balance)
                        == {
                            "source_record",
                            "key",
                            "account",
                            "position",
                            "commodity",
                            "amount",
                        }
                        and balance["source_record"] in part["source_records"]
                        and balance["account"] == facts["account"],
                        "statement balance evidence",
                    )
                    _require(
                        set(balance["amount"]) == {"coefficient", "scale"},
                        "canonical balance quantity",
                    )
            for replacement in membership:
                _require(
                    set(replacement)
                    == {
                        "replaced",
                        "replacement",
                        "account",
                        "fact_kind",
                        "basis",
                        "date_role",
                        "period",
                    },
                    "scoped statement disposition",
                )
        return values
    _require(target["table"] in {"review_events", "review_legs"}, "native table")
    expected = EVENT_FIELDS if target["table"] == "review_events" else LEG_FIELDS
    fields = target["fields"]
    _require(
        [field["name"] for field in fields] == sorted(expected),
        "contract-owned profile",
    )
    for field in fields:
        _require(set(field) == {"kind", "name", "nullable", "value"}, "typed profile")
        _require(
            field["kind"]
            == ("list" if field["name"] in {"accounts", "tags"} else "string"),
            "profile schema",
        )
        _require(
            field["nullable"] == (field["name"] in {"payee", "narration"}),
            "profile nullability",
        )
    result = {field["name"]: field["value"] for field in fields}
    _require(
        result["effective_role"]
        in {
            "authorized",
            "traded",
            "posted",
            "settled",
            "value",
            "initiated",
            "occurred",
        },
        "event effective role",
    )
    _require(len(json.loads(result["dates"])) == 5, "event date-role closure")
    event = _event_id(target["entity_id"], _occurrence(result["source_identity"]))
    if target["table"] == "review_events":
        _require(target["key"] == [event], "economic event identity")
        legs = [_leg(value) for value in json.loads(result["legs"])]
        _require(
            result["accounts"]
            == sorted({leg["account"] for leg in legs if leg["account"] is not None}),
            "reviewed account closure",
        )
    else:
        _require(result["parent"] == event, "leg parent closure")
        leg = _leg(result["leg"], native=True)
        _require(
            target["key"] == [_leg_id(target["entity_id"], event, leg["key"])],
            "stable semantic leg identity",
        )
    return result


def _projection(target: dict) -> str:
    _fields(target)
    fields = target["fields"]
    digest = hashlib.sha256(
        canonical_bytes(
            {
                "schema_version": 1,
                "columns": [
                    {key: field[key] for key in ("name", "kind", "nullable")}
                    for field in fields
                ],
            }
        )
    )
    digest.update(canonical_bytes([field["value"] for field in fields]))
    return digest.hexdigest()


def _address(target: dict) -> tuple:
    return target["entity_id"], target["table"], tuple(target["key"])


def _candidates(expected: dict, inventory: dict[tuple, list[dict]]) -> list[dict]:
    if expected["table"] != "review_merge":
        return inventory.get(_address(expected), [])
    members = json.loads(expected["fields"][0]["value"])
    groups = []
    for member in members:
        candidates = inventory.get(
            (expected["entity_id"], member["table"], tuple(member["key"])), []
        )
        if not candidates:
            return []
        groups.append(candidates)
    return [
        {
            **expected,
            "active": all(target["active"] for target in selected),
            "fields": [
                {
                    "kind": "string",
                    "name": "members",
                    "nullable": False,
                    "value": canonical_bytes(
                        [
                            {key: target[key] for key in ("table", "key", "fields")}
                            for target in selected
                        ],
                        integer_strings=False,
                    )
                    .decode()
                    .strip(),
                }
            ],
        }
        for selected in product(*groups)
    ]


def _reviewed_targets(boundary: list[dict], target: dict) -> list[dict]:
    inventory: dict[tuple, list[dict]] = {}
    for item in boundary:
        inventory.setdefault(_address(item), []).append(item)
    return _candidates(target, inventory)


def _body_operands(body: dict, target: dict) -> None:
    if body["kind"] not in {"identity", "statement_revision", "transfer", "link"}:
        return
    _require(target["table"] == "review_merge", "structural target")
    if body["kind"] == "identity":
        expected = [["review_sources", [value]] for value in body["members"]]
    elif body["kind"] == "statement_revision":
        expected = [
            ["review_statements", [value]]
            for value in (*body["replaced"], *body["replacement"])
        ]
    elif body["kind"] == "transfer":
        expected = [
            ["review_legs", [value]] for value in (*body["outgoing"], *body["incoming"])
        ]
    elif body["link_kind"] == "document":
        reference = body["target"]
        table, name = (
            ("review_events", "event_id")
            if "event_id" in reference
            else ("review_legs", "leg_id")
        )
        expected = [
            [table, [reference[name]]],
            *[["review_documents", [value]] for value in body["documents"]],
        ]
    else:
        expected = [
            ["review_events", [body[name]["event_id"]]] for name in ("attempt", "fee")
        ]
    operands = [
        [member["table"], member["key"]]
        for member in json.loads(target["fields"][0]["value"])
    ]
    _require(
        operands == sorted(expected),
        "sealed body operand binding",
    )


def _boundary(
    report: dict,
    key: str | None,
    *,
    kind: str | None = None,
) -> list[dict]:
    review = report["model"]["review"]
    stages = {
        "identity": 0,
        "statement_revision": 0,
        "assign": 1,
        "split": 1,
        "transfer": 2,
        "link": 2,
    }
    if key is None and kind is not None:
        declarations = {
            row["declaration_key"]: row["declaration_kind"]
            for row in report["model"]["tables"]["declarations"]
        }
        _require(
            all(
                declarations.get(receipt["decision_key"]) in stages
                for receipt in review["receipts"]
            ),
            "decision stage declaration binding",
        )
        key = next(
            (
                receipt["decision_key"]
                for receipt in review["receipts"]
                if stages[declarations[receipt["decision_key"]]] > stages[kind]
            ),
            None,
        )
    inventory: dict[tuple, list[dict]] = {}
    for target in review["tail"]:
        _fields(target)
        inventory.setdefault(_address(target), []).append(target)
    result = list(review["tail"])
    found = key is None
    keys = set()
    for receipt in reversed(review["receipts"]):
        _require(receipt["decision_key"] not in keys, "unique decision boundary")
        keys.add(receipt["decision_key"])
        addresses = set()
        for change in receipt["restore"]:
            _require(
                set(change) == {"entity_id", "table", "key", "candidates"},
                "closed reverse boundary change",
            )
            address = _address(change)
            restored = change["candidates"]
            for target in restored:
                _fields(target)
            _require(
                address not in addresses
                and all(_address(target) == address for target in restored)
                and restored != inventory.get(address, []),
                "unique target-specific boundary change",
            )
            addresses.add(address)
            if restored:
                inventory[address] = restored
            else:
                inventory.pop(address, None)
        _require(
            receipt["candidates"] == _candidates(receipt["expected"], inventory),
            "receipt matches reconstructed pre-entry boundary",
        )
        if receipt["decision_key"] == key:
            found = True
            result = [target for group in inventory.values() for target in group]
    _require(found, "original decision boundary")
    return result


def validate_receipt(receipt: dict) -> str:
    """Recompute the closed outcome from the retained pre-entry candidates."""
    expected = receipt["expected"]
    seal = _projection(expected)
    _require(seal == receipt["expected_digest"], "reviewed projection seal")
    candidates = receipt["candidates"]
    for candidate in candidates:
        _fields(candidate)
    matching = [
        candidate
        for candidate in candidates
        if _address(candidate) == _address(expected)
    ]
    _require(matching == candidates, "target-scoped pre-entry candidates")
    observed = None
    if not matching:
        outcome = "missing"
    elif len(matching) > 1:
        outcome = "ambiguous"
    else:
        observed = _projection(matching[0])
        outcome = (
            "inactive"
            if not matching[0]["active"]
            else "applied"
            if seal == observed
            else "changed"
        )
    _require(
        receipt["outcome"] == outcome and receipt["observed_digest"] == observed,
        "pre-entry guard receipt",
    )
    return outcome


def _body_chain(entries: list[bytes], bodies: dict[str, bytes]) -> None:
    variants: dict[tuple[str, str], tuple[str, str | None]] = {}
    for payload in entries:
        entry = read_entry(payload)
        if entry.payload_digest is None:
            continue
        wire = bodies[entry.payload_digest]
        body = read_decision_body(wire)
        _require(
            body.entity_id == entry.entity_id and body.kind == entry.kind,
            "decision header body binding",
        )
        document = _document(wire)
        variant = (body.kind, document.get("link_kind"))
        key = (entry.entity_id, entry.declaration_key)
        _require(
            key not in variants or variants[key] == variant,
            "same sealed body variant",
        )
        variants[key] = variant


def read_attempt(root: Path, attempt: str) -> dict:
    """Read only selected immutable evidence, never later live journal state."""
    witness = (root / "attempts" / f"{attempt}.selection").read_bytes()
    _require(hashlib.sha256(witness).hexdigest() == attempt, "attempt witness")
    invocation = _document(_retained(root, attempt))
    selected = _document(_retained(root, invocation["input_digest"]))
    reference = _document((root / "attempts" / f"{attempt}.report").read_bytes())
    report = _review_report(root, attempt, reference["report_digest"])
    _require(report["attempt_digest"] == attempt, "attempt lineage")
    _require(
        report["selection"]["input_digest"] == invocation["input_digest"],
        "input selection lineage",
    )
    if report["model"] is None:
        _require(report["failure"] is not None, "located failed attempt")
        return report
    journal = selected["journal"]
    head = canonical_bytes(journal["head"], integer_strings=False)
    entries = [_retained(root, digest) for digest in journal["headers"]]
    bodies = {
        entry.payload_digest: _retained(root, entry.payload_digest)
        for payload in entries
        if (entry := read_entry(payload)).payload_digest is not None
    }
    history = read_journal(entries, head, bodies=bodies)
    _body_chain(entries, bodies)
    references = journal["reports"]
    report_refs = {item["attempt_digest"]: item["report_digest"] for item in references}
    _require(
        references
        == [
            {"attempt_digest": key, "report_digest": value}
            for key, value in sorted(report_refs.items())
        ]
        and set(report_refs)
        == {read_entry(payload).reviewed_attempt_digest for payload in entries},
        "captured report role inventory",
    )
    reviewed_reports = {
        key: _review_report(root, key, value) for key, value in report_refs.items()
    }
    for payload in entries:
        entry = read_entry(payload)
        if entry.payload_digest is None:
            continue
        body = _document(bodies[entry.payload_digest])
        basis = _document(_retained(root, body["review"]["basis_digest"]))
        _body_operands(body, basis["target"])
        _require(
            basis["attempt_digest"] == entry.reviewed_attempt_digest
            and basis["report_digest"] == report_refs[entry.reviewed_attempt_digest],
            "reviewed report role binding",
        )
        expected = _reviewed_targets(
            _boundary(
                reviewed_reports[entry.reviewed_attempt_digest],
                basis["before_key"],
                kind=body["kind"],
            ),
            basis["target"],
        )
        _require(expected == [basis["target"]], "authenticated reviewed boundary")
    receipts = report["model"]["review"]["receipts"]
    _boundary(report, None)
    _require(
        [item["entry_digest"] for item in receipts]
        == [
            active.entry.digest
            for stage in (
                {"identity", "statement_revision"},
                {"assign", "split"},
                {"transfer", "link"},
            )
            for active in history.active
            if active.entry.kind in stage
        ],
        "first-record logical order",
    )
    ordered = [
        active
        for stage in (
            {"identity", "statement_revision"},
            {"assign", "split"},
            {"transfer", "link"},
        )
        for active in history.active
        if active.entry.kind in stage
    ]
    for receipt, active in zip(receipts, ordered, strict=True):
        entry = active.entry
        assert entry.payload_digest is not None
        body = _document(bodies[entry.payload_digest])
        basis = _document(_retained(root, body["review"]["basis_digest"]))
        _require(
            basis["attempt_digest"] == entry.reviewed_attempt_digest,
            "basis attempt lineage",
        )
        expected_report = reviewed_reports[entry.reviewed_attempt_digest]
        expected = _reviewed_targets(
            _boundary(expected_report, basis["before_key"], kind=body["kind"]),
            basis["target"],
        )
        _require(
            expected == [basis["target"]] and basis["target"] == receipt["expected"],
            "authenticated expected pre-entry evidence",
        )
        _require(
            receipt["decision_key"] == entry.declaration_key,
            "receipt decision key binding",
        )
        validate_receipt(receipt)
    return report
