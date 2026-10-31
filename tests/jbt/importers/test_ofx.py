import json
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jbt.domain.identity import (
    Occurrence,
    SourceKeyAnchor,
    event_id,
    source_record_digest,
)
from jbt.domain.ids import EntityId, SemanticKey
from jbt.extraction import ExtractionContext
from jbt.extraction import extract as extract_wire
from jbt.importers import ofx
from jbt.importers.ofx import (
    MAX_BYTES,
    MAX_DEPTH,
    MAX_NODES,
    MAX_ROWS,
    OfxError,
    extract,
    importer,
    source_anchor,
)

FIXTURES = Path(__file__).parent / "fixtures_synthetic"

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET


def _source(name: str = "bank-card") -> bytes:
    return (FIXTURES / f"{name}.ofx").read_bytes()


def _wire(raw: bytes) -> dict:
    context = ExtractionContext("synthetic-feed", sha256(raw).hexdigest(), "ofx")
    return json.loads(extract_wire(importer, raw, context))


def test_hand_calculated_bank_card_facts_and_separate_evidence() -> None:
    document = _wire(_source())
    bank, card = document["source_scope"]["sections"]
    assert (bank["source_account_identifier"], card["source_account_identifier"]) == (
        "CHECKING-1",
        "CARD-1",
    )
    assert {v["field_name"]: v["text_value"] for v in bank["account_context"]}[
        "BRANCHID"
    ] == "BR-1"
    assert {v["field_name"]: v["text_value"] for v in card["account_context"]}[
        "ORG"
    ] == "InventedIssuer"
    rows = [r for r in document["records"] if r["kind"] == "transaction"]
    assert [(r["source_key"], r["payload"]["legs"][0]["amount"]) for r in rows] == [
        ("same-a", {"coefficient": "20", "scale": 0, "source_scale": 2}),
        ("same-b", {"coefficient": "20", "scale": 0, "source_scale": 2}),
        (
            "same-a",
            {"coefficient": "-5000000000000000001", "scale": 18, "source_scale": 18},
        ),
        ("replacement", {"coefficient": "2", "scale": 0, "source_scale": 2}),
        ("deletion", {"coefficient": "0", "scale": 0, "source_scale": 3}),
        (None, {"coefficient": "-7", "scale": 0, "source_scale": 2}),
        (None, {"coefficient": "-11", "scale": 0, "source_scale": 2}),
    ]
    assert rows[0]["payload"]["description_as_stated"] == "  Cafe\u0301  "
    assert rows[1]["payload"]["payee_as_stated"] == "  Cafe\u0301  "
    assert [
        r["payload"]["text_value"]
        for r in document["records"]
        if r["payload"].get("field_name") == "source.memo"
    ] == ["  exact memo\u0301  "] * 2 + ["Hold memo", "Purchase memo"]
    assert rows[2]["payload"]["times"] == [
        {
            "date_role": "posted",
            "timestamp_date": "2026-01-31",
            "timestamp_local": "23:50:00.124",
            "timestamp_offset_seconds": 19800,
            "timestamp_zone": "IST",
            "timestamp_precision": "fractional_second",
            "timestamp_fraction_digits": 3,
        }
    ]
    assert rows[0]["payload"]["times"][0]["timestamp_local"] is None
    fx = rows[2]["payload"]["legs"][0]
    assert (
        fx["fx_base_commodity_token"],
        fx["fx_quote_commodity_token"],
        fx["fx_rate_as_stated"],
    ) == ("EUR", "USD", {"coefficient": "125", "scale": 2, "source_scale": 4})
    assert fx["original_amount"] is None
    assert rows[3]["source_correction"] == {
        "source_key_domain": "ofx-fitid",
        "target_source_key": "old-a",
        "action": "replace",
    }
    assert rows[4]["source_correction"]["action"] == "delete"
    assert rows[3]["payload"]["date_posted"] == "2026-01-10"
    assert rows[3]["payload"]["description_as_stated"] is None
    pending = rows[-2]
    assert pending["source_key_domain"] is None
    assert pending["payload"]["date_posted"] is None
    assert pending["payload"]["date_authorized"] is None
    assert pending["payload"]["times"][0]["date_role"] == "initiated"
    assert pending["payload"]["times"][0]["timestamp_date"] == "2025-12-31"
    assert pending["payload"]["times"][1]["date_role"] == "expires"
    assert pending["payload"]["status_token"] == "STMTTRNP"  # noqa: S105
    assert [p["part_key"] for p in bank["evidence_parts"]] == [
        "posted",
        "settled",
        "available",
    ]
    assert [p["part_key"] for p in card["evidence_parts"]] == [
        "posted",
        "settled",
        "available",
        "pending",
    ]
    snapshot = card["evidence_parts"][-1]
    assert snapshot["point"]["raw_value"] == "20260215120000[-5]"
    assert snapshot["record_locators"] == [row["record_locator"] for row in rows[-2:]]
    assert all(
        p["completeness"] == "unstated"
        for s in (bank, card)
        for p in s["evidence_parts"]
    )
    assertions = [r for r in document["records"] if r["kind"] == "assertion"]
    assert [
        r["payload"]["observations"][0]["amount"]["coefficient"] for r in assertions
    ] == ["140", "-3000000000000000001"]
    assert all(
        r["payload"]["assertion_kind"] == "point" and not r["payload"]["is_complete"]
        for r in assertions
    )
    available = [
        r
        for r in document["records"]
        if r["payload"].get("field_name")
        in {"source.available_funds", "source.available_credit"}
    ]
    assert [
        (r["kind"], r["payload"]["decimal_value"]["coefficient"]) for r in available
    ] == [("observation", "130"), ("observation", "997")]
    assert document["status_mappings"] == [
        {
            "source_field": "source.status",
            "source_token": "STMTTRN",
            "event_key": "transaction",
            "candidate_state": "recognized",
        },
        {
            "source_field": "source.status",
            "source_token": "STMTTRNP",
            "event_key": "transaction",
            "candidate_state": "pending",
        },
    ]
    assert all(
        r["payload"]["observation_kind"] == "source_status"
        for r in document["records"]
        if r["payload"].get("field_name") == "source.status"
    )
    context = ExtractionContext("synthetic-feed", document["source_blob_digest"], "ofx")
    for row in document["records"]:
        assert row["source_record_id"] == source_record_digest(
            source_anchor(context, row["source_section"], row["record_locator"])
        )
    field_name = "sections"
    with pytest.raises(FrozenInstanceError):
        setattr(extract(_source()), field_name, ())


