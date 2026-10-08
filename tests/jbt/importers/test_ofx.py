import xml.etree.ElementTree as ET
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from jbt.contracts.schemas import extraction_schema, validate_document
from jbt.domain.errors import IdentityError
from jbt.importers import ofx
from jbt.importers.ofx import (
    BALANCE_LOCATOR,
    MAX_BYTES,
    MAX_DEPTH,
    MAX_NODES,
    MAX_ROWS,
    OfxContext,
    OfxError,
    extract,
    parse_boundary,
    source_anchor,
    source_record_id,
    to_extraction,
)

FIXTURES = Path(__file__).parent / "fixtures_synthetic"


def _source(name: str = "main") -> bytes:
    return (FIXTURES / f"{name}.ofx").read_bytes()


def _replace(old: str, new: str, source: bytes | None = None) -> bytes:
    raw = source if source is not None else _source()
    assert raw.count(old.encode()) == 1
    return raw.replace(old.encode(), new.encode())


def test_independent_point_balance_and_statement_observations() -> None:
    main = extract(_source())
    prior = extract(_source("prior"))
    assert (main.bank_id, main.account_id, main.account_type, main.currency) == (
        "TESTBANK",
        "INVENTED-1",
        "CHECKING",
        "USD",
    )
    assert (main.start, main.end, main.balance_date) == (
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 2, 1),
    )
    assert (main.start_raw, main.end_raw, main.balance_date_raw) == (
        "20260101000000[0:GMT]",
        "20260201000000[0:GMT]",
        "20260201000000[0:GMT]",
    )
    assert main.start_instant == datetime(2026, 1, 1, tzinfo=UTC)
    assert main.end_instant == datetime(2026, 2, 1, tzinfo=UTC)
    assert main.balance_instant == main.end_instant
    assert (prior.balance.coefficient, prior.balance.source_scale) == ("100", 2)
    assert (prior.start, prior.end) == (date(2025, 12, 31), date(2026, 1, 1))
    assert prior.balance_date == date(2026, 1, 1)
    assert prior.balance_date_raw == "20260101000000[0:GMT]"
    assert prior.balance_instant == main.start_instant
    assert main.balance_date == main.end
    assert prior.transactions == ()
    assert [
        (r.external_id, r.date, r.amount.coefficient, r.amount.source_scale)
        for r in main.transactions
    ] == [
        ("credit", date(2026, 1, 12), "20", 2),
        ("debit", date(2026, 1, 20), "-5", 2),
    ]
    assert main.balance.coefficient == "115"
    assert [row.date_raw for row in main.transactions] == ["20260112", "20260120"]
    assert 100 + 20 - 5 == 115
    assert main.transactions[0].locator != main.transactions[1].locator
    field = "name"
    with pytest.raises(FrozenInstanceError):
        setattr(main.transactions[0], field, "changed")


@pytest.mark.parametrize("name", ["main", "prior"])
def test_wire_is_closed_schema_point_evidence_not_complete_period(name: str) -> None:
    raw = _source(name)
    statement = extract(raw)
    context = OfxContext(f"synthetic-{name}", sha256(raw).hexdigest(), "ofx-xml-220")
    document = to_extraction(statement, context)
    validate_document(document, extraction_schema())
    assert document["source_scope"]["completeness"] == "unstated"
    assert document["source_scope"]["source_class"] == "statement"
    assert document["source_scope"]["source_accounts"] == ["INVENTED-1"]
    assert document["records"][-1]["payload"]["assertion_kind"] == "point"
    assert document["records"][-1]["payload"]["is_complete"] is False
    assert document["records"][-1]["payload"]["date"] == (
        "2026-01-01" if name == "prior" else "2026-02-01"
    )
    assert (
        document["records"][-1]["payload"]["observations"][0]["amount"]["source_scale"]
        == 2
    )
    assert len([r for r in document["records"] if r["kind"] == "transaction"]) == (
        2 if name == "main" else 0
    )
    for record in document["records"]:
        anchor = source_anchor(
            context, record["source_section"], record["record_locator"]
        )
        assert anchor.blob_digest == context.source_blob_digest
        assert anchor.namespace.value == context.source_scope_id
        assert anchor.locator.value.startswith("ofx:")
        assert record["source_record_id"] == source_record_id(anchor)
        assert len(record["source_record_id"]) == 64
    assert document["records"][-1]["record_locator"] == BALANCE_LOCATOR
    assert document["records"][-1]["payload"]["timestamp_local"] == "00:00:00"
    assert document["records"][-1]["payload"]["timestamp_offset_minutes"] == 0
    assert document["records"][-1]["payload"]["timestamp_zone"] == "GMT"
    assert document["records"][-1]["payload"]["timestamp_precision"] == "second"
    interval = document["records"][-5:-3]
    assert [item["payload"]["field_name"] for item in interval] == [
        "source.dtstart",
        "source.dtend",
    ]
    assert [item["payload"]["text_value"] for item in interval] == [
        statement.start_raw,
        statement.end_raw,
    ]
    assert all(item["event_key"] == "interval" for item in interval)
    assert document["records"][-3]["payload"]["field_name"] == "source.bankid"
    assert document["records"][-3]["payload"]["text_value"] == "TESTBANK"
    assert document["records"][-2]["payload"]["field_name"] == "source.accttype"


