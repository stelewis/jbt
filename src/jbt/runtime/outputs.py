"""Render and independently validate outputs from one resolved record."""

from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from typing import TYPE_CHECKING

from jbt.artifacts.beancount import (
    BeancountPolicy,
    DirectiveLifetime,
    render_beancount,
)
from jbt.checks.quantity import CheckKind
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import total
from jbt.runtime.project import Project, document, objects, text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jbt.checks.quantity import Check


def _lifetime(project: Project, kind: str, identifier: str) -> DirectiveLifetime:
    field = f"{kind}_id"
    selected = tuple(
        item
        for item in project.declarations
        if item.kind == kind and item.decode()[field] == identifier
    )
    require(bool(selected), "render_lifetime_required", kind)
    declaration = min(selected, key=lambda d: d.valid_from or date.min)
    if kind == "position":
        account_id = text(declaration.decode()["account_id"], kind)
        accounts = tuple(
            d
            for d in project.declarations
            if d.kind == "account" and d.decode()["account_id"] == account_id
        )
        require(bool(accounts), "render_account_missing", kind)
        horizon = date.fromisoformat(
            text(project.configuration_document()["as_of"], "as_of")
        )
        known = tuple(
            d for d in accounts if d.valid_from is None or d.valid_from <= horizon
        )
        require(bool(known), "render_account_policy_unavailable", kind)
        latest = max(d.valid_from or date.min for d in known)
        applicable = tuple(d for d in known if (d.valid_from or date.min) == latest)
        require(len(applicable) == 1, "render_account_policy_unavailable", kind)
        account = applicable[0].decode()
        require(account["opened_date"] is not None, "render_lifetime_required", kind)
        closed = (
            date.fromisoformat(text(account["closed_date"], "closed_date"))
            if account["closed_date"]
            else None
        )
        require(closed != date.max, "render_close_date_overflow", kind)
        return DirectiveLifetime(
            declaration.revision,
            date.fromisoformat(text(account["opened_date"], "opened_date")),
            closed + timedelta(days=1) if closed is not None else None,
        )
    start = declaration.valid_from
    if start is None:
        code = "render_lifetime_required"
        raise ContractError(code, kind)
    return DirectiveLifetime(declaration.revision, start, None)


def ledger(
    project: Project, tables: Mapping[str, Sequence[dict[str, object]]]
) -> bytes:
    """Require the real ledger loader to accept the complete projection."""
    from beancount import loader  # noqa: PLC0415

    positions: dict[str, str] = {}
    categories: dict[str, str] = {}
    notes: dict[str, str] = {}
    for mapping in objects(project.configuration_document()["rendering"], "rendering"):
        require(
            mapping["sink"] == "beancount"
            and mapping["primary_date_role"] == "posted"
            and mapping["negative_name"] is None,
            "rendering_unsupported",
            "rendering",
        )
        target = document(mapping["target"])
        keys = target["key"]
        require(isinstance(keys, list) and len(keys) == 1, "render_target", "rendering")
        if not isinstance(keys, list):
            code = "render_target"
            raise ContractError(code, "rendering")
        identifier = text(keys[0], "render_target")
        require(
            target["table"] in {"positions", "categories", "transactions"},
            "render_target",
            identifier,
        )
        names = (
            positions
            if target["table"] == "positions"
            else notes
            if target["table"] == "transactions"
            else categories
        )
        require(identifier not in names, "duplicate_render_target", identifier)
        names[identifier] = text(mapping["positive_name"], "render_name")
    commodity_symbols = {}
    for commodity in tables["commodities"]:
        identifier = text(commodity["commodity_id"], "commodity")
        symbols = {
            text(symbol["symbol"], "symbol")
            for symbol in tables["commodity_symbols"]
            if symbol["commodity_id"] == identifier
            and symbol["source_scope_id"] is None
        }
        require(len(symbols) == 1, "render_symbol_ambiguous", identifier)
        commodity_symbols[identifier] = next(iter(symbols))
    policy = BeancountPolicy(
        entity_id=project.entity.value,
        date_role="posted",
        position_accounts=positions,
        category_accounts=categories,
        commodity_symbols=commodity_symbols,
        position_lifetimes={
            key: _lifetime(project, "position", key) for key in positions
        },
        category_lifetimes={
            key: _lifetime(project, "category", key) for key in categories
        },
        commodity_lifetimes={
            identifier: _lifetime(project, "commodity", identifier)
            for identifier in commodity_symbols
        },
        note_accounts=notes,
    )
    output = render_beancount(tables, policy=policy)
    _, errors, _ = loader.load_string(output)
    require(not errors, "ledger_validation_failed", "beancount")
    return output.encode("utf-8")