def test_two_keyless_pending_subjects_keep_their_own_field_evidence() -> None:
    document = _wire(_source())
    pending = [
        row
        for row in document["records"]
        if row["kind"] == "transaction" and row["source_key"] is None
    ]
    assert [row["payload"]["payee_as_stated"] for row in pending] == [
        "Invented hold",
        "Invented pending purchase",
    ]

    def fields(subject: str) -> dict:
        return {
            row["payload"]["field_name"]: row["payload"]["text_value"]
            for row in document["records"]
            if row["kind"] == "observation"
            and row["payload"]["subject_source_record_id"] == subject
        }

    assert fields(pending[0]["source_record_id"]) == {
        "source.status": "STMTTRNP",
        "source.trntype": "HOLD",
        "source.memo": "Hold memo",
        "source.dttran": "20251231235000[-5:EST]",
        "source.dtexpire": "20260216120000[-5]",
    }
    assert fields(pending[1]["source_record_id"]) == {
        "source.status": "STMTTRNP",
        "source.trntype": "POS",
        "source.memo": "Purchase memo",
        "source.dttran": "20260215114500[-5:EST]",
    }


def test_reordering_responses_and_rows_preserves_keys_not_occurrences() -> None:
    raw = _source()
    first = raw.index(b"<STMTTRN>")
    end = raw.index(b"</STMTTRN>", first) + len(b"</STMTTRN>")
    second = raw.index(b"<STMTTRN>", end)
    second_end = raw.index(b"</STMTTRN>", second) + len(b"</STMTTRN>")
    reordered = (
        raw[:first]
        + raw[second:second_end]
        + raw[end:second]
        + raw[first:end]
        + raw[second_end:]
    )
    bank_start = reordered.index(b"<BANKMSGSRSV1>")
    bank_end = reordered.index(b"</BANKMSGSRSV1>") + len(b"</BANKMSGSRSV1>")
    card_start = reordered.index(b"<CREDITCARDMSGSRSV1>")
    card_end = reordered.index(b"</CREDITCARDMSGSRSV1>") + len(b"</CREDITCARDMSGSRSV1>")
    reordered = (
        reordered[:bank_start]
        + reordered[card_start:card_end]
        + reordered[bank_end:card_start]
        + reordered[bank_start:bank_end]
        + reordered[card_end:]
    )

    def keyed(document: dict) -> dict:
        return {
            (r["source_section"], r["source_key"]): (
                event_id(
                    EntityId("owner"),
                    Occurrence(
                        SourceKeyAnchor(
                            SemanticKey(document["source_scope_id"]),
                            SemanticKey(r["source_section"]),
                            SemanticKey(r["source_key_domain"]),
                            SemanticKey(r["source_key"]),
                        ),
                        SemanticKey(r["event_key"]),
                    ),
                ),
                r["source_record_id"],
                r["record_locator"],
            )
            for r in document["records"]
            if r["kind"] == "transaction" and r["source_key"] is not None
        }

    original, changed = keyed(_wire(raw)), keyed(_wire(reordered))
    assert original.keys() == changed.keys()
    assert all(
        original[key][0] == changed[key][0] and original[key][1] != changed[key][1]
        for key in original
    )
    assert any(original[key][2] != changed[key][2] for key in original)
    assert len({v[0] for v in original.values()}) == 5


