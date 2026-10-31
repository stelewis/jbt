import ast
import json
from copy import deepcopy
from pathlib import Path

import pytest
from tests.integration.conformance.reader import check_assertions

from jbt.artifacts.integrity import byte_digest
from jbt.artifacts.projection import projection_key
from jbt.checks.quantity import CheckKind, CheckStatus
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.extraction import ExtractionContext
from jbt.extraction import extract as extract_wire
from jbt.importers.ofx import OfxImporter
from jbt.model.corrections import leg_facts
from jbt.model.quantity import FindingCode, Resolution, resolve
from jbt.runtime.build import _effective_configuration, _findings, _validate_model
from jbt.runtime.checks import projected_model_checks as model_checks
from jbt.runtime.checks import retained_source_checks
from jbt.runtime.outputs import ledger, summary
from jbt.runtime.project import Project, read_project, text
from jbt.runtime.resolution import ExtractInput, prepare, project_resolution

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("variant", "failure"),
    [
        ("stated", None),
        ("missing-symbol", FindingCode.SYMBOL),
        ("expired-symbol", FindingCode.SYMBOL_VALIDITY),
        ("conflicting-rate", FindingCode.KEY_CONFLICT),
        ("equivalent-rate", None),
    ],
)
def test_stated_foreign_rate_resolves_without_inventing_original_amount(
    variant: str, failure: FindingCode | None
) -> None:
    raw = source(
        bank=(),
        card=(("foreign", "20260112", -5),),
        bank_balance=100,
        card_balance=-5,
    ).replace(
        b"</STMTTRN>",
        b"<ORIGCURRENCY><CURRATE>1.2500</CURRATE><CURSYM>EUR</CURSYM>"
        b"</ORIGCURRENCY></STMTTRN>",
    )
    downloads = (
        (raw, raw.replace(b"1.2500", b"1.50"))
        if variant == "conflicting-rate"
        else (raw, raw.replace(b"1.2500", b"1.25"))
        if variant == "equivalent-rate"
        else (raw,)
    )
    value, extracts = history(downloads)
    if variant != "missing-symbol":
        euro = deepcopy(next(d for d in value["declarations"] if d["key"] == "usd"))
        euro["key"] = "eur"
        euro["payload"].update(commodity_id="EUR", name="EUR")
        euro["payload"]["symbols"][0].update(
            symbol_id="eur-symbol",
            symbol="EUR",
            valid_to="2026-01-12" if variant == "expired-symbol" else None,
        )
        value["declarations"].append(euro)
    value["registries"]["observation_fields"].extend(
        {
            "field_name": f"source.origcurrency.{field}",
            "value_type": "text",
            "units": "none",
            "payload_schema": None,
        }
        for field in ("currate", "cursym")
    )
    project, _, resolution, tables = run(value, extracts)
    blocking = {finding.code for finding in resolution.findings if finding.blocking}
    if failure is not None:
        assert failure in blocking
        return
    assert not blocking
    _validate_model(project, tables, "0" * 64)
    posting = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "card-cash" and row["amount_coefficient"] == "-5"
    )
    assert posting["original_amount_coefficient"] is None
    assert posting["original_commodity_id"] == "EUR"
    assert posting["fx_rate_as_stated_coefficient"] == "125"
    assert posting["fx_rate_as_stated_scale"] == 2
    assert posting["fx_base_commodity_id"] == "EUR"
    assert posting["fx_quote_commodity_id"] == "USD"
    event = next(event for event in resolution.events if event.declaration is None)
    facts = leg_facts(event.legs[0])
    assert facts["fx_rate"] == {"coefficient": "125", "scale": 2}
    assert facts["original_commodity"] == facts["fx_base_commodity"] == "EUR"
    assert facts["fx_quote_commodity"] == "USD"
    evidence = next(
        row
        for row in tables["provenance"]
        if row["record_kind"] == "postings"
        and row["record_id"]
        == json.dumps([posting["posting_id"]], separators=(",", ":"))
        and row["field_name"] == "fx_rate_as_stated"
    )
    selected = next(
        s.source_record.value for s in event.support if s.kind == "selected"
    )
    assert evidence["evidence_id"] == selected
    posting.update(
        original_amount_coefficient="-4",
        original_amount_scale=0,
        original_amount_source_scale=None,
        original_commodity_id=None,
    )
    with pytest.raises(ContractError, match="original_amount_commodity"):
        _validate_model(project, tables, "0" * 64)


