"""Render and independently validate outputs from one resolved record."""

# Exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING

from jbt.artifacts.beancount import (
    BeancountPolicy,
    DirectiveLifetime,
    render_beancount,
)
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import encode_json
from jbt.domain.cash import CashCheck, total
from jbt.domain.numbers import ExactDecimal
from jbt.runtime.project import Project, document, objects, text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _lifetime(project: Project, kind: str, identifier: str) -> DirectiveLifetime:
    field = f"{kind}_id"
    selected = tuple(
        item
        for item in project.declarations
        if item.kind == kind and item.decode()[field] == identifier
    )
    require(
        len(selected) == 1 and selected[0].valid_from is not None,
        "render_lifetime_required",
        kind,
    )
    declaration = selected[0]
    if declaration.valid_from is None:
        raise ContractError("render_lifetime_required", kind)
    return DirectiveLifetime(
        declaration.revision, declaration.valid_from, declaration.valid_to
    )


def ledger(
    project: Project, tables: Mapping[str, Sequence[dict[str, object]]]
) -> bytes:
    """Require the real ledger loader to accept the complete projection."""
    from beancount import loader  # noqa: PLC0415

    positions: dict[str, str] = {}
    categories: dict[str, str] = {}
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
            raise ContractError("render_target", "rendering")
        identifier = text(keys[0], "render_target")
        require(
            target["table"] in {"positions", "categories"}, "render_target", identifier
        )
        names = positions if target["table"] == "positions" else categories
        require(identifier not in names, "duplicate_render_target", identifier)
        names[identifier] = text(mapping["positive_name"], "render_name")
    commodity = project.one("commodity")
    payload = commodity.decode()
    symbols = objects(payload["symbols"], commodity.key)
    require(len(symbols) == 1, "single_render_symbol_required", commodity.key)
    commodity_id = text(payload["commodity_id"], commodity.key)
    policy = BeancountPolicy(
        entity_id=project.entity.value,
        date_role="posted",
        position_accounts=positions,
        category_accounts=categories,
        commodity_symbols={commodity_id: text(symbols[0]["symbol"], commodity.key)},
        position_lifetimes={
            key: _lifetime(project, "position", key) for key in positions
        },
        category_lifetimes={
            key: _lifetime(project, "category", key) for key in categories
        },
        commodity_lifetimes={
            commodity_id: _lifetime(project, "commodity", commodity_id)
        },
    )
    output = render_beancount(tables, policy=policy)
    _, errors, _ = loader.load_string(output)
    require(not errors, "ledger_validation_failed", "beancount")
    return output.encode("utf-8")


def check_document(
    check: CashCheck, *, entity_id: str, commodity_id: str
) -> dict[str, object]:
    """Serialize actual check results, never configuration-supplied claims."""

    def value(item: ExactDecimal | int | None) -> dict[str, object]:
        if isinstance(item, ExactDecimal):
            return {
                "kind": "decimal",
                "value": asdict(item),
                "commodity_id": commodity_id,
            }
        if item is None:
            return {"kind": "unavailable", "reason": "insufficient_evidence"}
        return {"kind": "integer", "value": item}

    return {
        "check_id": check.key,
        "check_kind": check.kind,
        "entity_id": entity_id,
        "status": check.status.value,
        "severity": "error",
        "expected": value(check.expected),
        "observed": value(check.observed),
        "scope": [],
        "evidence": [
            {"kind": "source_record", "id": key} for key in check.evidence_ids
        ],
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
        raise ContractError("numeric_group", "postings")
    return ExactDecimal(
        text(row["amount_coefficient"], "postings"), scale, source_scale
    )


def summary(
    project: Project,
    tables: Mapping[str, Sequence[dict[str, object]]],
    *,
    checks: Sequence[dict[str, object]],
    baseline: Mapping[str, Sequence[dict[str, object]]] | None,
    baseline_digest: str | None,
) -> bytes:
    """Keep the comparison context explicit and currencies unaggregated."""
    require(
        (baseline is None) == (baseline_digest is None), "summary_baseline", "summary"
    )
    monthly: dict[str, list[ExactDecimal]] = defaultdict(list)
    initialization: dict[str, list[ExactDecimal]] = defaultdict(list)
    movement: dict[str, list[ExactDecimal]] = defaultdict(list)
    flows: dict[tuple[str, str], list[ExactDecimal]] = defaultdict(list)
    transactions = {row["txn_id"]: row for row in tables["transactions"]}
    boundary = {
        row["txn_id"]: row
        for row in tables["postings"]
        if row["leg_kind"] == "boundary"
    }
    for posting in tables["postings"]:
        event = transactions[posting["txn_id"]]
        if event["event_state"] != "recognized" or posting["leg_kind"] != "position":
            continue
        month = text(event["date_posted"], "date_posted")[:7]
        value = _amount(posting)
        monthly[month].append(value)
        if event["event_kind"] == "opening":
            initialization[month].append(value)
        else:
            movement[month].append(value)
            category = text(boundary[posting["txn_id"]]["category_id"], "category")
            flows[(month, category)].append(value)
    position = project.one("position").decode()
    current = _quantities(tables)
    previous = _quantities(baseline) if baseline is not None else {}
    zero = ExactDecimal("0", 0, None)
    changes = []
    for key in sorted(current.keys() | previous.keys()):
        before = previous.get(key, zero)
        after = current.get(key, zero)
        changes.append(
            {
                "position_id": key,
                "before": asdict(before) if baseline is not None else None,
                "after": asdict(after),
                "change": asdict(
                    after.rational().subtract(before.rational()).exact_decimal()
                )
                if baseline is not None
                else None,
            }
        )
    running = zero
    periods = []
    coverage = text(project.one("account").decode()["coverage_start_date"], "coverage")
    as_of = text(project.configuration_document()["as_of"], "as_of")
    first = date.fromisoformat(min((coverage, *(f"{month}-01" for month in monthly))))
    last = date.fromisoformat(as_of)
    require(all(month <= as_of[:7] for month in monthly), "summary_horizon", "summary")
    for index in range(first.year * 12 + first.month - 1, last.year * 12 + last.month):
        year, month_index = divmod(index, 12)
        month = f"{year:04d}-{month_index + 1:02d}"
        values = monthly[month]
        start = total((running, *initialization[month]))
        movements = tuple(movement[month])
        credit_total = total(tuple(v for v in movements if int(v.coefficient) > 0))
        debits = total(tuple(v for v in movements if int(v.coefficient) < 0))
        running = total((running, *values))
        if month < coverage[:7]:
            continue
        periods.append(
            {
                "month": month,
                "account_id": position["account_id"],
                "position_id": position["position_id"],
                "commodity_id": position["commodity_id"],
                "opening": asdict(start),
                "credits": asdict(credit_total),
                "debits": asdict(debits),
                "closing": asdict(running),
            }
        )
    return encode_json(
        {
            "schema_version": 1,
            "entity_id": project.entity.value,
            "as_of": project.configuration_document()["as_of"],
            "comparison_baseline": baseline_digest,
            "monthly_quantities": periods,
            "category_flows": [
                {
                    "month": month,
                    "category_id": category,
                    "commodity_id": position["commodity_id"],
                    "amount": asdict(total(tuple(values))),
                }
                for (month, category), values in sorted(flows.items())
            ],
            "changes": changes,
            "review": {
                "unreviewed_events": sum(
                    row["review_state"] == "unreviewed"
                    for row in tables["transactions"]
                ),
                "uncategorized_events": sum(
                    row["category_id"] is None for row in boundary.values()
                ),
            },
            "checks": list(checks),
        },
        integer_strings=False,
    )
