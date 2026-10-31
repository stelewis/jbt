"""Bounded OFX bank/card observations, without financial booking."""

# ruff: noqa: EM101

import re
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass

from jbt.domain.canonical import digest_record
from jbt.domain.errors import (
    IdentityError,
    NumericLimitError,
    NumericSyntaxError,
    TemporalError,
)
from jbt.domain.identity import AcquiredAnchor, source_record_digest
from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import STORED_DIGITS, STORED_SCALE, ExactDecimal
from jbt.domain.time import Precision, SourceTimeObservation
from jbt.extraction import ExtractionContext, ExtractionError

_HEADER = re.compile(
    rb'\s*<\?xml version="1\.0" encoding="UTF-8"\?>\s*'
    rb'<\?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" '
    rb'OLDFILEUID="NONE" NEWFILEUID="NONE"\?>\s*'
)
_DATETIME = re.compile(
    r"(?P<date>[0-9]{8})(?:(?P<clock>[0-9]{6})(?:\.(?P<fraction>[0-9]{3}))?"
    r"(?: ?\[(?P<offset>[+-]?[0-9]{1,2}(?:\.[0-9]{2})?)"
    r"(?::(?P<zone>[^\[\]]+))?\])?)?\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_TYPES = {
    "CREDIT",
    "DEBIT",
    "INT",
    "DIV",
    "FEE",
    "SRVCHG",
    "DEP",
    "ATM",
    "POS",
    "XFER",
    "CHECK",
    "PAYMENT",
    "CASH",
    "DIRECTDEP",
    "DIRECTDEBIT",
    "REPEATPMT",
    "HOLD",
    "OTHER",
}
MAX_BYTES = 1_048_576
MAX_DEPTH = 12
MAX_ROWS = 500
MAX_NODES = 8192
MAX_AMOUNT_CHARS = STORED_DIGITS + STORED_SCALE + 2


class OfxError(ExtractionError):
    """A safe structural rejection; no source values in diagnostics."""


@dataclass(frozen=True, slots=True)
class OfxTime:
    """An exact token and source civil evidence, without model conversion."""

    raw: str
    observation: SourceTimeObservation


@dataclass(frozen=True, slots=True)
class OfxTransaction:
    """One posted or pending row with no inferred treatment."""

    locator: str
    external_id: str | None
    amount: ExactDecimal
    trntype: str
    name: str | None
    memo: str | None
    times: tuple[tuple[str, OfxTime], ...]
    correction: tuple[str, str] | None
    original_currency: tuple[str, ExactDecimal] | None
    fields: tuple[tuple[str, str], ...]
    pending: bool


@dataclass(frozen=True, slots=True)
class OfxBalance:
    """A separately stated amount and point time."""

    locator: str
    amount: ExactDecimal
    time: OfxTime


@dataclass(frozen=True, slots=True)
class OfxSection:
    """One stated account context and its independent evidence parts."""

    section_id: str
    locator: str
    account_context: tuple[tuple[str, str], ...]
    institution: str | None
    account_id: str
    currency: str
    start: OfxTime | None
    end: OfxTime | None
    pending_as_of: OfxTime | None
    ledger: OfxBalance
    available: OfxBalance | None
    transactions: tuple[OfxTransaction, ...]
    card: bool


@dataclass(frozen=True, slots=True)
class OfxDocument:
    """Immutable multi-account source observations."""

    sections: tuple[OfxSection, ...]


def source_anchor(
    context: ExtractionContext, section: str, locator: str
) -> AcquiredAnchor:
    """Frame a blob-specific structural occurrence."""
    return AcquiredAnchor(
        context.source_blob_digest,
        SemanticKey(context.source_scope_id),
        SemanticKey(section),
        SemanticKey(locator),
    )