def test_main_wire_source_field_provenance() -> None:
    raw = _source()
    document = to_extraction(
        extract(raw), OfxContext("main", sha256(raw).hexdigest(), "ofx-xml-220")
    )
    transaction = document["records"][0]
    assert transaction["event_key"] == "transaction"
    assert transaction["payload"]["status_token"] is None
    assert transaction["external_record_id"] == "credit"
    assert transaction["source_account_identifier"] == "INVENTED-1"
    assert transaction["payload"]["date_posted"] == "2026-01-12"
    assert transaction["payload"]["description_as_stated"] == "Invented income"
    assert transaction["payload"]["payee_as_stated"] == "Invented income"
    assert transaction["payload"]["times"][0]["timestamp_offset_minutes"] is None
    assert transaction["payload"]["times"][0]["timestamp_zone"] is None
    assert transaction["payload"]["legs"][0]["amount"]["source_scale"] == 2
    assert document["records"][1]["payload"]["field_name"] == "source.trntype"
    assert document["records"][1]["payload"]["text_value"] == "CREDIT"
    assert document["records"][1]["event_key"] == "transaction"
    assert document["records"][1]["external_record_id"] == "credit"
    assert document["records"][2]["payload"]["field_name"] == "source.memo"
    assert document["records"][2]["payload"]["text_value"] == "Synthetic deposit"
    assert document["records"][2]["event_key"] == "transaction"
    assert document["records"][2]["external_record_id"] == "credit"
    assert document["records"][3]["external_record_id"] == "debit"
    assert document["records"][3]["payload"]["description_as_stated"] == (
        "Invented expense"
    )
    assert not any(
        record["payload"].get("field_name") == "source.memo"
        and record["record_locator"].endswith("STMTTRN[2]/MEMO")
        for record in document["records"]
    )


def test_date_only_balance_keeps_unknown_wire_instant() -> None:
    prior_date_only = _replace(
        "<DTASOF>20260101000000[0:GMT]",
        "<DTASOF>20251231",
        _source("prior"),
    )
    statement = extract(prior_date_only)
    assert statement.balance_date == date(2025, 12, 31)
    assert statement.balance_date_raw == "20251231"
    assert statement.balance_instant is None
    context = OfxContext("prior-date-only", sha256(prior_date_only).hexdigest(), "ofx")
    wire = to_extraction(statement, context)
    validate_document(wire, extraction_schema())
    assertion = wire["records"][-1]["payload"]
    assert assertion["date"] == "2025-12-31"
    assert assertion["timestamp_precision"] == "date"
    assert assertion["timestamp_local"] is None
    assert assertion["timestamp_offset_minutes"] is None
    assert assertion["timestamp_zone"] is None


def test_public_boundary_decoder_matches_extraction_and_rejects_other_forms() -> None:
    statement = extract(_source())
    assert parse_boundary(statement.start_raw) == (
        statement.start,
        statement.start_instant,
    )
    assert parse_boundary(statement.end_raw) == (
        statement.end,
        statement.end_instant,
    )
    assert parse_boundary(statement.balance_date_raw) == (
        statement.balance_date,
        statement.balance_instant,
    )
    assert parse_boundary("20260101") == (date(2026, 1, 1), None)
    for token, code in (
        ("2026-01-01", "unsupported_boundary_timestamp"),
        ("20260101010000[0:GMT]", "unsupported_boundary_timestamp"),
        ("20260101000000[0:UTC]", "unsupported_boundary_timestamp"),
        ("20260230", "invalid_date"),
    ):
        with pytest.raises(OfxError) as error:
            parse_boundary(token)
        assert error.value.code == code
        assert error.value.location == "OFX/boundary"