def test_actual_processor_and_independent_reader_distinguish_net_from_gross() -> None:
    raw = source(
        bank=(("credit", "20260112", 10),), card=(), bank_balance=110, card_balance=0
    )
    value, extracts = history((raw,))
    position = deepcopy(next(d for d in value["declarations"] if d["key"] == "cash"))
    position["key"] = "bank-claim"
    position["payload"]["position_id"] = "bank-claim"
    binding = deepcopy(
        next(d for d in value["declarations"] if d["key"] == "bank")["payload"][
            "bindings"
        ][0]
    )
    binding["source_position_key"] = "authored-claim"
    position["payload"]["bindings"] = [binding]
    value["declarations"].append(position)
    claim = deepcopy(
        next(d for d in value["declarations"] if d["key"] == "initial-cash")
    )
    claim["key"] = "authored-negative-claim"
    claim["payload"]["record"].update(
        event_kind="transaction", date_posted="2026-01-02"
    )
    for leg in claim["payload"]["record"]["legs"]:
        if leg["leg_kind"] == "position":
            leg["position_id"] = "bank-claim"
            leg["amount"]["coefficient"] = "-50"
        else:
            leg["amount"]["coefficient"] = "50"
    value["declarations"].append(claim)
    scope = deepcopy(
        next(
            d
            for d in value["declarations"]
            if d["payload"]["kind"] == "assertion_scope"
            and d["payload"]["account_id"] == "bank"
        )
    )
    net_scope = scope["payload"]["scope_id"]
    scope["key"] = "gross-bank"
    scope["payload"].update(
        scope_id="gross-bank",
        scope_kind="positions",
        selection="positions",
        position_ids=["bank-claim", "cash"],
    )
    value["declarations"].append(scope)
    timestamp = {
        "timestamp_date": None,
        "timestamp_local": None,
        "timestamp_offset_minutes": None,
        "timestamp_zone": None,
        "timestamp_precision": None,
        "timestamp_fraction_digits": None,
    }
    for key, scope_id, observations in (
        ("stated-gross", "gross-bank", (("cash", "100"), ("bank-claim", "-40"))),
        ("stated-net", net_scope, ((None, "60"),)),
        ("stated-empty", "gross-bank", ()),
        ("stated-partial-empty", "gross-bank", ()),
        ("stated-ambiguous", "gross-bank", ((None, "60"),)),
    ):
        value["declarations"].append(
            {
                "key": key,
                "valid_from": None,
                "valid_to": None,
                "payload": {
                    "schema_version": 1,
                    "kind": "authored_fact",
                    "fact_kind": "assertion",
                    "event_key": "assertion",
                    "evidence": [],
                    "record": {
                        "scope_id": scope_id,
                        "date": "2026-01-31",
                        "assertion_kind": "closing",
                        "is_complete": key == "stated-empty",
                        **timestamp,
                        "observations": [
                            {
                                "observation_key": f"observation-{index}",
                                "position_id": identifier,
                                "commodity_id": "USD",
                                "amount": {
                                    "coefficient": amount,
                                    "scale": 0,
                                    "source_scale": None,
                                },
                            }
                            for index, (identifier, amount) in enumerate(observations)
                        ],
                    },
                },
            }
        )
    project, inputs, resolution, tables = run(value, extracts)
    assert not any(f.blocking for f in resolution.findings)
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    checks = model_checks(tables, frozenset({CheckKind.MODEL}))
    gross_id, net_id = (
        projection_key("authored-assertion", key)
        for key in ("stated-gross", "stated-net")
    )
    assert [c.status for c in checks if gross_id in c.key.value] == [
        CheckStatus.FAIL,
        CheckStatus.FAIL,
    ]
    assert [c.status for c in checks if net_id in c.key.value] == [CheckStatus.PASS]
    for key, expected in (
        ("stated-empty", [CheckStatus.FAIL, CheckStatus.FAIL]),
        ("stated-partial-empty", [CheckStatus.NOT_EVALUABLE]),
        ("stated-ambiguous", [CheckStatus.NOT_EVALUABLE]),
    ):
        identity = projection_key("authored-assertion", key)
        assert [c.status for c in checks if identity in c.key.value] == expected
    independent = check_assertions(
        tables, entity="invented-owner", date_roles={"posted": "posted"}
    )
    (gross,) = [c for c in independent if c.assertion_set_id == gross_id]
    assert gross.status == "fail"
    assert set(gross.expected.values()) == {100, -40}
    assert set(gross.observed.values()) == {110, -50}
    assert [c.status for c in independent if c.assertion_set_id == net_id] == ["pass"]