def test_same_service_response_ordinals_do_not_choose_section_identity() -> None:
    raw = _source()
    start = raw.index(b"<STMTTRNRS>")
    end = raw.index(b"</STMTTRNRS>") + len(b"</STMTTRNRS>")
    first = raw[start:end]
    second = first.replace(b"CHECKING-1", b"CHECKING-2")
    before = _wire(raw[:start] + first + second + raw[end:])
    after = _wire(raw[:start] + second + first + raw[end:])

    def accounts(document: dict) -> dict:
        return {
            s["source_account_identifier"]: s["section_id"]
            for s in document["source_scope"]["sections"]
        }

    assert accounts(before) == accounts(after)

    def locator(document: dict) -> str:
        return next(
            r["record_locator"]
            for r in document["records"]
            if r["kind"] == "transaction"
            and r["source_account_identifier"] == "CHECKING-1"
        )

    assert "STMTTRNRS[1]" in locator(before)
    assert "STMTTRNRS[2]" in locator(after)


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("<FITID>same-b", "<FITID>same-a", "duplicate_fitid"),
        ("<FITID>same-b", "<FITID> same-b", "unsupported_identifier"),
        ("<STMTTRNP>", "<STMTTRNP><FITID>invented</FITID>", "unsupported_element"),
        ("<CORRECTACTION>REPLACE</CORRECTACTION>", "", "correction_pair_required"),
        ("<CORRECTFITID>old-b</CORRECTFITID>", "", "correction_pair_required"),
        ("<FITID>deletion</FITID>", "", "missing_element"),
        ("<TRNAMT>-0.000</TRNAMT>", "", "missing_element"),
        ("<DTPOSTED>20260109</DTPOSTED>", "", "missing_element"),
        ("<TRNTYPE>HOLD", "<TRNTYPE>DEBIT", "hold_expiration_required"),
        ("<DTTRAN>20251231235000[-5:EST]</DTTRAN>", "", "missing_element"),
        ("<ORIGCURRENCY>", "<CURRENCY/><ORIGCURRENCY>", "unsupported_element"),
        ("<CCSTMTTRNRS>", "<CCSTMTENDRS/> <CCSTMTTRNRS>", "unsupported_element"),
        (
            "<NAME>Foreign purchase",
            "<NAME><PRIVATE-SOURCE/>Foreign purchase",
            "invalid_value",
        ),
    ],
)
def test_malformed_or_unsupported_economics_fail_safely(
    old: str, new: str, code: str
) -> None:
    with pytest.raises(OfxError) as error:
        extract(_source().replace(old.encode(), new.encode()))
    assert error.value.code == code
    assert "PRIVATE-SOURCE" not in str(error.value)
    assert error.value.location


