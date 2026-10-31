import json
from copy import deepcopy

import pytest
from tests.jbt.model.test_quantity import history, run, source

from jbt.artifacts.integrity import byte_digest
from jbt.checks.quantity import CheckKind, CheckStatus
from jbt.domain.canonical import encode_json
from jbt.extraction import ExtractionContext
from jbt.extraction import extract as extract_wire
from jbt.importers.ofx import OfxImporter
from jbt.runtime.checks import (
    projected_account_checks,
    projected_model_checks,
    retained_source_checks,
)
from jbt.runtime.outputs import ledger
from jbt.runtime.project import read_project


def test_projected_occurrence_check_detects_dropped_support() -> None:
    project, inputs, _, tables = run(*history())
    dropped = deepcopy(tables)
    dropped["event_support"].pop()
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    assert (
        projected_model_checks(dropped, frozenset({CheckKind.OCCURRENCE}))[0].status
        == CheckStatus.FAIL
    )


def test_source_groups_do_not_depend_on_authored_opening_citations() -> None:
    value, extracts = history()
    project, inputs, _, _ = run(value, extracts)
    expected = retained_source_checks(project, inputs)
    value["declarations"] = [
        d for d in value["declarations"] if d["payload"]["kind"] != "authored_fact"
    ]
    without_openings = read_project(encode_json(value, integer_strings=False))
    assert retained_source_checks(without_openings, inputs) == expected
    assert len(expected) == 2


def test_coverage_requires_live_complete_claims_not_rows_or_transactions(
    project_document: dict,
) -> None:
    tables: dict[str, list[dict[str, object]]] = {
        name: []
        for name in (
            "transactions",
            "positions",
            "postings",
            "balance_assertions",
            "event_times",
        )
    }
    tables["statements"] = [
        {"statement_id": "statement", "account_id": "bank", "is_eclipsed": False}
    ]
    tables["statement_coverage"] = [
        {
            "statement_id": "statement",
            "coverage_basis": "posted",
            "date_role": "posted",
            "period_start": "2026-01-01",
            "period_end": "2026-02-01",
            "is_complete": False,
            "is_eclipsed": False,
        }
    ]
    project_document["configuration"]["as_of"] = "2026-01-30"
    project = read_project(encode_json(project_document, integer_strings=False))
    assert [
        c.status
        for c in projected_account_checks(project, tables)
        if c.kind == CheckKind.COVERAGE
    ] == [CheckStatus.PASS]
    project_document["configuration"]["as_of"] = "2026-02-02"
    project = read_project(encode_json(project_document, integer_strings=False))
    (missing,) = (
        c
        for c in projected_account_checks(project, tables)
        if c.kind == CheckKind.COVERAGE
    )
    assert missing.status == CheckStatus.FAIL
    assert isinstance(missing.observed, str)
    assert json.loads(missing.observed)["gaps"] == [
        {"start": "2026-01-01", "end": "2026-02-01"}
    ]

    for row in tables["statement_coverage"]:
        row["is_complete"] = True
    assert [
        c.status
        for c in projected_account_checks(project, tables)
        if c.kind == CheckKind.COVERAGE
    ] == [CheckStatus.PASS]
    for row in tables["statement_coverage"]:
        row["is_eclipsed"] = True
    assert [
        c.status
        for c in projected_account_checks(project, tables)
        if c.kind == CheckKind.COVERAGE
    ] == [CheckStatus.FAIL]


def test_source_and_model_use_period_end_civil_clock_not_utc_date() -> None:
    main = source(
        bank=(("late", "20260201045000", -5),),
        card=(),
        bank_balance=95,
        card_balance=0,
    )
    prior = source(bank=(), card=(), bank_balance=100, card_balance=0, prior=True)
    raws = tuple(
        raw.replace(b"[0:GMT]", b"[-5:America/New_York]") for raw in (prior, main)
    )
    value, _ = history((main,))
    retained = [
        json.loads(
            extract_wire(
                OfxImporter(),
                raw,
                ExtractionContext("bank-feed", byte_digest(raw), "ofx"),
            )
        )
        for raw in raws
    ]
    for extract in retained:
        for record in extract["records"]:
            if record["kind"] == "assertion":
                record["payload"].update(
                    timestamp_local="00:00",
                    timestamp_precision="minute",
                    timestamp_fraction_digits=None,
                )
            elif record["kind"] == "transaction":
                record["payload"]["times"][0].update(
                    timestamp_offset_seconds=0,
                    timestamp_zone="Etc/UTC",
                )
    extracts = tuple(
        encode_json(extract, integer_strings=False) for extract in retained
    )
    value["sources"] = [byte_digest(raw) for raw in raws]
    prior_records = json.loads(extracts[0])["records"]
    for declaration in value["declarations"]:
        if declaration["payload"]["kind"] == "account":
            declaration["payload"]["civil_zone"] = "America/New_York"
        elif declaration["payload"]["kind"] == "authored_fact":
            payload = declaration["payload"]
            account = payload["record"]["legs"][0]["account_id"]
            binding = next(
                d["payload"]["bindings"][0]
                for d in value["declarations"]
                if d["payload"]["kind"] == "account"
                and d["payload"]["account_id"] == account
            )
            payload["evidence"][0]["id"] = next(
                record["source_record_id"]
                for record in prior_records
                if record["kind"] == "assertion"
                and record["source_section"] == binding["source_section"]
            )
    project, inputs, resolution, tables = run(value, extracts)
    assert not any(f.blocking for f in resolution.findings)
    assert [
        row["date_posted"]
        for row in tables["transactions"]
        if row["event_kind"] == "transaction"
    ] == ["2026-01-31"]
    transaction = next(
        row for row in retained[1]["records"] if row["kind"] == "transaction"
    )
    assert transaction["payload"]["times"][0]["timestamp_date"] == "2026-02-01"
    assert transaction["source_record_id"] in {
        row["source_record_id"] for row in tables["source_records"]
    }
    assert all(
        c.status == CheckStatus.PASS for c in retained_source_checks(project, inputs)
    )
    assert all(
        c.status == CheckStatus.PASS
        for c in projected_model_checks(tables, frozenset({CheckKind.MODEL}))
    )
    assert b"2026-01-31" in ledger(project, tables)


