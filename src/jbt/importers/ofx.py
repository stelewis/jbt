"""Strict OFX XML bank-statement observations, without financial booking."""

# ruff: noqa: EM101

import re
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from unicodedata import is_normalized

from jbt.domain.canonical import digest_record
from jbt.domain.errors import NumericLimitError, NumericSyntaxError
from jbt.domain.identity import AcquiredAnchor
from jbt.domain.ids import SemanticKey, validate_digest
from jbt.domain.numbers import ExactDecimal

_HEADER = re.compile(
    rb'\s*<\?xml version="1\.0" encoding="UTF-8"\?>\s*'
    rb'<\?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" '
    rb'OLDFILEUID="NONE" NEWFILEUID="NONE"\?>\s*'
)
_DATE = re.compile(r"[0-9]{8}\Z")
_MIDNIGHT_GMT = re.compile(r"[0-9]{8}000000\[0:GMT\]\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
MAX_BYTES = 1_048_576
MAX_DEPTH = 12
MAX_ROWS = 500
MAX_NODES = 8192
MAX_AMOUNT_CHARS = 32
BALANCE_LOCATOR = "ofx:OFX/BANKMSGSRSV1/STMTTRNRS/STMTRS/LEDGERBAL"
ACCOUNT_TYPE_LOCATOR = "ofx:OFX/BANKMSGSRSV1/STMTTRNRS/STMTRS/BANKACCTFROM/ACCTTYPE"
BANK_ID_LOCATOR = "ofx:OFX/BANKMSGSRSV1/STMTTRNRS/STMTRS/BANKACCTFROM/BANKID"
_TRANSACTION_LOCATOR = "ofx:OFX/BANKMSGSRSV1/STMTTRNRS/STMTRS/BANKTRANLIST/STMTTRN"
_INTERVAL_LOCATOR = "ofx:OFX/BANKMSGSRSV1/STMTTRNRS/STMTRS/BANKTRANLIST"


class OfxError(ValueError):
    """Rejected source profile, with a safe structural location."""

    def __init__(self, code: str, location: str) -> None:
        """Keep error diagnostics structural and free of source values."""
        self.code = code
        self.location = location
        super().__init__(f"{code}: {location}")


@dataclass(frozen=True, slots=True)
class OfxTransaction:
    """One source-posted cash movement; no inferred counterparty or booking."""

    external_id: str
    date: date
    date_raw: str
    amount: ExactDecimal
    trntype: str
    name: str
    memo: str | None
    locator: str


@dataclass(frozen=True, slots=True)
class OfxStatement:
    """A transaction-list interval and a separate point-in-time ledger balance."""

    bank_id: str
    account_id: str
    account_type: str
    currency: str
    start: date
    start_raw: str
    start_instant: datetime | None
    end: date
    end_raw: str
    end_instant: datetime | None
    balance_date: date
    balance_date_raw: str
    balance_instant: datetime | None
    balance: ExactDecimal
    transactions: tuple[OfxTransaction, ...]


@dataclass(frozen=True, slots=True)
class OfxContext:
    """Validated caller-owned identity of the retained source."""

    source_scope_id: str
    source_blob_digest: str
    importer_id: str

    def __post_init__(self) -> None:
        """Require a real retained digest and canonical source namespace."""
        SemanticKey(self.source_scope_id)
        validate_digest(self.source_blob_digest, "ofx.source_blob_digest")
        SemanticKey(self.importer_id)


def source_anchor(context: OfxContext, section: str, locator: str) -> AcquiredAnchor:
    """Build the acquired source anchor shared by extraction and cash modeling."""
    return AcquiredAnchor(
        context.source_blob_digest,
        SemanticKey(context.source_scope_id),
        SemanticKey(section),
        SemanticKey(locator),
    )


def source_record_id(anchor: AcquiredAnchor) -> str:
    """Name a source record from the complete canonical acquired anchor."""
    return digest_record(
        "source-record",
        (
            ("blob_digest", anchor.blob_digest),
            ("namespace", anchor.namespace.value),
            ("section", anchor.section.value),
            ("locator", anchor.locator.value),
        ),
    )


def _children(
    node: ET.Element | None, allowed: set[str], location: str
) -> dict[str, ET.Element]:
    if node is None:
        raise OfxError("missing_element", location)
    if node.attrib or (node.text or "").strip():
        raise OfxError("unexpected_content", location)
    found: dict[str, ET.Element] = {}
    for child in node:
        if child.tag not in allowed:
            raise OfxError("unsupported_element", location)
        if child.tag in found:
            raise OfxError("duplicate_element", f"{location}/{child.tag}")
        if (child.tail or "").strip():
            raise OfxError("unexpected_content", location)
        found[child.tag] = child
    return found


def _value(node: ET.Element | None, location: str) -> str:
    if node is None:
        raise OfxError("missing_element", location)
    if node.attrib or len(node) or node.text is None or not node.text.strip():
        raise OfxError("invalid_value", location)
    value = node.text.strip()
    if not is_normalized("NFC", value):
        raise OfxError("unsupported_text", location)
    return value


@dataclass(frozen=True, slots=True)
class _ParsedDate:
    value: date
    raw: str
    instant: datetime | None = None


def _civil_date(text: str, location: str) -> date:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as error:
        raise OfxError("invalid_date", location) from error


def _date(node: ET.Element | None, location: str) -> _ParsedDate:
    text = _value(node, location)
    if not _DATE.fullmatch(text):
        raise OfxError("unsupported_date", location)
    return _ParsedDate(_civil_date(text, location), text)


def _parse_boundary(token: str, location: str) -> _ParsedDate:
    if not isinstance(token, str):
        raise OfxError("unsupported_boundary_timestamp", location)
    if _DATE.fullmatch(token):
        return _ParsedDate(_civil_date(token, location), token)
    if not _MIDNIGHT_GMT.fullmatch(token):
        raise OfxError("unsupported_boundary_timestamp", location)
    civil = _civil_date(token, location)
    return _ParsedDate(civil, token, datetime.combine(civil, time.min, tzinfo=UTC))


def parse_boundary(token: str) -> tuple[date, datetime | None]:
    """Decode a supported OFX boundary without inventing an instant."""
    parsed = _parse_boundary(token, "OFX/boundary")
    return parsed.value, parsed.instant


def _boundary_date(node: ET.Element | None, location: str) -> _ParsedDate:
    return _parse_boundary(_value(node, location), location)


def _number(node: ET.Element | None, location: str) -> ExactDecimal:
    text = _value(node, location)
    if len(text) > MAX_AMOUNT_CHARS:
        raise OfxError("amount_limit", location)
    try:
        return ExactDecimal.parse(text)
    except (NumericLimitError, NumericSyntaxError) as error:
        raise OfxError("invalid_amount", location) from error


def _identifier(node: ET.Element | None, location: str) -> str:
    text = _value(node, location)
    if not _IDENTIFIER.fullmatch(text):
        raise OfxError("unsupported_identifier", location)
    return text


def _transaction(node: ET.Element, index: int) -> OfxTransaction:
    location = f"{_TRANSACTION_LOCATOR}[{index}]"
    fields = _children(
        node, {"TRNTYPE", "DTPOSTED", "TRNAMT", "FITID", "NAME", "MEMO"}, location
    )
    kind = _value(fields.get("TRNTYPE"), f"{location}/TRNTYPE")
    if kind not in {"CREDIT", "DEBIT"}:
        raise OfxError("unsupported_transaction_type", f"{location}/TRNTYPE")
    amount = _number(fields.get("TRNAMT"), f"{location}/TRNAMT")
    if (int(amount.coefficient) > 0) != (kind == "CREDIT") or amount.coefficient == "0":
        raise OfxError("inconsistent_sign", f"{location}/TRNAMT")
    memo = fields.get("MEMO")
    posted = _date(fields.get("DTPOSTED"), f"{location}/DTPOSTED")
    return OfxTransaction(
        external_id=_identifier(fields.get("FITID"), f"{location}/FITID"),
        date=posted.value,
        date_raw=posted.raw,
        amount=amount,
        trntype=kind,
        name=_value(fields.get("NAME"), f"{location}/NAME"),
        memo=_value(memo, f"{location}/MEMO") if memo is not None else None,
        locator=location,
    )


class _GuardedTreeBuilder(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.rows = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        depth = self.depth + 1
        if depth > MAX_DEPTH:
            raise OfxError("depth_limit", "OFX")
        if self.nodes >= MAX_NODES:
            raise OfxError("node_limit", "OFX")
        if tag == "STMTTRN":
            if self.rows >= MAX_ROWS:
                raise OfxError("row_limit", "BANKTRANLIST")
            self.rows += 1
        self.depth = depth
        self.nodes += 1
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        node = super().end(tag)
        self.depth -= 1
        return node


def extract(data: bytes) -> OfxStatement:  # noqa: C901, PLR0912, PLR0915
    """Extract a bounded XML 2.2 bank response; never fetch external inputs."""
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise OfxError("size_limit", "OFX")
    header = _HEADER.match(data)
    if header is None:
        raise OfxError("unsupported_header", "OFX")
    try:
        body = data[header.end() :].decode("utf-8")
    except UnicodeDecodeError as error:
        raise OfxError("unsupported_encoding", "OFX") from error
    # Reject DTDs and extra processing instructions before XML parsing.
    if "<!" in body or "<?" in body:
        raise OfxError("forbidden_markup", "OFX")
    try:
        parser = ET.XMLParser(target=_GuardedTreeBuilder())  # noqa: S314  # nosec B314
        root = ET.fromstring(body, parser=parser)  # noqa: S314  # nosec B314
    except ET.ParseError as error:
        raise OfxError("malformed_xml", "OFX") from error
    if root.tag != "OFX":
        raise OfxError("unsupported_root", "OFX")
    top = _children(root, {"SIGNONMSGSRSV1", "BANKMSGSRSV1"}, "OFX")
    signon = top.get("SIGNONMSGSRSV1")
    if signon is not None:
        signon_fields = _children(signon, {"SONRS"}, "OFX/SIGNONMSGSRSV1")
        sonrs = _children(
            signon_fields.get("SONRS"),
            {"STATUS", "DTSERVER", "LANGUAGE", "FI"},
            "OFX/SIGNONMSGSRSV1/SONRS",
        )
        status = _children(
            sonrs.get("STATUS"),
            {"CODE", "SEVERITY", "MESSAGE"},
            "OFX/SIGNONMSGSRSV1/SONRS/STATUS",
        )
        if (
            _value(status.get("CODE"), "SIGNON/STATUS/CODE") != "0"
            or _value(status.get("SEVERITY"), "SIGNON/STATUS/SEVERITY") != "INFO"
        ):
            raise OfxError("signon_failure", "SIGNON/STATUS")
        for key in ("MESSAGE", "DTSERVER", "LANGUAGE"):
            if key in (status if key == "MESSAGE" else sonrs):
                _value((status if key == "MESSAGE" else sonrs)[key], f"SIGNON/{key}")
        if "FI" in sonrs:
            fi = _children(sonrs["FI"], {"ORG", "FID"}, "SIGNON/FI")
            for key, item in fi.items():
                _value(item, f"SIGNON/FI/{key}")
    bank = _children(top.get("BANKMSGSRSV1"), {"STMTTRNRS"}, "OFX/BANKMSGSRSV1")
    response = _children(
        bank.get("STMTTRNRS"),
        {"TRNUID", "STATUS", "STMTRS"},
        "OFX/BANKMSGSRSV1/STMTTRNRS",
    )
    if "TRNUID" in response:
        _value(response["TRNUID"], "STMTTRNRS/TRNUID")
    if "STATUS" in response:
        status = _children(
            response["STATUS"],
            {"CODE", "SEVERITY", "MESSAGE"},
            "STMTTRNRS/STATUS",
        )
        if (
            _value(status.get("CODE"), "STMTTRNRS/STATUS/CODE") != "0"
            or _value(status.get("SEVERITY"), "STMTTRNRS/STATUS/SEVERITY") != "INFO"
        ):
            raise OfxError("response_failure", "STMTTRNRS/STATUS")
        if "MESSAGE" in status:
            _value(status["MESSAGE"], "STMTTRNRS/STATUS/MESSAGE")
    statement = _children(
        response.get("STMTRS"),
        {"CURDEF", "BANKACCTFROM", "BANKTRANLIST", "LEDGERBAL"},
        "STMTRS",
    )
    currency = _value(statement.get("CURDEF"), "STMTRS/CURDEF")
    if not _CURRENCY.fullmatch(currency):
        raise OfxError("unsupported_currency", "STMTRS/CURDEF")
    account = _children(
        statement.get("BANKACCTFROM"), {"BANKID", "ACCTID", "ACCTTYPE"}, "BANKACCTFROM"
    )
    account_type = _value(account.get("ACCTTYPE"), "BANKACCTFROM/ACCTTYPE")
    if account_type not in {"CHECKING", "SAVINGS"}:
        raise OfxError("unsupported_account_type", "BANKACCTFROM/ACCTTYPE")
    listing = statement.get("BANKTRANLIST")
    if listing is None:
        raise OfxError("missing_element", "BANKTRANLIST")
    if listing.attrib or (listing.text or "").strip():
        raise OfxError("unexpected_content", "BANKTRANLIST")
    seen: set[str] = set()
    rows: list[OfxTransaction] = []
    start: _ParsedDate | None = None
    end: _ParsedDate | None = None
    for child in listing:
        if (child.tail or "").strip():
            raise OfxError("unexpected_content", "BANKTRANLIST")
        if child.tag == "DTSTART" and start is None:
            start = _boundary_date(child, "BANKTRANLIST/DTSTART")
        elif child.tag == "DTEND" and end is None:
            end = _boundary_date(child, "BANKTRANLIST/DTEND")
        elif child.tag == "STMTTRN":
            row = _transaction(child, len(rows) + 1)
            if row.external_id in seen:
                raise OfxError("duplicate_fitid", row.locator)
            seen.add(row.external_id)
            rows.append(row)
        else:
            raise OfxError("unsupported_element", "BANKTRANLIST")
    if start is None or end is None or start.value >= end.value:
        raise OfxError("invalid_interval", "BANKTRANLIST")
    ledger = _children(statement.get("LEDGERBAL"), {"BALAMT", "DTASOF"}, "LEDGERBAL")
    balance_date = _boundary_date(ledger.get("DTASOF"), "LEDGERBAL/DTASOF")
    return OfxStatement(
        bank_id=_identifier(account.get("BANKID"), "BANKACCTFROM/BANKID"),
        account_id=_identifier(account.get("ACCTID"), "BANKACCTFROM/ACCTID"),
        account_type=account_type,
        currency=currency,
        start=start.value,
        start_raw=start.raw,
        start_instant=start.instant,
        end=end.value,
        end_raw=end.raw,
        end_instant=end.instant,
        balance_date=balance_date.value,
        balance_date_raw=balance_date.raw,
        balance_instant=balance_date.instant,
        balance=_number(ledger.get("BALAMT"), "LEDGERBAL/BALAMT"),
        transactions=tuple(rows),
    )


def to_extraction(statement: OfxStatement, context: OfxContext) -> dict:
    """Serialize typed observations at the version-1 extraction wire edge."""
    account = statement.account_id

    def decimal(value: ExactDecimal) -> dict:
        return {
            "coefficient": value.coefficient,
            "scale": value.scale,
            "source_scale": value.source_scale,
        }

    def timestamp(value: date, instant: datetime | None) -> dict:
        return {
            "timestamp_date": value.isoformat(),
            "timestamp_local": "00:00:00" if instant is not None else None,
            "timestamp_offset_minutes": 0 if instant is not None else None,
            "timestamp_zone": "GMT" if instant is not None else None,
            "timestamp_precision": "second" if instant is not None else "date",
            "timestamp_fraction_digits": None,
        }

    def source_field(
        *,
        section: str,
        locator: str,
        external_id: str | None,
        field_name: str,
        text: str,
    ) -> dict:
        return {
            "kind": "observation",
            "source_record_id": source_record_id(
                source_anchor(context, section, locator)
            ),
            "source_section": section,
            "record_locator": locator,
            "external_record_id": external_id,
            "event_key": (
                "account-type"
                if section == "BANKACCTFROM"
                else "transaction"
                if external_id is not None
                else "interval"
            ),
            "source_account_identifier": account,
            "payload": {
                "field_name": field_name,
                "observation_kind": "source_field",
                "value_type": "text",
                "decimal_value": None,
                "text_value": text,
                "date_value": None,
                "commodity_token": None,  # nosec B105
            },
        }

    records = []
    for row in statement.transactions:
        anchor = source_anchor(context, "BANKTRANLIST", row.locator)
        dates = dict.fromkeys(
            ("date_authorized", "date_traded", "date_settled", "date_value")
        )
        dates["date_posted"] = row.date.isoformat()
        leg = dict.fromkeys(
            (
                "source_position_key",
                "symbol_as_stated",
                "broker_lot_id",
                "price_per_unit",
                "price_commodity_token",
                "original_amount",
                "original_commodity_token",
                "fx_rate_as_stated",
                "fx_base_commodity_token",
                "fx_quote_commodity_token",
                "date_authorized",
                "date_traded",
                "date_settled",
                "date_value",
            )
        )
        leg.update(
            posting_key="cash",
            source_account_identifier=account,
            commodity_token=statement.currency,
            amount=decimal(row.amount),
            date_posted=row.date.isoformat(),
            posting_role="principal",
            date_authorized_mode="unknown",
            date_traded_mode="unknown",
            date_posted_mode="value",
            date_settled_mode="unknown",
            date_value_mode="unknown",
            components=[],
        )
        records.append(
            {
                "kind": "transaction",
                "source_record_id": source_record_id(anchor),
                "source_section": "BANKTRANLIST",
                "record_locator": row.locator,
                "external_record_id": row.external_id,
                "event_key": "transaction",
                "source_account_identifier": account,
                "payload": {
                    **dates,
                    "description_as_stated": row.name,
                    "payee_as_stated": row.name,
                    "status_token": None,  # nosec B105
                    "legs": [leg],
                    "times": [{"date_role": "posted", **timestamp(row.date, None)}],
                },
            }
        )
        records.append(
            source_field(
                section="BANKTRANLIST",
                locator=f"{row.locator}/TRNTYPE",
                external_id=row.external_id,
                field_name="source.trntype",
                text=row.trntype,
            )
        )
        if row.memo is not None:
            records.append(
                source_field(
                    section="BANKTRANLIST",
                    locator=f"{row.locator}/MEMO",
                    external_id=row.external_id,
                    field_name="source.memo",
                    text=row.memo,
                )
            )
    for field_name, tag, token in (
        ("source.dtstart", "DTSTART", statement.start_raw),
        ("source.dtend", "DTEND", statement.end_raw),
    ):
        records.append(
            source_field(
                section="BANKTRANLIST",
                locator=f"{_INTERVAL_LOCATOR}/{tag}",
                external_id=None,
                field_name=field_name,
                text=token,
            )
        )
    records.append(
        source_field(
            section="BANKACCTFROM",
            locator=BANK_ID_LOCATOR,
            external_id=None,
            field_name="source.bankid",
            text=statement.bank_id,
        )
    )
    records.append(
        source_field(
            section="BANKACCTFROM",
            locator=ACCOUNT_TYPE_LOCATOR,
            external_id=None,
            field_name="source.accttype",
            text=statement.account_type,
        )
    )
    records.append(
        {
            "kind": "assertion",
            "source_record_id": source_record_id(
                source_anchor(context, "LEDGERBAL", BALANCE_LOCATOR)
            ),
            "source_section": "LEDGERBAL",
            "record_locator": BALANCE_LOCATOR,
            "external_record_id": None,
            "event_key": "ledgerbal",
            "source_account_identifier": account,
            "payload": {
                **timestamp(statement.balance_date, statement.balance_instant),
                "date": statement.balance_date.isoformat(),
                "assertion_kind": "point",
                "measurement": "units",
                "is_complete": False,
                "coverage_basis": "ofx-ledgerbal-point",
                "scope_kind": "account_net",
                "quote_commodity_token": None,  # nosec B105
                "observations": [
                    {
                        "observation_key": "ledgerbal",
                        "source_position_key": None,
                        "commodity_token": statement.currency,
                        "amount": decimal(statement.balance),
                    }
                ],
            },
        }
    )
    return {
        "schema_version": 1,
        "source_scope_id": context.source_scope_id,
        "source_blob_digest": context.source_blob_digest,
        "importer_id": context.importer_id,
        "source_scope": {
            "source_class": "statement",
            "institution": None,
            "period_start": statement.start.isoformat(),
            "period_end": statement.end.isoformat(),
            "completeness": "unstated",
            "source_accounts": [account],
            "revisions": [],
        },
        "records": records,
        "unsupported_content": [],
    }