@pytest.mark.parametrize(
    ("token", "local", "offset", "zone"),
    [
        ("20260101", None, None, None),
        ("20260101235000", "23:50:00", 0, None),
        ("20260101235000[-5]", "23:50:00", -18000, None),
        ("20260101235000.000[5.50:IST]", "23:50:00.000", 19800, "IST"),
        ("20260101235000.124[+12:ZONE]", "23:50:00.124", 43200, "ZONE"),
        ("20260101235000[+5.01]", "23:50:00", 18036, None),
        ("20260101235000[-5.01:ZONE]", "23:50:00", -18036, "ZONE"),
        ("20260101235000.124[-0.01]", "23:50:00.124", -36, None),
        ("20260101235000[5.50:Asia/Calcutta]", "23:50:00", 19800, "Asia/Calcutta"),
        ("20260101235060[0:GMT]", "23:50:60", 0, "GMT"),
    ],
)
def test_primary_datetime_forms_preserve_stated_precision(
    token: str, local: str | None, offset: int | None, zone: str | None
) -> None:
    document = extract(
        _source().replace(b"20260112</DTPOSTED>", token.encode() + b"</DTPOSTED>")
    )
    source_time = document.sections[0].transactions[0].times[0][1]
    assert source_time.raw == token
    time = source_time.observation
    assert (time.local, time.offset_seconds, time.zone) == (local, offset, zone)


@pytest.mark.parametrize(
    ("token", "code"),
    [
        ("20260101235000[13]", "invalid_offset"),
        ("20260101235000[-12.01]", "invalid_offset"),
        ("20260230", "invalid_datetime"),
        ("01000229", "invalid_datetime"),
        ("20260101235061", "invalid_datetime"),
        ("20260101246000", "invalid_datetime"),
        ("202601012350", "unsupported_datetime"),
        ("20260101235000.12", "unsupported_datetime"),
    ],
)
def test_invalid_datetimes_are_explicit(token: str, code: str) -> None:
    with pytest.raises(OfxError, match=code):
        extract(
            _source().replace(b"20260112</DTPOSTED>", token.encode() + b"</DTPOSTED>")
        )


def test_year_zero_is_retained_in_time_and_source_date_projections() -> None:
    document = _wire(
        _source().replace(
            b"20260112</DTPOSTED>", b"00000229235060.124[5.01]</DTPOSTED>"
        )
    )
    transaction = next(r for r in document["records"] if r["kind"] == "transaction")
    assert transaction["payload"]["date_posted"] == "0000-02-29"
    assert transaction["payload"]["legs"][0]["date_posted"] == "0000-02-29"
    assert transaction["payload"]["times"] == [
        {
            "date_role": "posted",
            "timestamp_date": "0000-02-29",
            "timestamp_local": "23:50:60.124",
            "timestamp_offset_seconds": 18036,
            "timestamp_zone": None,
            "timestamp_precision": "fractional_second",
            "timestamp_fraction_digits": 3,
        }
    ]
    raw = next(
        r["payload"]["text_value"]
        for r in document["records"]
        if r["payload"].get("field_name") == "source.dtposted"
    )
    assert raw == "00000229235060.124[5.01]"


@pytest.mark.parametrize(
    ("token", "coefficient", "scale", "source_scale"),
    [
        ("-0.000000000000000000", "0", 0, 18),
        ("1.234567890123456789", "1234567890123456789", 18, 18),
        ("9" * 96, "9" * 96, 0, 0),
        ("0." + "0" * 37 + "1", "1", 38, 38),
    ],
)
def test_exact_numeric_capacity(
    token: str, coefficient: str, scale: int, source_scale: int
) -> None:
    document = extract(_source().replace(b"20.00", token.encode(), 1))
    amount = document.sections[0].transactions[0].amount
    assert (amount.coefficient, amount.scale, amount.source_scale) == (
        coefficient,
        scale,
        source_scale,
    )