def source(  # noqa: PLR0913 - independent synthetic statement controls
    *,
    bank: tuple[tuple[str, str, int], ...],
    card: tuple[tuple[str, str, int], ...],
    bank_balance: int,
    card_balance: int,
    prior: bool = False,
    pending: bool = False,
    marker: str = "download",
    bank_number: str = "INVENTED-1",
) -> bytes:
    start, end = ("20251231", "20260101") if prior else ("20260101", "20260201")

    def section(
        kind: str, movements: tuple[tuple[str, str, int], ...], balance: int
    ) -> str:
        is_bank = kind == "bank"
        account = (
            f"<BANKACCTFROM><BANKID>TESTBANK</BANKID><ACCTID>{bank_number}</ACCTID>"
            "<ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>"
            if is_bank
            else "<CCACCTFROM><ACCTID>INVENTED-CARD</ACCTID></CCACCTFROM>"
        )
        rows = "".join(
            f"<STMTTRN><TRNTYPE>{'CREDIT' if amount > 0 else 'DEBIT'}</TRNTYPE>"
            f"<DTPOSTED>{when}</DTPOSTED><TRNAMT>{amount}.00</TRNAMT>"
            f"<FITID>{key}</FITID>"
            f"<NAME>Invented {'income' if amount > 0 else 'expense'}</NAME></STMTTRN>"
            for key, when, amount in movements
        )
        held = (
            "<BANKTRANLISTP><DTASOF>20260125</DTASOF><STMTTRNP><TRNTYPE>DEBIT</TRNTYPE>"
            "<DTTRAN>20260124</DTTRAN><TRNAMT>-9.00</TRNAMT>"
            "<NAME>Invented pending</NAME>"
            "</STMTTRNP></BANKTRANLISTP>"
            if pending and not is_bank
            else ""
        )
        tag = "STMTRS" if is_bank else "CCSTMTRS"
        response = "STMTTRNRS" if is_bank else "CCSTMTTRNRS"
        messages = "BANKMSGSRSV1" if is_bank else "CREDITCARDMSGSRSV1"
        return (
            f"<{messages}><{response}><TRNUID>{marker}-{kind}</TRNUID>"
            "<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
            f"<{tag}><CURDEF>USD</CURDEF>{account}<BANKTRANLIST>"
            f"<DTSTART>{start}000000[0:GMT]</DTSTART><DTEND>{end}000000[0:GMT]</DTEND>"
            f"{rows}</BANKTRANLIST>{held}<LEDGERBAL><BALAMT>{balance}.00</BALAMT>"
            f"<DTASOF>{end}000000[0:GMT]</DTASOF></LEDGERBAL></{tag}></{response}></{messages}>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" '
        'OLDFILEUID="NONE" NEWFILEUID="NONE"?>'
        f"<OFX>{section('bank', bank, bank_balance)}"
        f"{section('card', card, card_balance)}</OFX>"
    ).encode()