def test_anchor_identity_is_scoped_and_independent_of_financial_values() -> None:
    main = extract(_source())
    digest = sha256(_source()).hexdigest()
    context = OfxContext("main", digest, "ofx-xml-220")
    first = main.transactions[0]
    anchor = source_anchor(context, "BANKTRANLIST", first.locator)
    assert anchor.section.value == "BANKTRANLIST"
    assert (
        source_record_id(anchor)
        == to_extraction(main, context)["records"][0]["source_record_id"]
    )
    assert source_record_id(anchor) != source_record_id(
        source_anchor(context, "BANKTRANLIST", main.transactions[1].locator)
    )
    assert source_record_id(anchor) != source_record_id(
        source_anchor(
            OfxContext("other", digest, "ofx-xml-220"), "BANKTRANLIST", first.locator
        )
    )
    assert source_record_id(anchor) != source_record_id(
        source_anchor(
            OfxContext("main", "a" * 64, "ofx-xml-220"), "BANKTRANLIST", first.locator
        )
    )
    assert source_record_id(anchor) != source_record_id(
        source_anchor(context, "LEDGERBAL", first.locator)
    )
    with pytest.raises(IdentityError, match="sha256_digest_required"):
        OfxContext("main", "sha256:synthetic", "ofx-xml-220")


def test_vary_values_dates_and_row_count_without_fixture_matching() -> None:
    raw = _replace("20.00", "41.300")
    raw = _replace("20260112", "20260113", raw)
    raw = _replace("Invented income", "Another label", raw)
    raw = _replace("115.00", "136.300", raw)
    raw = raw.replace(
        b"          </STMTTRN>\n        </BANKTRANLIST>",
        b"          </STMTTRN>\n"
        b"          <STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260125</DTPOSTED>"
        b"<TRNAMT>-1.00</TRNAMT><FITID>third</FITID><NAME>Extra</NAME>"
        b"</STMTTRN>\n        </BANKTRANLIST>",
    )
    result = extract(raw)
    assert len(result.transactions) == 3
    assert result.transactions[0].amount.coefficient == "413"
    assert result.transactions[0].amount.source_scale == 3
    assert result.transactions[0].date == date(2026, 1, 13)
    assert result.transactions[0].date_raw == "20260113"
    assert result.transactions[0].name == "Another label"
    assert result.balance.coefficient == "1363"
    assert result.transactions[-1].external_id == "third"


def test_outside_search_interval_posted_date_is_retained() -> None:
    result = extract(_replace("20260112", "20251230"))
    assert result.transactions[0].date < result.start