@pytest.mark.parametrize("token", ["9" * 97, "0." + "0" * 38 + "1", "NaN", "+1", " 1"])
def test_numeric_capacity_or_syntax_rejected(token: str) -> None:
    with pytest.raises(OfxError, match="invalid_amount"):
        extract(_source().replace(b"20.00", token.encode(), 1))


@pytest.mark.parametrize(
    "markup",
    [
        b"<!DOCTYPE OFX [<!ENTITY x SYSTEM 'file:///not-read'>]>",
        b"<!-- hidden -->",
        b"<?other value?>",
    ],
)
def test_forbidden_markup_rejected_before_parser(markup: bytes) -> None:
    with pytest.raises(OfxError, match="forbidden_markup"):
        extract(_source().replace(b"<OFX>", markup + b"<OFX>"))


def test_encoding_and_unknown_names_are_not_revealed() -> None:
    with pytest.raises(OfxError, match="unsupported_encoding"):
        extract(_source().replace(b"Foreign purchase", b"\xff"))
    with pytest.raises(OfxError) as error:
        extract(
            _source().replace(b"<OFX>", b'<OFX><x:SECRET xmlns:x="private-value"/>')
        )
    assert str(error.value) == "unsupported_element: OFX"


@pytest.mark.parametrize(
    ("body", "code", "allocated"),
    [
        (b"<OFX>" + b"<X>" * MAX_DEPTH, "depth_limit", MAX_DEPTH),
        (
            b"<OFX>"
            + b"<STMTTRN/>" * (MAX_ROWS // 2)
            + b"<STMTTRNP/>" * (MAX_ROWS // 2)
            + b"<STMTTRNP>",
            "row_limit",
            MAX_ROWS + 1,
        ),
        (b"<OFX>" + b"<X/>" * (MAX_NODES - 1) + b"<X>", "node_limit", MAX_NODES),
    ],
)
def test_parser_limits_before_excess_allocation(
    body: bytes, code: str, allocated: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = []

    class RecordingBuilder(ofx._GuardedTreeBuilder):  # noqa: SLF001
        def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
            node = super().start(tag, attrs)
            built.append(tag)
            return node

    monkeypatch.setattr(ofx, "_GuardedTreeBuilder", RecordingBuilder)
    with pytest.raises(OfxError, match=code):
        extract(_source().split(b"<OFX>", 1)[0] + body)
    assert len(built) == allocated


@pytest.mark.parametrize(
    "body",
    [
        b"<OFX>" + b"<X>" * (MAX_DEPTH - 1) + b"</X>" * (MAX_DEPTH - 1) + b"</OFX>",
        b"<OFX>" + b"<X/>" * (MAX_NODES - 1) + b"</OFX>",
    ],
)
def test_exact_structural_budgets_reach_profile_validation(body: bytes) -> None:
    with pytest.raises(OfxError, match="unsupported_element"):
        extract(_source().split(b"<OFX>", 1)[0] + body)


def test_exact_rows_and_bytes_admitted() -> None:
    raw = _source("prior")
    row = (
        b"<STMTTRN><TRNTYPE>OTHER</TRNTYPE><DTPOSTED>20260112</DTPOSTED>"
        b"<TRNAMT>0</TRNAMT><FITID>row-%d</FITID></STMTTRN>"
    )
    raw = raw.replace(
        b"</BANKTRANLIST>",
        b"".join(row % i for i in range(MAX_ROWS)) + b"</BANKTRANLIST>",
    )
    raw += b" " * (MAX_BYTES - len(raw))
    assert len(extract(raw).sections[0].transactions) == MAX_ROWS
    with pytest.raises(OfxError, match="size_limit"):
        extract(raw + b" ")


def test_cash_equation_retains_independent_points() -> None:
    prior, main = (_wire(_source(name)) for name in ("prior", "main"))
    amounts = [
        r["payload"]["legs"][0]["amount"]["coefficient"]
        for r in main["records"]
        if r["kind"] == "transaction"
    ]
    assert amounts == ["20", "-5"]

    def balance(document: dict) -> str:
        return next(r for r in document["records"] if r["kind"] == "assertion")[
            "payload"
        ]["observations"][0]["amount"]["coefficient"]

    assert (balance(prior), balance(main)) == ("100", "115")
    assert 100 + 20 - 5 == 115