def history(
    downloads: tuple[bytes, ...] | None = None,
) -> tuple[dict, tuple[bytes, ...]]:
    prior = source(bank=(), card=(), bank_balance=100, card_balance=0, prior=True)
    main = source(
        bank=(
            ("credit", "20260112", 20),
            ("debit", "20260120", -5),
            ("fee", "20260128", -7),
        ),
        card=(
            ("purchase", "20260103", -8),
            ("overpay", "20260115", 15),
            ("fee", "20260129", -1),
        ),
        bank_balance=108,
        card_balance=6,
        pending=True,
    )
    raws = (prior, *(downloads if downloads is not None else (main,)))
    extracts = tuple(
        extract_wire(
            OfxImporter(), raw, ExtractionContext("bank-feed", byte_digest(raw), "ofx")
        )
        for raw in raws
    )
    documents = tuple(json.loads(payload) for payload in extracts)
    value = json.loads(
        (ROOT / "tests/jbt/runtime/fixtures_synthetic/project.json").read_bytes()
    )
    value["sources"] = [byte_digest(raw) for raw in raws]
    value["configuration"]["as_of"] = "2026-02-01"
    account = next(
        d for d in value["declarations"] if d["payload"]["kind"] == "account"
    )
    account["payload"]["bindings"] = [
        {
            "source_scope_id": "bank-feed",
            "source_section": documents[0]["source_scope"]["sections"][0]["section_id"],
            "source_position_key": None,
        }
    ]
    position = next(
        d for d in value["declarations"] if d["payload"]["kind"] == "position"
    )
    assertion_scope = next(
        d for d in value["declarations"] if d["payload"]["kind"] == "assertion_scope"
    )
    opening = next(
        d for d in value["declarations"] if d["payload"]["kind"] == "authored_fact"
    )
    for declaration in (account, position, assertion_scope, opening):
        copy = deepcopy(declaration)
        copy["key"] = f"card-{copy['key']}"
        payload = copy["payload"]
        if payload["kind"] == "account":
            payload["account_id"] = "card"
            payload["bindings"][0]["source_section"] = documents[0]["source_scope"][
                "sections"
            ][1]["section_id"]
        elif payload["kind"] == "position":
            payload.update(position_id="card-cash", account_id="card")
        elif payload["kind"] == "assertion_scope":
            payload.update(scope_id="card-scope", account_id="card", position_ids=[])
        else:
            payload["evidence"] = [
                {
                    "kind": "source_record",
                    "id": next(
                        r["source_record_id"]
                        for r in documents[0]["records"]
                        if r["kind"] == "assertion"
                        and r["source_section"]
                        == documents[0]["source_scope"]["sections"][1]["section_id"]
                    ),
                }
            ]
            for leg in payload["record"]["legs"]:
                leg["amount"]["coefficient"] = "0"
                if leg["leg_kind"] == "position":
                    leg.update(account_id="card", position_id="card-cash")
        value["declarations"].append(copy)
    opening["payload"]["evidence"][0]["id"] = next(
        r["source_record_id"]
        for r in documents[0]["records"]
        if r["kind"] == "assertion"
        and r["source_section"]
        == documents[0]["source_scope"]["sections"][0]["section_id"]
    )
    value["configuration"]["rendering"].append(
        {
            "entity_id": value["entity_id"],
            "sink": "beancount",
            "target": {"table": "positions", "key": ["card-cash"]},
            "positive_name": "Liabilities:Card",
            "negative_name": None,
            "primary_date_role": "posted",
        }
    )
    return value, extracts


def run(
    value: dict, extracts: tuple[bytes, ...]
) -> tuple[
    Project, tuple[ExtractInput, ...], Resolution, dict[str, list[dict[str, object]]]
]:
    project = read_project(encode_json(value, integer_strings=False))
    inputs = tuple(
        ExtractInput(selection, payload)
        for selection, payload in zip(project.sources, extracts, strict=True)
    )
    prepared = prepare(project, inputs)
    resolution = resolve(prepared.inputs)
    tables = project_resolution(prepared, resolution)
    return project, inputs, resolution, tables


def _without_asserted_keys(extracts: tuple[bytes, ...]) -> tuple[bytes, ...]:
    retained = []
    for encoded in extracts:
        extract = json.loads(encoded)
        for record in extract["records"]:
            if record["kind"] == "transaction":
                record["source_key_domain"] = record["source_key"] = None
        retained.append(encode_json(extract, integer_strings=False))
    return tuple(retained)


def test_identical_keyless_rows_in_one_extract_keep_distinct_occurrence_identity() -> (
    None
):
    raw = source(
        bank=(("A", "20260110", -5), ("B", "20260110", -5)),
        card=(),
        bank_balance=90,
        card_balance=0,
    )
    value, extracts = history((raw,))
    project, inputs, resolution, tables = run(value, _without_asserted_keys(extracts))
    assert not [f for f in resolution.findings if f.blocking]
    purchases = [t for t in tables["transactions"] if t["event_kind"] == "transaction"]
    assert len(purchases) == len({t["txn_id"] for t in purchases}) == 2
    assert len(tables["event_support"]) == 2
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )


def test_differing_keyless_rows_in_overlapping_downloads_require_correspondence() -> (
    None
):
    downloads = tuple(
        source(
            bank=((key, when, amount),),
            card=(),
            bank_balance=100 + amount,
            card_balance=0,
            marker=key,
        )
        for key, when, amount in (("A", "20260110", -5), ("B", "20260112", -7))
    )
    value, extracts = history(downloads)
    project, inputs, resolution, tables = run(value, _without_asserted_keys(extracts))
    findings = [f for f in resolution.findings if f.code == FindingCode.OVERLAP]
    assert len(findings) == 2
    assert all(f.blocking for f in findings)
    assert sum(d.status == "blocked" for d in resolution.determinations) == 2
    assert not [t for t in tables["transactions"] if t["event_kind"] == "transaction"]
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )


def test_real_checking_card_processor_and_recognized_outputs() -> None:
    project, inputs, resolution, tables = run(*history())
    assert not [f for f in resolution.findings if f.blocking]
    assert len(tables["transactions"]) == 9
    assert sum(t["event_state"] == "pending" for t in tables["transactions"]) == 1
    _validate_model(project, tables, "0" * 64)
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    assert all(
        c.status == CheckStatus.PASS
        for c in model_checks(
            tables, frozenset({CheckKind.MODEL, CheckKind.OCCURRENCE})
        )
    )
    result = json.loads(
        summary(project, tables, checks=[], baseline=None, baseline_digest=None)
    )
    january = {
        p["account_id"]: p
        for p in result["monthly_quantities"]
        if p["month"] == "2026-01"
    }
    assert [
        [
            january[account][key]["coefficient"]
            for key in ("opening", "credits", "debits", "closing")
        ]
        for account in ("bank", "card")
    ] == [["100", "20", "-12", "108"], ["0", "15", "-9", "6"]]
    output = ledger(project, tables).decode()
    assert "Liabilities:Card" in output
    assert output.count("open Liabilities:Card") == 1
    assert "2026-01-03 !" in output
    assert "Invented pending" not in output


def test_rolling_keys_are_two_events_four_occurrences_order_independent() -> None:
    downloads = tuple(
        source(
            bank=(("A", "20260112", -5), ("B", "20260112", -5)),
            card=(),
            bank_balance=90,
            card_balance=0,
            marker=marker,
        )
        for marker in ("first", "second")
    )
    value, extracts = history(downloads)
    project, inputs, resolution, tables = run(value, extracts)
    assert not [f for f in resolution.findings if f.blocking]
    movements = [t for t in tables["transactions"] if t["event_kind"] == "transaction"]
    assert len(movements) == 2
    assert len(tables["event_support"]) == 4
    for event in movements:
        assert sorted(
            text(s["support_kind"], "support_kind")
            for s in tables["event_support"]
            if s["txn_id"] == event["txn_id"]
        ) == ["confirming", "selected"]
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    reversed_value = deepcopy(value)
    reversed_value["sources"] = list(reversed(value["sources"]))
    assert run(reversed_value, tuple(reversed(extracts)))[3] == tables
    dropped = deepcopy(tables)
    remove = next(
        p["posting_id"]
        for p in dropped["postings"]
        if p["leg_kind"] == "position" and p["amount_coefficient"] == "-5"
    )
    dropped["postings"] = [p for p in dropped["postings"] if p["posting_id"] != remove]
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    assert any(
        c.status == CheckStatus.FAIL
        for c in model_checks(dropped, frozenset({CheckKind.MODEL}))
    )