def _children(
    node: ET.Element | None,
    allowed: set[str],
    location: str,
    *,
    repeated: frozenset[str] = frozenset(),
) -> dict[str, list[ET.Element]]:
    if node is None:
        raise OfxError("missing_element", location)
    if node.attrib or (node.text or "").strip():
        raise OfxError("unexpected_content", location)
    found: dict[str, list[ET.Element]] = {}
    for child in node:
        if child.tag not in allowed:
            raise OfxError("unsupported_element", location)
        if child.tag in found and child.tag not in repeated:
            raise OfxError("duplicate_element", f"{location}/{child.tag}")
        if (child.tail or "").strip():
            raise OfxError("unexpected_content", location)
        found.setdefault(child.tag, []).append(child)
    return found


def _one(fields: dict[str, list[ET.Element]], key: str) -> ET.Element | None:
    return fields[key][0] if key in fields else None


def _value(node: ET.Element | None, location: str) -> str:
    if node is None:
        raise OfxError("missing_element", location)
    if node.attrib or len(node) or node.text is None:
        raise OfxError("invalid_value", location)
    return node.text


def _identifier(node: ET.Element | None, location: str) -> str:
    text = _value(node, location)
    if not _IDENTIFIER.fullmatch(text):
        raise OfxError("unsupported_identifier", location)
    return text


def _currency(node: ET.Element | None, location: str) -> str:
    text = _value(node, location)
    if not _CURRENCY.fullmatch(text):
        raise OfxError("unsupported_currency", location)
    return text


def _number(node: ET.Element | None, location: str) -> ExactDecimal:
    text = _value(node, location)
    if len(text) > MAX_AMOUNT_CHARS:
        raise OfxError("amount_limit", location)
    try:
        return ExactDecimal.parse(text)
    except NumericLimitError, NumericSyntaxError:
        raise OfxError("invalid_amount", location) from None


def _time(node: ET.Element | None, location: str) -> OfxTime:
    raw = _value(node, location)
    match = _DATETIME.fullmatch(raw)
    if match is None:
        raise OfxError("unsupported_datetime", location)
    civil = match["date"]
    clock, fraction, offset, zone = (
        match["clock"],
        match["fraction"],
        match["offset"],
        match["zone"],
    )
    seconds = 0 if clock is not None else None
    if offset is not None:
        integral, separator, fraction_text = offset.lstrip("+-").partition(".")
        rate = ExactDecimal.parse(
            ("-" if offset.startswith("-") else "")
            + str(int(integral))
            + (separator + fraction_text if separator else "")
        )
        numerator = int(rate.coefficient) * 3600
        denominator = 10**rate.scale
        if abs(int(rate.coefficient)) > 12 * denominator:
            raise OfxError("invalid_offset", location)
        seconds = numerator // denominator
    local = (
        f"{clock[:2]}:{clock[2:4]}:{clock[4:]}" + (f".{fraction}" if fraction else "")
        if clock is not None
        else None
    )
    try:
        observation = SourceTimeObservation(
            f"{civil[:4]}-{civil[4:6]}-{civil[6:]}",
            local,
            seconds,
            zone,
            Precision.FRACTIONAL_SECOND
            if fraction
            else Precision.SECOND
            if clock
            else Precision.DATE,
            len(fraction) if fraction else None,
        )
    except ValueError, TemporalError, IdentityError:
        raise OfxError("invalid_datetime", location) from None
    return OfxTime(raw, observation)