def test_wire_preserves_source_order_and_ordinals_not_chronology() -> None:
    raw = _replace("20260112", "20260121")
    document = to_extraction(
        extract(raw), OfxContext("main", sha256(raw).hexdigest(), "ofx-xml-220")
    )
    assert [
        (
            record["external_record_id"],
            record["payload"]["date_posted"],
            record["record_locator"].rsplit("/", 1)[-1],
        )
        for record in document["records"]
        if record["kind"] == "transaction"
    ] == [
        ("credit", "2026-01-21", "STMTTRN[1]"),
        ("debit", "2026-01-20", "STMTTRN[2]"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("TRNAMT", "20.00"), ("FITID", "credit"), ("BALAMT", "115.00")],
)
def test_missing_financial_fields_are_not_defaulted(field: str, value: str) -> None:
    with pytest.raises(OfxError) as error:
        extract(_replace(f"<{field}>{value}</{field}>", ""))
    assert error.value.code == "missing_element"
    assert error.value.location.endswith(f"/{field}")


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ('VERSION="220"', 'VERSION="102"', "unsupported_header"),
        ("<TRNTYPE>CREDIT", "<TRNTYPE>PENDING", "unsupported_transaction_type"),
        ("<TRNTYPE>CREDIT", "<TRNTYPE>DEBIT", "inconsistent_sign"),
        ("<FITID>debit", "<FITID>credit", "duplicate_fitid"),
        ("<TRNAMT>20.00", "<TRNAMT>NaN", "invalid_amount"),
        ("<DTPOSTED>20260112", "<DTPOSTED>20261312", "invalid_date"),
        ("<DTPOSTED>20260112", "<DTPOSTED>20260112120000", "unsupported_date"),
        (
            "<DTASOF>20260201000000[0:GMT]",
            "<DTASOF>20260201010000[0:GMT]",
            "unsupported_boundary_timestamp",
        ),
        (
            "<DTASOF>20260201000000[0:GMT]",
            "<DTASOF>20260201000000[-5:EST]",
            "unsupported_boundary_timestamp",
        ),
        ("<CURDEF>USD", "<CURDEF>usd", "unsupported_currency"),
        ("<BALAMT>115.00", "<BALAMT>1e4", "invalid_amount"),
        ("<BALAMT>115.00", "<BALAMT>" + "1" * 33, "amount_limit"),
        (
            "<TRNTYPE>CREDIT",
            "<CORRECTFITID>old</CORRECTFITID><TRNTYPE>CREDIT",
            "unsupported_element",
        ),
        ("<BANKTRANLIST>", "<BANKTRANLISTP/><BANKTRANLIST>", "unsupported_element"),
        ("<LEDGERBAL>", "<AVAILBAL/><LEDGERBAL>", "unsupported_element"),
        (
            "<NAME>Invented income</NAME>",
            "<NAME>Invented income</NAME><SIC>1234</SIC>",
            "unsupported_element",
        ),
        ("<FITID>credit", "<FITID>test credit 1", "unsupported_identifier"),
        ("<BANKID>TESTBANK", "<BANKID>TESTBANK<OTHER>another</OTHER>", "invalid_value"),
        ("<BANKMSGSRSV1>", "<BANKMSGSRSV1><STMTTRNRS/>", "duplicate_element"),
        ("<TRNAMT>20.00", "<TRNAMT>20.00<TRNAMT>2.00</TRNAMT>", "invalid_value"),
    ],
)
def test_reject_ambiguous_or_unsupported_economic_content(
    old: str, new: str, code: str
) -> None:
    with pytest.raises(OfxError) as error:
        extract(_replace(old, new))
    assert error.value.code == code
    assert error.value.location


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (
            b"<!DOCTYPE OFX [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>",
            "forbidden_markup",
        ),
        (b"<!-- hidden -->", "forbidden_markup"),
        (b"<?unknown value?>", "forbidden_markup"),
        (b"<STMTTRNRS/>", "duplicate_element"),
    ],
)
def test_reject_markup_and_extra_responses(raw: bytes, code: str) -> None:
    source = _source()
    if raw.startswith(b"<STMTTRNRS"):
        source = source.replace(b"<STMTTRNRS>", raw + b"<STMTTRNRS>", 1)
    else:
        source = source.replace(b"<OFX>", raw + b"<OFX>", 1)
    with pytest.raises(OfxError) as error:
        extract(source)
    assert error.value.code == code


def test_bound_input_bytes_rows_and_depth() -> None:
    with pytest.raises(OfxError, match="size_limit"):
        extract(b"x" * 1_048_577)
    row = (
        b"<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20260112</DTPOSTED>"
        b"<TRNAMT>1.00</TRNAMT><FITID>extra-%d</FITID><NAME>Extra</NAME></STMTTRN>"
    )
    many = _source().replace(
        b"</BANKTRANLIST>",
        b"".join(row % i for i in range(500)) + b"</BANKTRANLIST>",
    )
    with pytest.raises(OfxError, match="row_limit"):
        extract(many)
    deep = _source().replace(
        b"<NAME>Invented income</NAME>",
        b"<NAME>" + b"<X>" * 15 + b"Invented income" + b"</X>" * 15 + b"</NAME>",
    )
    with pytest.raises(OfxError, match="depth_limit"):
        extract(deep)


@pytest.mark.parametrize(
    ("old", "new", "code", "location"),
    [
        (
            b"<OFX>",
            b'<OFX><bad:SECRET xmlns:bad="secret&#10;injected"/>',
            "unsupported_element",
            "OFX",
        ),
        (
            b"<BANKTRANLIST>",
            b'<BANKTRANLIST><bad:SECRET xmlns:bad="secret&#10;injected"/>',
            "unsupported_element",
            "BANKTRANLIST",
        ),
        (
            b"<OFX>",
            b'<OFX xmlns="secret&#10;injected">',
            "unsupported_root",
            "OFX",
        ),
    ],
)
def test_diagnostics_never_include_unknown_names_or_namespaces(
    old: bytes, new: bytes, code: str, location: str
) -> None:
    with pytest.raises(OfxError) as error:
        extract(_source().replace(old, new))
    assert error.value.code == code
    assert error.value.location == location
    assert str(error.value) == f"{code}: {location}"