def test_policy_revisions_do_not_close_position_and_rule_is_dated() -> None:
    main = source(
        bank=(
            ("credit", "20260112", 20),
            ("debit", "20260120", -5),
            ("fee", "20260128", -7),
        ),
        card=(
            ("purchase", "20260103", -8),
            ("overpay", "20260115", 15),
            ("fee", "20260129", -1),
        ),
        bank_balance=108,
        card_balance=6,
    ).replace(b"<DTSTART>20260101000000", b"<DTSTART>20260102000000")
    value, _ = history((main,))
    prior = source(bank=(), card=(), bank_balance=100, card_balance=0, prior=True)
    prior = (
        prior.replace(b"20251231000000", b"20260101000000")
        .replace(b"20260101000000[0:GMT]</DTEND>", b"20260102000000[0:GMT]</DTEND>")
        .replace(b"20260101000000[0:GMT]</DTASOF>", b"20260102000000[0:GMT]</DTASOF>")
    )
    extracts = tuple(
        extract_wire(
            OfxImporter(), raw, ExtractionContext("bank-feed", byte_digest(raw), "ofx")
        )
        for raw in (prior, main)
    )
    value["sources"] = [byte_digest(raw) for raw in (prior, main)]
    prior_records = json.loads(extracts[0])["records"]
    for declaration in value["declarations"]:
        if declaration["payload"]["kind"] == "authored_fact":
            account = declaration["payload"]["record"]["legs"][0]["account_id"]
            binding = next(
                d
                for d in value["declarations"]
                if d["payload"]["kind"] == "account"
                and d["payload"]["account_id"] == account
            )["payload"]["bindings"][0]
            declaration["payload"]["evidence"][0]["id"] = next(
                r["source_record_id"]
                for r in prior_records
                if r["kind"] == "assertion"
                and r["source_section"] == binding["source_section"]
            )
            if account == "bank":
                declaration["payload"]["record"]["date_posted"] = "2026-01-01"
        elif declaration["key"] == "bank":
            declaration["payload"].update(
                opened_date="2026-01-01", coverage_start_date="2026-01-01"
            )
    position = next(
        d
        for d in value["declarations"]
        if d["payload"]["kind"] == "position" and d["key"] == "cash"
    )
    later = deepcopy(position)
    position["valid_from"] = "2026-01-01"
    position["valid_to"] = later["valid_from"] = "2026-01-15"
    value["declarations"].append(later)
    rule = next(d for d in value["declarations"] if d["key"] == "rule-income")
    rule["valid_from"] = "2026-01-01"
    later_rule = deepcopy(rule)
    rule["valid_to"] = later_rule["valid_from"] = "2026-01-15"
    later_rule["payload"]["assignments"][0]["value"] = "equity"
    value["declarations"].append(later_rule)
    project, _, resolution, tables = run(value, extracts)
    assert not [f for f in resolution.findings if f.blocking]
    output = ledger(project, tables).decode()
    assert output.count("open Assets:Bank") == 1
    assert "2026-01-01 open Assets:Bank" in output
    assert "close Assets:Bank" not in output
    categories = {
        t["date_posted"]: p["category_id"]
        for t in tables["transactions"]
        for p in tables["postings"]
        if p["txn_id"] == t["txn_id"]
        and p["leg_kind"] == "boundary"
        and t["payee"] == "Invented income"
    }
    assert categories["2026-01-12"] == "income"
    assert categories["2026-01-15"] == "equity"


def test_half_open_source_interval_keeps_outside_evidence() -> None:
    downloads = (
        source(
            bank=(("first", "20260101", -5), ("outside", "20260201", -9)),
            card=(),
            bank_balance=95,
            card_balance=0,
        ),
    )
    project, inputs, resolution, tables = run(*history(downloads))
    assert not [f for f in resolution.findings if f.blocking]
    assert len(tables["event_support"]) == 2
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    assert all(
        c.status == CheckStatus.PASS
        for c in model_checks(tables, frozenset({CheckKind.MODEL}))
    )
    value, extracts = history(downloads)
    value["configuration"]["as_of"] = "2026-01-31"
    _, _, _, bounded = run(value, extracts)
    assert len(bounded["source_records"]) == len(tables["source_records"])
    assert len(bounded["event_support"]) == 1
    assert all(
        c.status == CheckStatus.PASS
        for c in model_checks(
            bounded, frozenset({CheckKind.MODEL, CheckKind.OCCURRENCE})
        )
    )


def test_checks_have_no_model_imports() -> None:
    for path in (ROOT / "src/jbt/checks").rglob("*.py"):
        tree = ast.parse(path.read_text())
        assert not any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("jbt.model")
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.startswith("jbt.model") for alias in node.names)
            )
            for node in ast.walk(tree)
        )


def test_unbound_is_blocked_ignored_is_retained_and_account_number_change_binds() -> (
    None
):
    value, extracts = history()
    bank = next(d for d in value["declarations"] if d["key"] == "bank")
    binding = deepcopy(bank["payload"]["bindings"][0])
    bank["payload"]["bindings"] = []
    project, inputs, resolution, tables = run(value, extracts)
    assert any(
        f.code == FindingCode.UNBOUND and f.blocking for f in resolution.findings
    )
    value["configuration"]["ignored_sections"] = [
        {
            "source_scope_id": binding["source_scope_id"],
            "source_section": binding["source_section"],
            "reason": "Explicitly excluded synthetic account",
        }
    ]
    # Ignoring also requires removing authored financial references to that account.
    value["declarations"] = [
        d for d in value["declarations"] if d["key"] != "initial-cash"
    ]
    _, _, ignored_resolution, ignored_tables = run(value, extracts)
    assert not any(f.code == FindingCode.UNBOUND for f in ignored_resolution.findings)
    assert len(ignored_tables["source_records"]) == len(tables["source_records"])
    changed = source(
        bank=(("after-change", "20260120", -5),),
        card=(),
        bank_balance=95,
        card_balance=0,
        bank_number="INVENTED-2",
    )
    value, extracts = history((changed,))
    changed_document = json.loads(extracts[1])
    bank = next(d for d in value["declarations"] if d["key"] == "bank")
    bank["payload"]["bindings"].append(
        {
            "source_scope_id": "bank-feed",
            "source_section": changed_document["source_scope"]["sections"][0][
                "section_id"
            ],
            "source_position_key": None,
        }
    )
    project, inputs, resolution, tables = run(value, extracts)
    assert not [f for f in resolution.findings if f.blocking]
    assert {
        p["account_id"]
        for p in tables["postings"]
        if p["amount_coefficient"] == "-5" and p["leg_kind"] == "position"
    } == {"bank"}
    checks = retained_source_checks(project, inputs)
    assert [c.status for c in checks if c.account and c.account.value == "bank"] == [
        CheckStatus.NOT_EVALUABLE
    ]
    assert [c.status for c in checks if c.account and c.account.value == "card"] == [
        CheckStatus.PASS
    ]