def _transaction(node: ET.Element, locator: str, *, pending: bool) -> OfxTransaction:
    dates = (
        {"DTTRAN": "initiated", "DTEXPIRE": "expires"}
        if pending
        else {
            "DTPOSTED": "posted",
            "DTUSER": "authorized",
            "DTAVAIL": "value",
        }
    )
    info = {"CHECKNUM", "REFNUM", "SIC", "PAYEEID", "EXTDNAME"}
    allowed = {"TRNTYPE", "TRNAMT", "NAME", "MEMO", "ORIGCURRENCY", *dates, *info}
    if not pending:
        allowed |= {"FITID", "CORRECTFITID", "CORRECTACTION"}
    fields = _children(node, allowed, locator)
    kind = _value(_one(fields, "TRNTYPE"), f"{locator}/TRNTYPE")
    if kind not in _TYPES:
        raise OfxError("unsupported_transaction_type", f"{locator}/TRNTYPE")
    if "DTEXPIRE" in fields and kind != "HOLD":
        raise OfxError("hold_expiration_required", f"{locator}/DTEXPIRE")
    required_date = "DTTRAN" if pending else "DTPOSTED"
    _one_date = _time(_one(fields, required_date), f"{locator}/{required_date}")
    times = tuple(
        (
            role,
            _one_date
            if tag == required_date
            else _time(_one(fields, tag), f"{locator}/{tag}"),
        )
        for tag, role in dates.items()
        if tag in fields
    )
    correction = None
    if ("CORRECTFITID" in fields) != ("CORRECTACTION" in fields):
        raise OfxError("correction_pair_required", locator)
    if "CORRECTFITID" in fields:
        target = _identifier(_one(fields, "CORRECTFITID"), f"{locator}/CORRECTFITID")
        action = _value(_one(fields, "CORRECTACTION"), f"{locator}/CORRECTACTION")
        if action not in {"REPLACE", "DELETE"}:
            raise OfxError("unsupported_correction_action", f"{locator}/CORRECTACTION")
        correction = (target, action.lower())
    original = None
    if "ORIGCURRENCY" in fields:
        currency = _children(
            _one(fields, "ORIGCURRENCY"),
            {"CURRATE", "CURSYM"},
            f"{locator}/ORIGCURRENCY",
        )
        original = (
            _currency(_one(currency, "CURSYM"), f"{locator}/ORIGCURRENCY/CURSYM"),
            _number(_one(currency, "CURRATE"), f"{locator}/ORIGCURRENCY/CURRATE"),
        )
        if int(original[1].coefficient) <= 0:
            raise OfxError("invalid_currency_rate", f"{locator}/ORIGCURRENCY/CURRATE")
    return OfxTransaction(
        locator,
        None if pending else _identifier(_one(fields, "FITID"), f"{locator}/FITID"),
        _number(_one(fields, "TRNAMT"), f"{locator}/TRNAMT"),
        kind,
        _value(_one(fields, "NAME"), f"{locator}/NAME") if "NAME" in fields else None,
        _value(_one(fields, "MEMO"), f"{locator}/MEMO") if "MEMO" in fields else None,
        times,
        correction,
        original,
        tuple(
            (tag, _value(_one(fields, tag), f"{locator}/{tag}"))
            for tag in sorted(info & fields.keys())
        ),
        pending,
    )


def _balance(node: ET.Element | None, locator: str) -> OfxBalance:
    fields = _children(node, {"BALAMT", "DTASOF"}, locator)
    return OfxBalance(
        locator,
        _number(_one(fields, "BALAMT"), f"{locator}/BALAMT"),
        _time(_one(fields, "DTASOF"), f"{locator}/DTASOF"),
    )