def transfer_tables() -> dict[str, list[dict]]:
    return {
        "transactions": [
            {
                "txn_id": "departure",
                "event_state": "recognized",
                "date_posted": "2026-01-31",
            },
            {
                "txn_id": "arrival",
                "event_state": "recognized",
                "date_posted": "2026-02-02",
            },
        ],
        "positions": [
            {
                "position_id": key,
                "account_id": key,
                "commodity_id": "USD",
                "purpose": purpose,
            }
            for key, purpose in (
                ("checking", "general"),
                ("card", "general"),
                ("transit", "in_transit"),
            )
        ],
        "postings": [
            {
                "posting_id": key,
                "txn_id": event,
                "position_id": position,
                "commodity_id": "USD",
                "amount_coefficient": amount,
                "amount_scale": 0,
                "amount_source_scale": None,
                "leg_kind": "position",
                "category_id": None,
            }
            for key, event, position, amount in (
                ("out", "departure", "checking", "-10"),
                ("in-a", "arrival", "card", "4"),
                ("in-b", "arrival", "card", "6"),
                ("transit-in", "departure", "transit", "10"),
                ("transit-out", "arrival", "transit", "-10"),
            )
        ],
        "balance_assertions": [],
        "links": [
            {
                "link_id": "payment",
                "link_kind": "transfer",
                "origin": "correction",
                "terms_declaration_id": None,
                "member_role": role,
                "member_kind": "posting",
                "member_id": key,
            }
            for role, key in (
                ("outgoing", "out"),
                ("incoming", "in-a"),
                ("incoming", "in-b"),
            )
        ],
    }


def test_transfer_checks_read_nary_source_members_without_optional_selectors() -> None:
    tables = transfer_tables()
    checks = projected_model_checks(tables, frozenset())
    assert len(checks) == 1
    assert checks[0].kind == CheckKind.TRANSFER
    assert checks[0].status == CheckStatus.PASS
    assert {key.value for key in checks[0].evidence} == {"out", "in-a", "in-b"}
    tables["links"].reverse()
    assert projected_model_checks(tables, frozenset()) == checks
    tables["links"] = []
    assert projected_model_checks(tables, frozenset()) == ()


@pytest.mark.parametrize(
    "damage",
    [
        "amount",
        "sign",
        "commodity",
        "state",
        "boundary",
        "transit",
        "missing",
        "reused",
        "side",
        "origin",
        "terms",
    ],
)
def test_transfer_checks_reject_malformed_or_nonconserving_members(  # noqa: C901 - independent failure modes
    damage: str,
) -> None:
    tables = transfer_tables()
    incoming = tables["postings"][1]
    if damage == "amount":
        incoming["amount_coefficient"] = "5"
    elif damage == "sign":
        incoming["amount_coefficient"] = "-4"
    elif damage == "commodity":
        incoming["commodity_id"] = "EUR"
    elif damage == "state":
        tables["transactions"][1]["event_state"] = "pending"
    elif damage == "boundary":
        incoming["leg_kind"] = "boundary"
    elif damage == "transit":
        tables["links"][1]["member_id"] = "transit-in"
    elif damage == "missing":
        tables["links"][1]["member_id"] = "absent"
    elif damage == "reused":
        tables["links"].append({**tables["links"][0], "link_id": "other"})
    elif damage == "side":
        tables["links"] = tables["links"][:1]
    elif damage == "origin":
        tables["links"][0]["origin"] = "derived"
    else:
        tables["links"][0]["terms_declaration_id"] = "terms"
    assert all(
        c.status == CheckStatus.FAIL
        for c in projected_model_checks(tables, frozenset())
    )