def test_key_conflict_returns_complete_blocked_determinations() -> None:
    downloads = tuple(
        source(
            bank=(("A", "20260112", amount),),
            card=(),
            bank_balance=100 + amount,
            card_balance=0,
            marker=str(amount),
        )
        for amount in (-5, -6)
    )
    _, _, resolution, _ = run(*history(downloads))
    assert any(f.code == FindingCode.KEY_CONFLICT for f in resolution.findings)
    blocked = [d for d in resolution.determinations if d.status.value == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].event is None
    assert len(resolution.events) == 2  # independent authored openings survive


@pytest.mark.parametrize("mode", ["disagreement", "expired", "ambiguous-position"])
def test_symbol_and_position_resolution_fails_closed(mode: str) -> None:
    value, extracts = history()
    usd = next(d for d in value["declarations"] if d["key"] == "usd")
    if mode == "expired":
        usd["payload"]["symbols"][0]["valid_to"] = "2026-01-10"
    elif mode == "disagreement":
        second = deepcopy(usd)
        second["key"] = "other-dollar"
        second["payload"]["commodity_id"] = "OTHER"
        second["payload"]["symbols"][0].update(
            symbol_id="other-dollar-symbol", source_scope_id="bank-feed"
        )
        value["declarations"].append(second)
    else:
        duplicate = deepcopy(
            next(d for d in value["declarations"] if d["key"] == "cash")
        )
        duplicate["key"] = duplicate["payload"]["position_id"] = "other-cash"
        value["declarations"].append(duplicate)
    _, _, resolution, _ = run(value, extracts)
    expected = {
        "disagreement": FindingCode.SYMBOL_DISAGREEMENT,
        "expired": FindingCode.SYMBOL_VALIDITY,
        "ambiguous-position": FindingCode.POSITION_AMBIGUOUS,
    }[mode]
    assert any(f.code == expected and f.blocking for f in resolution.findings)


def test_unclassified_working_record_and_classification_sink_gate() -> None:
    value, extracts = history()
    value["declarations"] = [
        d for d in value["declarations"] if d["payload"]["kind"] != "rule"
    ]
    project, _, resolution, tables = run(value, extracts)
    assert not [f for f in resolution.findings if f.blocking]
    assert any(f.code == FindingCode.UNCLASSIFIED for f in resolution.findings)
    result = json.loads(
        summary(project, tables, checks=[], baseline=None, baseline_digest=None)
    )
    assert result["review"]["uncategorized_events"] == 6
    assert {w["account_id"]: w["leg_count"] for w in result["uncategorized_work"]} == {
        "bank": 3,
        "card": 3,
    }
    classification = _findings(
        project, model_checks(tables, frozenset({CheckKind.CLASSIFICATION}))
    )
    assert classification[0]["status"] == "fail"
    for sink in value["configuration"]["outputs"]:
        sink["required_checks"].append("classification")
    classified_project = read_project(encode_json(value, integer_strings=False))
    with pytest.raises(ContractError, match=r"required_check_missing|failed"):
        _effective_configuration(classified_project, classification)