def _section(
    node: ET.Element | None,
    locator: str,
    institution: tuple[tuple[str, str], ...],
    *,
    card: bool,
) -> OfxSection:
    account_tag = "CCACCTFROM" if card else "BANKACCTFROM"
    fields = _children(
        node,
        {
            "CURDEF",
            account_tag,
            "BANKTRANLIST",
            "BANKTRANLISTP",
            "LEDGERBAL",
            "AVAILBAL",
        },
        locator,
    )
    account = _children(
        _one(fields, account_tag),
        {"ACCTID", "ACCTKEY"}
        if card
        else {"BANKID", "BRANCHID", "ACCTID", "ACCTTYPE", "ACCTKEY"},
        f"{locator}/{account_tag}",
    )
    required = {"ACCTID"} if card else {"BANKID", "ACCTID", "ACCTTYPE"}
    context = tuple(
        (tag, _identifier(_one(account, tag), f"{locator}/{account_tag}/{tag}"))
        for tag in sorted(required | account.keys())
    )
    if not card and dict(context)["ACCTTYPE"] not in {
        "CHECKING",
        "SAVINGS",
        "MONEYMRKT",
        "CREDITLINE",
        "CD",
    }:
        raise OfxError("unsupported_account_type", f"{locator}/{account_tag}/ACCTTYPE")
    context = (("account_kind", "card" if card else "bank"), *institution, *context)
    section_id = "ofx-account:" + digest_record("ofx-account-section", context)
    rows = []
    start = end = snapshot = None
    for tag, row_tag, pending in (
        ("BANKTRANLIST", "STMTTRN", False),
        ("BANKTRANLISTP", "STMTTRNP", True),
    ):
        if tag not in fields:
            continue
        listing = _children(
            _one(fields, tag),
            {row_tag, "DTASOF"} if pending else {row_tag, "DTSTART", "DTEND"},
            f"{locator}/{tag}",
            repeated=frozenset({row_tag}),
        )
        if pending:
            snapshot = _time(_one(listing, "DTASOF"), f"{locator}/{tag}/DTASOF")
        else:
            start = _time(_one(listing, "DTSTART"), f"{locator}/{tag}/DTSTART")
            end = _time(_one(listing, "DTEND"), f"{locator}/{tag}/DTEND")
            if start.observation.date > end.observation.date:
                raise OfxError("invalid_interval", f"{locator}/{tag}")
        rows.extend(
            _transaction(row, f"{locator}/{tag}/{row_tag}[{index}]", pending=pending)
            for index, row in enumerate(listing.get(row_tag, []), 1)
        )
    if len({row.external_id for row in rows if not row.pending}) != sum(
        not row.pending for row in rows
    ):
        raise OfxError("duplicate_fitid", f"{locator}/BANKTRANLIST")
    if start is None and snapshot is None:
        raise OfxError("missing_element", f"{locator}/BANKTRANLIST")
    return OfxSection(
        section_id,
        locator,
        context,
        dict(institution).get("ORG") or dict(context).get("BANKID"),
        dict(context)["ACCTID"],
        _currency(_one(fields, "CURDEF"), f"{locator}/CURDEF"),
        start,
        end,
        snapshot,
        _balance(_one(fields, "LEDGERBAL"), f"{locator}/LEDGERBAL"),
        _balance(_one(fields, "AVAILBAL"), f"{locator}/AVAILBAL")
        if "AVAILBAL" in fields
        else None,
        tuple(rows),
        card,
    )


def _status(node: ET.Element | None, locator: str) -> None:
    fields = _children(node, {"CODE", "SEVERITY", "MESSAGE"}, locator)
    if (
        _value(_one(fields, "CODE"), f"{locator}/CODE") != "0"
        or _value(_one(fields, "SEVERITY"), f"{locator}/SEVERITY") != "INFO"
    ):
        raise OfxError("response_failure", locator)
    if "MESSAGE" in fields:
        _value(_one(fields, "MESSAGE"), f"{locator}/MESSAGE")