def check_document(
    check: Check, *, entity_id: str, commodity_id: str | None
) -> dict[str, object]:
    """Serialize actual check results, never configuration-supplied claims."""

    def value(item: ExactDecimal | int | str | None) -> dict[str, object]:
        if isinstance(item, ExactDecimal):
            return {
                "kind": "decimal",
                "value": asdict(item),
                "commodity_id": commodity_id,
            }
        if item is None:
            return {"kind": "unavailable", "reason": "insufficient_evidence"}
        if isinstance(item, str):
            return {"kind": "text", "value": item}
        return {"kind": "integer", "value": item}

    return {
        "check_id": check.key.value,
        "check_kind": check.kind.value,
        "entity_id": entity_id,
        "status": check.status.value,
        "severity": "error",
        "expected": value(check.expected),
        "observed": value(check.observed),
        "scope": [{"table": "postings", "key": [key.value]} for key in check.evidence]
        if check.kind == CheckKind.TRANSFER
        else [],
        "evidence": [
            {"kind": "source_record", "id": key.value} for key in check.evidence
        ]
        if check.kind != CheckKind.TRANSFER
        else [],
        "policy_declaration_ids": [],
        "exception_declaration_id": None,
    }


def _quantities(
    tables: Mapping[str, Sequence[dict[str, object]]],
) -> dict[str, ExactDecimal]:
    recognized = {
        r["txn_id"] for r in tables["transactions"] if r["event_state"] == "recognized"
    }
    values: dict[str, list[ExactDecimal]] = defaultdict(list)
    for row in tables["postings"]:
        if row["txn_id"] not in recognized or row["leg_kind"] != "position":
            continue
        key = text(row["position_id"], "position_id")
        values[key].append(_amount(row))
    return {key: total(tuple(items)) for key, items in sorted(values.items())}


def _amount(row: Mapping[str, object]) -> ExactDecimal:
    scale, source_scale = row["amount_scale"], row["amount_source_scale"]
    require(
        type(scale) is int and (source_scale is None or type(source_scale) is int),
        "numeric_group",
        "postings",
    )
    if not isinstance(scale, int) or not (
        source_scale is None or isinstance(source_scale, int)
    ):
        code = "numeric_group"
        raise ContractError(code, "postings")
    return ExactDecimal(
        text(row["amount_coefficient"], "postings"), scale, source_scale
    )


def _summary_flows(
    tables: Mapping[str, Sequence[dict[str, object]]],
) -> tuple[
    dict[tuple[str, str], list[ExactDecimal]],
    dict[tuple[str, str], list[ExactDecimal]],
    dict[tuple[str, str], list[ExactDecimal]],
    dict[tuple[str, str, str], list[ExactDecimal]],
    dict[tuple[str, str | None], list[ExactDecimal]],
    set[str],
]:
    monthly: dict[tuple[str, str], list[ExactDecimal]] = defaultdict(list)
    initialization: dict[tuple[str, str], list[ExactDecimal]] = defaultdict(list)
    movement: dict[tuple[str, str], list[ExactDecimal]] = defaultdict(list)
    flows: dict[tuple[str, str, str], list[ExactDecimal]] = defaultdict(list)
    uncategorized: dict[tuple[str, str | None], list[ExactDecimal]] = defaultdict(list)
    uncategorized_events: set[str] = set()
    transactions = {row["txn_id"]: row for row in tables["transactions"]}
    accounts_by_event: dict[str, set[str]] = defaultdict(set)
    for row in tables["postings"]:
        if row["leg_kind"] == "position":
            accounts_by_event[text(row["txn_id"], "txn")].add(
                text(row["account_id"], "account")
            )
    for posting in tables["postings"]:
        event = transactions[posting["txn_id"]]
        if event["event_state"] != "recognized":
            continue
        effective_date = (
            posting["date_posted"]
            if posting["date_posted_mode"] == "value"
            else event["date_posted"]
            if posting["date_posted_mode"] == "inherit"
            else None
        )
        require(
            effective_date is not None,
            "summary_posting_date_required",
            text(posting["posting_id"], "posting_id"),
        )
        month = text(effective_date, "date_posted")[:7]
        value = _amount(posting)
        commodity = text(posting["commodity_id"], "commodity")
        if posting["leg_kind"] == "boundary":
            if posting["category_id"] is None:
                accounts = accounts_by_event[text(posting["txn_id"], "txn")]
                account = next(iter(accounts)) if len(accounts) == 1 else None
                uncategorized[(commodity, account)].append(value)
                uncategorized_events.add(text(posting["txn_id"], "txn"))
            else:
                flows[
                    (month, text(posting["category_id"], "category"), commodity)
                ].append(value)
            continue
        key = (month, text(posting["position_id"], "position"))
        monthly[key].append(value)
        if event["event_kind"] == "opening":
            initialization[key].append(value)
        else:
            movement[key].append(value)
    return monthly, initialization, movement, flows, uncategorized, uncategorized_events