def test_multi_account_authored_uncategorized_boundary_is_unattributed() -> None:
    value, extracts = history()
    opening = next(d for d in value["declarations"] if d["key"] == "initial-cash")
    authored = deepcopy(opening)
    authored["key"] = "multi-account"
    payload = authored["payload"]
    payload["record"].update(
        event_kind="transaction", date_posted="2026-01-10", review_state="unreviewed"
    )
    position, boundary = payload["record"]["legs"]
    position["amount"]["coefficient"] = "3"
    another = deepcopy(position)
    another.update(posting_key="card-leg", account_id="card", position_id="card-cash")
    another["amount"]["coefficient"] = "4"
    boundary.update(category_id=None)
    boundary["amount"]["coefficient"] = "-7"
    payload["record"]["legs"].append(another)
    value["declarations"].append(authored)
    project, _, resolution, tables = run(value, extracts)
    assert not [f for f in resolution.findings if f.blocking]
    result = json.loads(
        summary(project, tables, checks=[], baseline=None, baseline_digest=None)
    )
    assert result["uncategorized_work"] == [
        {
            "commodity_id": "USD",
            "account_id": None,
            "attribution": "unattributed",
            "leg_count": 1,
            "amount": {"coefficient": "-7", "scale": 0, "source_scale": None},
        }
    ]


def test_net_and_both_gross_are_independent_complete_empty_is_not_partial() -> None:
    # Independently constructed projected-table oracles, not model internals.
    tables = {
        "transactions": [
            {
                "txn_id": "positive",
                "event_state": "recognized",
                "date_posted": "2026-01-02",
            },
            {
                "txn_id": "negative",
                "event_state": "recognized",
                "date_posted": "2026-01-02",
            },
        ],
        "positions": [
            {
                "position_id": "asset",
                "account_id": "bank",
                "commodity_id": "USD",
                "purpose": "general",
            },
            {
                "position_id": "claim",
                "account_id": "bank",
                "commodity_id": "USD",
                "purpose": "general",
            },
        ],
        "postings": [
            {
                "txn_id": "positive",
                "posting_id": "positive",
                "position_id": "asset",
                "leg_kind": "position",
                "commodity_id": "USD",
                "amount_coefficient": "110",
                "amount_scale": 0,
                "amount_source_scale": None,
            },
            {
                "txn_id": "negative",
                "posting_id": "negative",
                "position_id": "claim",
                "leg_kind": "position",
                "commodity_id": "USD",
                "amount_coefficient": "-50",
                "amount_scale": 0,
                "amount_source_scale": None,
            },
        ],
        "assertion_scopes": [
            {
                "scope_id": "gross",
                "account_id": "bank",
                "selection": "positions",
                "scope_kind": "positions",
                "position_ids": ["asset", "claim"],
                "measurement": "units",
            },
            {
                "scope_id": "net",
                "account_id": "bank",
                "selection": "account_all",
                "scope_kind": "account_net",
                "position_ids": [],
                "measurement": "units",
            },
        ],
        "balance_assertions": [
            {
                "assertion_set_id": "gross",
                "scope_id": "gross",
                "date": "2026-01-03",
                "assertion_kind": "point",
                "is_complete": False,
            },
            {
                "assertion_set_id": "net",
                "scope_id": "net",
                "date": "2026-01-03",
                "assertion_kind": "point",
                "is_complete": False,
            },
        ],
        "balances": [
            {
                "assertion_set_id": "gross",
                "position_id": "asset",
                "commodity_id": "USD",
                "amount_coefficient": "100",
                "amount_scale": 0,
                "amount_source_scale": None,
            },
            {
                "assertion_set_id": "gross",
                "position_id": "claim",
                "commodity_id": "USD",
                "amount_coefficient": "-40",
                "amount_scale": 0,
                "amount_source_scale": None,
            },
            {
                "assertion_set_id": "net",
                "position_id": None,
                "commodity_id": "USD",
                "amount_coefficient": "60",
                "amount_scale": 0,
                "amount_source_scale": None,
            },
        ],
        "links": [],
    }
    checks = model_checks(tables, frozenset({CheckKind.MODEL}))
    assert [(c.key.value, c.status.value) for c in checks] == [
        ("model:gross:asset:USD", "fail"),
        ("model:gross:claim:USD", "fail"),
        ("model:net:net:USD", "pass"),
    ]
    tables["balances"] = []
    tables["balance_assertions"] = [tables["balance_assertions"][0]]
    assert (
        model_checks(tables, frozenset({CheckKind.MODEL}))[0].status
        == CheckStatus.NOT_EVALUABLE
    )
    tables["balance_assertions"][0]["is_complete"] = True
    assert [c.status for c in model_checks(tables, frozenset({CheckKind.MODEL}))] == [
        CheckStatus.FAIL,
        CheckStatus.FAIL,
    ]