class _GuardedTreeBuilder(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = self.nodes = self.rows = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        if self.depth >= MAX_DEPTH:
            raise OfxError("depth_limit", "OFX")
        if self.nodes >= MAX_NODES:
            raise OfxError("node_limit", "OFX")
        if tag in {"STMTTRN", "STMTTRNP"}:
            if self.rows >= MAX_ROWS:
                raise OfxError("row_limit", "OFX")
            self.rows += 1
        self.depth += 1
        self.nodes += 1
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        node = super().end(tag)
        self.depth -= 1
        return node


def extract(data: bytes) -> OfxDocument:  # noqa: C901, PLR0912
    """Construct only a bounded tree and return immutable exact evidence."""
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise OfxError("size_limit", "OFX")
    header = _HEADER.match(data)
    if header is None:
        raise OfxError("unsupported_header", "OFX")
    try:
        body = data[header.end() :].decode("utf-8")
    except UnicodeDecodeError:
        raise OfxError("unsupported_encoding", "OFX") from None
    if "<!" in body or "<?" in body:
        raise OfxError("forbidden_markup", "OFX")
    try:
        root = ET.fromstring(body, parser=ET.XMLParser(target=_GuardedTreeBuilder()))  # noqa: S314  # nosec B314
    except ET.ParseError:
        raise OfxError("malformed_xml", "OFX") from None
    if root.tag != "OFX":
        raise OfxError("unsupported_root", "OFX")
    top = _children(
        root, {"SIGNONMSGSRSV1", "BANKMSGSRSV1", "CREDITCARDMSGSRSV1"}, "OFX"
    )
    institution = ()
    if "SIGNONMSGSRSV1" in top:
        signon = _children(_one(top, "SIGNONMSGSRSV1"), {"SONRS"}, "OFX/SIGNONMSGSRSV1")
        fields = _children(
            _one(signon, "SONRS"),
            {"STATUS", "DTSERVER", "LANGUAGE", "FI"},
            "SIGNON/SONRS",
        )
        _status(_one(fields, "STATUS"), "SIGNON/STATUS")
        for tag in ("DTSERVER", "LANGUAGE"):
            if tag in fields:
                _value(_one(fields, tag), f"SIGNON/{tag}")
        if "FI" in fields:
            fi = _children(_one(fields, "FI"), {"ORG", "FID"}, "SIGNON/FI")
            institution = tuple(
                (tag, _identifier(_one(fi, tag), f"SIGNON/FI/{tag}"))
                for tag in sorted(fi)
            )
    sections = []
    for group, response, statement, card in (
        ("BANKMSGSRSV1", "STMTTRNRS", "STMTRS", False),
        ("CREDITCARDMSGSRSV1", "CCSTMTTRNRS", "CCSTMTRS", True),
    ):
        if group not in top:
            continue
        messages = _children(
            _one(top, group), {response}, f"OFX/{group}", repeated=frozenset({response})
        )
        for index, item in enumerate(messages.get(response, []), 1):
            locator = f"ofx:OFX/{group}/{response}[{index}]"
            fields = _children(item, {"TRNUID", "STATUS", statement}, locator)
            if "STATUS" in fields:
                _status(_one(fields, "STATUS"), f"{locator}/STATUS")
            if "TRNUID" in fields:
                _value(_one(fields, "TRNUID"), f"{locator}/TRNUID")
            sections.append(
                _section(
                    _one(fields, statement),
                    f"{locator}/{statement}",
                    institution,
                    card=card,
                )
            )
    if not sections:
        raise OfxError("missing_element", "OFX")
    if len({section.section_id for section in sections}) != len(sections):
        raise OfxError("duplicate_account_section", "OFX")
    return OfxDocument(tuple(sections))


def _decimal(value: ExactDecimal) -> dict:
    return {
        "coefficient": value.coefficient,
        "scale": value.scale,
        "source_scale": value.source_scale,
    }


def _timestamp(value: OfxTime) -> dict:
    time = value.observation
    return {
        "timestamp_date": time.date,
        "timestamp_local": time.local,
        "timestamp_offset_seconds": time.offset_seconds,
        "timestamp_zone": time.zone,
        "timestamp_precision": time.precision.value,
        "timestamp_fraction_digits": time.fraction_digits,
    }


def to_extraction(document: OfxDocument, context: ExtractionContext) -> dict:  # noqa: C901, PLR0912
    """Emit one closed wire for bank, card, posted and pending observations."""
    records = []
    sections = []
    for section in document.sections:

        def record(  # noqa: PLR0913, PLR0917
            kind: str,
            locator: str,
            event: str,
            key: str | None,
            payload: dict,
            correction: tuple[str, str] | None = None,
        ) -> dict:
            return {
                "kind": kind,
                "source_record_id": source_record_digest(
                    source_anchor(context, section.section_id, locator)  # noqa: B023
                ),
                "source_section": section.section_id,  # noqa: B023
                "record_locator": locator,
                "external_record_id": key,
                "event_key": event,
                "source_account_identifier": section.account_id,  # noqa: B023
                "source_key_domain": "ofx-fitid" if key is not None else None,
                "source_key": key,
                "source_correction": {
                    "source_key_domain": "ofx-fitid",
                    "target_source_key": correction[0],
                    "action": correction[1],
                }
                if correction
                else None,
                "payload": payload,
            }

        def field(  # noqa: PLR0913
            locator: str,
            event: str,
            key: str | None,
            name: str,
            value: str | ExactDecimal,
            *,
            subject_locator: str | None = None,
        ) -> None:
            records.append(
                record(
                    "observation",
                    locator,
                    event,
                    key,
                    {
                        "subject_source_record_id": source_record_digest(
                            source_anchor(context, section.section_id, subject_locator)  # noqa: B023
                        )
                        if subject_locator is not None
                        else None,
                        "field_name": name,
                        "observation_kind": "source_status"
                        if name == "source.status"
                        else "source_field",
                        "value_type": "decimal"
                        if isinstance(value, ExactDecimal)
                        else "text",
                        "decimal_value": _decimal(value)
                        if isinstance(value, ExactDecimal)
                        else None,
                        "text_value": value if isinstance(value, str) else None,
                        "date_value": None,
                        "commodity_token": section.currency  # noqa: B023
                        if isinstance(value, ExactDecimal)
                        else None,
                    },
                )
            )

        for row in section.transactions:
            dates = dict.fromkeys(
                f"date_{role}"
                for role in ("authorized", "traded", "posted", "settled", "value")
            )
            for role, time in row.times:
                if role not in {"initiated", "expires"}:
                    dates[f"date_{role}"] = time.observation.date
            leg = {
                "posting_key": "cash",
                "source_account_identifier": section.account_id,
                "source_position_key": None,
                "commodity_token": section.currency,
                "amount": _decimal(row.amount),
                "posting_role": "principal",
                "symbol_as_stated": None,
                "broker_lot_id": None,
                "price_per_unit": None,
                "price_commodity_token": None,  # nosec B105
                "original_amount": None,
                "original_commodity_token": row.original_currency[0]
                if row.original_currency
                else None,
                "fx_rate_as_stated": _decimal(row.original_currency[1])
                if row.original_currency
                else None,
                "fx_base_commodity_token": row.original_currency[0]
                if row.original_currency
                else None,
                "fx_quote_commodity_token": section.currency
                if row.original_currency
                else None,
                **dates,
                **{
                    f"{name}_mode": "unknown" if value is None else "value"
                    for name, value in dates.items()
                },
                "components": [],
            }
            records.append(
                record(
                    "transaction",
                    row.locator,
                    "transaction",
                    row.external_id,
                    {
                        **dates,
                        "description_as_stated": row.name,
                        "payee_as_stated": row.name,
                        "status_token": "STMTTRNP" if row.pending else "STMTTRN",
                        "legs": [leg],
                        "times": [
                            {"date_role": role, **_timestamp(time)}
                            for role, time in row.times
                        ],
                    },
                    row.correction,
                )
            )
            field(
                row.locator,
                "transaction",
                row.external_id,
                "source.status",
                "STMTTRNP" if row.pending else "STMTTRN",
                subject_locator=row.locator,
            )
            for tag, value in (
                ("TRNTYPE", row.trntype),
                ("MEMO", row.memo),
                *row.fields,
            ):
                if value is not None:
                    field(
                        f"{row.locator}/{tag}",
                        "transaction",
                        row.external_id,
                        f"source.{tag.lower()}",
                        value,
                        subject_locator=row.locator,
                    )
            for role, time in row.times:
                tag = {
                    "posted": "DTPOSTED",
                    "authorized": "DTUSER",
                    "initiated": "DTTRAN",
                    "value": "DTAVAIL",
                    "expires": "DTEXPIRE",
                }[role]
                field(
                    f"{row.locator}/{tag}",
                    "transaction",
                    row.external_id,
                    f"source.{tag.lower()}",
                    time.raw,
                    subject_locator=row.locator,
                )
            if row.original_currency:
                for tag, value in (
                    ("CURSYM", row.original_currency[0]),
                    ("CURRATE", row.original_currency[1]),
                ):
                    field(
                        f"{row.locator}/ORIGCURRENCY/{tag}",
                        "transaction",
                        row.external_id,
                        f"source.origcurrency.{tag.lower()}",
                        value,
                        subject_locator=row.locator,
                    )
        for tag, value in section.account_context:
            if tag in {"BANKID", "ACCTTYPE"}:
                field(
                    f"{section.locator}/BANKACCTFROM/{tag}",
                    "account",
                    None,
                    f"source.{tag.lower()}",
                    value,
                )
        parts = []
        if section.start is not None and section.end is not None:
            parts.append(
                {
                    "part_key": "posted",
                    "kind": "movement_interval",
                    "date_role": "posted",
                    "start": {
                        "raw_value": section.start.raw,
                        **_timestamp(section.start),
                    },
                    "end": {"raw_value": section.end.raw, **_timestamp(section.end)},
                    "completeness": "unstated",
                    "record_locators": [
                        row.locator for row in section.transactions if not row.pending
                    ],
                }
            )
            for tag, time in (("DTSTART", section.start), ("DTEND", section.end)):
                field(
                    f"{section.locator}/BANKTRANLIST/{tag}",
                    "interval",
                    None,
                    f"source.{tag.lower()}",
                    time.raw,
                )
        for balance, part_key, kind, meaning in (
            (section.ledger, "settled", "balance_point", "settled_position"),
            (
                section.available,
                "available",
                "available_balance",
                "available_credit" if section.card else "available_funds",
            ),
        ):
            if balance is None:
                continue
            parts.append(
                {
                    "part_key": part_key,
                    "kind": kind,
                    "meaning": meaning,
                    "point": {
                        "raw_value": balance.time.raw,
                        **_timestamp(balance.time),
                    },
                    "completeness": "unstated",
                    "record_locators": [balance.locator],
                }
            )
            if kind == "balance_point":
                records.append(
                    record(
                        "assertion",
                        balance.locator,
                        "ledgerbal",
                        None,
                        {
                            **_timestamp(balance.time),
                            "date": balance.time.observation.date,
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
                                    "commodity_token": section.currency,
                                    "amount": _decimal(balance.amount),
                                }
                            ],
                        },
                    )
                )
            else:
                field(
                    balance.locator,
                    "availbal",
                    None,
                    f"source.{meaning}",
                    balance.amount,
                )
            field(
                f"{balance.locator}/DTASOF",
                part_key,
                None,
                f"source.{part_key}.dtasof",
                balance.time.raw,
                subject_locator=balance.locator if kind == "balance_point" else None,
            )
        if section.pending_as_of is not None:
            parts.append(
                {
                    "part_key": "pending",
                    "kind": "pending_snapshot",
                    "meaning": "pending",
                    "point": {
                        "raw_value": section.pending_as_of.raw,
                        **_timestamp(section.pending_as_of),
                    },
                    "completeness": "unstated",
                    "record_locators": [
                        row.locator for row in section.transactions if row.pending
                    ],
                }
            )
            field(
                f"{section.locator}/BANKTRANLISTP/DTASOF",
                "pending",
                None,
                "source.pending.dtasof",
                section.pending_as_of.raw,
            )
        sections.append(
            {
                "section_id": section.section_id,
                "source_class": "rolling",
                "institution": section.institution,
                "source_account_identifier": section.account_id,
                "stated_currency": section.currency,
                "account_context": [
                    {"field_name": tag, "text_value": value}
                    for tag, value in section.account_context
                ],
                "revisions": [],
                "evidence_parts": parts,
            }
        )
    return {
        "schema_version": 1,
        "source_scope_id": context.source_scope_id,
        "source_blob_digest": context.source_blob_digest,
        "importer_id": context.importer_id,
        "status_mappings": [
            {
                "source_field": "source.status",
                "source_token": token,
                "event_key": "transaction",
                "candidate_state": state,
            }
            for token, state in (("STMTTRN", "recognized"), ("STMTTRNP", "pending"))
        ],
        "source_scope": {"sections": sections},
        "records": records,
        "unsupported_content": [],
    }


class OfxImporter:
    """Installed bounded bank/card adapter."""

    contract_version = 1
    profile = "ofx-xml-bank-card"
    profile_version = 2
    maximum_bytes = MAX_BYTES

    def extract(self, data: bytes, context: ExtractionContext) -> dict:
        """Preserve exact source evidence through the shared wire."""
        return to_extraction(extract(data), context)


importer = OfxImporter()