def _changes(
    current: dict[str, ExactDecimal],
    previous: dict[str, ExactDecimal],
    *,
    has_baseline: bool,
) -> list[dict[str, object]]:
    zero = ExactDecimal("0", 0, None)
    changes: list[dict[str, object]] = []
    for key in sorted(current.keys() | previous.keys()):
        before = previous.get(key, zero)
        after = current.get(key, zero)
        changes.append(
            {
                "position_id": key,
                "before": asdict(before) if has_baseline else None,
                "after": asdict(after),
                "change": asdict(
                    after.rational().subtract(before.rational()).exact_decimal()
                )
                if has_baseline
                else None,
            }
        )
    return changes


def _monthly_periods(
    project: Project,
    tables: Mapping[str, Sequence[dict[str, object]]],
    *,
    monthly: dict[tuple[str, str], list[ExactDecimal]],
    initialization: dict[tuple[str, str], list[ExactDecimal]],
    movement: dict[tuple[str, str], list[ExactDecimal]],
) -> list[dict[str, object]]:
    periods: list[dict[str, object]] = []
    zero = ExactDecimal("0", 0, None)
    as_of = text(project.configuration_document()["as_of"], "as_of")
    last = date.fromisoformat(as_of)
    require(
        all(month <= as_of[:7] for month, _ in monthly), "summary_horizon", "summary"
    )
    accounts = {a["account_id"]: a for a in tables["accounts"]}
    for position in tables["positions"]:
        identifier = text(position["position_id"], "position")
        account = accounts[position["account_id"]]
        coverage = text(
            account["coverage_start_date"] or account["opened_date"],
            "coverage_start_date",
        )
        months = tuple(f"{month}-01" for month, p in monthly if p == identifier)
        first = date.fromisoformat(min((coverage, *months)))
        running = zero
        for index in range(
            first.year * 12 + first.month - 1, last.year * 12 + last.month
        ):
            year, month_index = divmod(index, 12)
            month = f"{year:04d}-{month_index + 1:02d}"
            key = (month, identifier)
            start = total((running, *initialization[key]))
            movements = tuple(movement[key])
            credited = total(v for v in movements if int(v.coefficient) > 0)
            debits = total(v for v in movements if int(v.coefficient) < 0)
            running = total((running, *monthly[key]))
            if month < coverage[:7]:
                continue
            periods.append(
                {
                    "month": month,
                    "account_id": position["account_id"],
                    "position_id": identifier,
                    "commodity_id": position["commodity_id"],
                    "opening": asdict(start),
                    "credits": asdict(credited),
                    "debits": asdict(debits),
                    "closing": asdict(running),
                }
            )
    return periods


def summary(  # noqa: PLR0913 - complete sink context, including captured journal
    project: Project,
    tables: Mapping[str, Sequence[dict[str, object]]],
    *,
    checks: Sequence[dict[str, object]],
    baseline: Mapping[str, Sequence[dict[str, object]]] | None,
    baseline_digest: str | None,
    journal_state: Mapping[str, object] | None = None,
) -> bytes:
    """Keep the comparison context explicit and currencies unaggregated."""
    require(
        (baseline is None) == (baseline_digest is None), "summary_baseline", "summary"
    )
    monthly, initialization, movement, flows, uncategorized, uncategorized_events = (
        _summary_flows(tables)
    )
    changes = _changes(
        _quantities(tables),
        _quantities(baseline) if baseline is not None else {},
        has_baseline=baseline is not None,
    )
    periods = _monthly_periods(
        project,
        tables,
        monthly=monthly,
        initialization=initialization,
        movement=movement,
    )
    transit = {
        p["position_id"] for p in tables["positions"] if p["purpose"] == "in_transit"
    }
    return encode_json(
        {
            "schema_version": 1,
            "entity_id": project.entity.value,
            "as_of": project.configuration_document()["as_of"],
            "comparison_baseline": baseline_digest,
            "journal": journal_state,
            "monthly_quantities": periods,
            "transit_balances": [
                period for period in periods if period["position_id"] in transit
            ],
            "category_flows": [
                {
                    "month": month,
                    "category_id": category,
                    "commodity_id": commodity,
                    "amount": asdict(total(tuple(values))),
                }
                for (month, category, commodity), values in sorted(flows.items())
            ],
            "changes": changes,
            "review": {
                "unreviewed_events": sum(
                    row["review_state"] == "unreviewed"
                    for row in tables["transactions"]
                ),
                "uncategorized_events": len(uncategorized_events),
                "pending_events": sum(
                    row["event_state"] == "pending" for row in tables["transactions"]
                ),
                "blocked_records": 0,
            },
            "uncategorized_work": [
                {
                    "commodity_id": commodity,
                    "account_id": account,
                    "attribution": "account" if account is not None else "unattributed",
                    "leg_count": len(values),
                    "amount": asdict(total(values)),
                }
                for (commodity, account), values in sorted(
                    uncategorized.items(),
                    key=lambda item: (item[0][0], item[0][1] or ""),
                )
            ],
            "checks": list(checks),
        },
        integer_strings=False,
    )