@pytest.mark.parametrize(
    ("body", "code", "allocated"),
    [
        (b"<OFX>" + b"<X>" * MAX_DEPTH, "depth_limit", MAX_DEPTH),
        (
            b"<OFX>" + b"<STMTTRN/>" * MAX_ROWS + b"<STMTTRN>",
            "row_limit",
            MAX_ROWS + 1,
        ),
        (
            b"<OFX>" + b"<X/>" * (MAX_NODES - 1) + b"<X>",
            "node_limit",
            MAX_NODES,
        ),
    ],
)
def test_parser_rejects_budget_before_allocating_excess_node(
    body: bytes, code: str, allocated: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []

    class RecordingBuilder(ofx._GuardedTreeBuilder):  # noqa: SLF001
        def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
            node = super().start(tag, attrs)
            built.append(tag)
            return node

    monkeypatch.setattr(ofx, "_GuardedTreeBuilder", RecordingBuilder)
    header = _source().split(b"<OFX>", 1)[0]
    # The unfinished document must hit its resource limit before syntax errors.
    with pytest.raises(OfxError) as error:
        extract(header + body)
    assert error.value.code == code
    assert len(built) == allocated


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            b"<OFX>" + b"<X>" * (MAX_DEPTH - 1) + b"</X>" * (MAX_DEPTH - 1) + b"</OFX>",
            "unsupported_element",
        ),
        (b"<OFX>" + b"<X/>" * (MAX_NODES - 1) + b"</OFX>", "unsupported_element"),
    ],
)
def test_inclusive_parser_budgets_reach_profile_validation(
    body: bytes, code: str
) -> None:
    header = _source().split(b"<OFX>", 1)[0]
    with pytest.raises(OfxError) as error:
        extract(header + body)
    assert error.value.code == code


def test_maximum_supported_profile_fits_node_and_byte_budgets() -> None:
    row = (
        b"<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20260112</DTPOSTED>"
        b"<TRNAMT>1.00</TRNAMT><FITID>row-%d</FITID><NAME>Extra</NAME>"
        b"<MEMO>Memo</MEMO></STMTTRN>"
    )
    signon = (
        b"<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY>"
        b"<MESSAGE>OK</MESSAGE></STATUS><DTSERVER>20260101</DTSERVER>"
        b"<LANGUAGE>ENG</LANGUAGE><FI><ORG>Test</ORG><FID>Test</FID></FI>"
        b"</SONRS></SIGNONMSGSRSV1>"
    )
    source = _source("prior").replace(b"</STATUS>", b"<MESSAGE>OK</MESSAGE></STATUS>")
    source = source.replace(b"<OFX>", b"<OFX>" + signon)
    source = source.replace(
        b"</BANKTRANLIST>",
        b"".join(row % index for index in range(MAX_ROWS)) + b"</BANKTRANLIST>",
    )
    parser = ET.XMLParser(target=ofx._GuardedTreeBuilder())  # noqa: S314, SLF001
    tree = ET.fromstring(  # noqa: S314
        b"<OFX>" + source.split(b"<OFX>", 1)[1], parser=parser
    )
    assert sum(1 for _ in tree.iter()) == 31 + 7 * MAX_ROWS == 3531
    assert MAX_NODES > 3531
    source += b" " * (MAX_BYTES - len(source))
    assert len(source) == MAX_BYTES
    statement = extract(source)
    assert len(statement.transactions) == MAX_ROWS
    assert all(row.memo == "Memo" for row in statement.transactions)


def test_reject_non_utf8_and_noncanonical_text() -> None:
    with pytest.raises(OfxError, match="unsupported_encoding"):
        extract(_source().replace(b"Invented income", b"\xff"))
    with pytest.raises(OfxError, match="unsupported_text"):
        extract(_source().replace(b"Invented income", "Cafe\u0301".encode()))


def test_signon_control_allowlist_and_malformed_xml() -> None:
    signon = (
        b"<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE>"
        b"<SEVERITY>INFO</SEVERITY></STATUS><LANGUAGE>ENG</LANGUAGE>"
        b"</SONRS></SIGNONMSGSRSV1>"
    )
    source = _source().replace(b"<OFX>", b"<OFX>" + signon)
    assert extract(source).transactions == extract(_source()).transactions
    with pytest.raises(OfxError, match="unsupported_element"):
        extract(source.replace(b"<LANGUAGE>", b"<BALAMT>1</BALAMT><LANGUAGE>"))
    with pytest.raises(OfxError, match="signon_failure"):
        extract(source.replace(b"<CODE>0", b"<CODE>2000"))
    with pytest.raises(OfxError, match="malformed_xml"):
        extract(_source().replace(b"</OFX>", b""))
