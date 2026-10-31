"""Decode retained source evidence for the independent check engine."""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import decode_document
from jbt.artifacts.projection import projection_key
from jbt.checks.coverage import (
    Cadence,
    Claim,
    ExpectedPeriod,
    Movement,
    Period,
    Policy,
    coverage_checks,
    lifecycle_checks,
)
from jbt.checks.quantity import (
    AssertionKind,
    Check,
    CheckKind,
    Measurement,
    ModelEvidence,
    ProjectedAssertion,
    ProjectedBalance,
    ProjectedPosition,
    ProjectedPosting,
    ProjectedTransfer,
    ProjectedTransferMember,
    ScopeKind,
    SourceInterval,
    SourceMovement,
    SourcePoint,
    model_checks,
    source_checks,
)
from jbt.contracts.primitives import ContractError, require
from jbt.domain.ids import SemanticKey
from jbt.domain.quantity import EventState, LegKind
from jbt.domain.time import TimeRole, is_midnight
from jbt.runtime.project import (
    Project,
    boolean,
    document,
    integer,
    nullable_text,
    objects,
    strings,
    text,
)
from jbt.runtime.resolution import (
    ExtractInput,
    exact,
    model_boundary,
    source_civil_context,
    source_role_time,
)

if TYPE_CHECKING:
    from jbt.domain.numbers import ExactDecimal


def _source_points(
    project: Project,
    record: Mapping[str, object],
    *,
    scope: SemanticKey,
    section: SemanticKey,
) -> tuple[SourcePoint, ...]:
    payload = document(record["payload"])
    account, zone, _ = source_civil_context(
        project,
        scope=scope,
        section=section,
        time=payload,
    )
    time = model_boundary(payload, point=True, civil_zone=zone)
    return tuple(
        SourcePoint(
            scope,
            section,
            text(observation["commodity_token"], "commodity_token"),
            time.date,
            exact(observation["amount"]),
            _key(record["source_record_id"]),
            account=account,
            basis=_key(payload["coverage_basis"]),
            scope_kind=ScopeKind(text(payload["scope_kind"], "scope_kind")),
            measurement=Measurement(text(payload["measurement"], "measurement")),
            kind=AssertionKind(text(payload["assertion_kind"], "assertion_kind")),
            source_position_key=_key(observation["source_position_key"])
            if observation["source_position_key"] is not None
            else None,
        )
        for observation in objects(payload["observations"], "observations")
    )


def _source_movements(
    project: Project,
    record: Mapping[str, object],
    *,
    scope: SemanticKey,
    section: SemanticKey,
) -> tuple[SourceMovement, ...]:
    payload = document(record["payload"])
    posted = source_role_time(payload, TimeRole.POSTED)
    _, _, time = source_civil_context(
        project, scope=scope, section=section, time=posted
    )
    return tuple(
        SourceMovement(
            time.date,
            text(leg["commodity_token"], "commodity_token"),
            exact(leg["amount"]),
            _key(record["source_record_id"]),
            recognized=True,
            source_position_key=_key(leg["source_position_key"])
            if leg["source_position_key"] is not None
            else None,
        )
        for leg in objects(payload["legs"], "legs")
    )


def _section_evidence(
    project: Project,
    extract: Mapping[str, object],
    section: SemanticKey,
) -> tuple[list[SourceMovement], list[SourcePoint], list[SemanticKey]]:
    scope = _key(extract["source_scope_id"])
    states = {
        (m["source_token"], m["event_key"]): m["candidate_state"]
        for m in objects(extract["status_mappings"], "status")
        if m["source_field"] == "source.status"
    }
    movements: list[SourceMovement] = []
    points: list[SourcePoint] = []
    unavailable: list[SemanticKey] = []
    for record in objects(extract["records"], "records"):
        if record["source_section"] != section.value:
            continue
        payload = document(record["payload"])
        try:
            if record["kind"] == "assertion" and payload["measurement"] == "units":
                points.extend(
                    _source_points(project, record, scope=scope, section=section)
                )
            elif (
                record["kind"] == "transaction"
                and states.get((payload["status_token"], record["event_key"]))
                == "recognized"
            ):
                movements.extend(
                    _source_movements(project, record, scope=scope, section=section)
                )
        except ContractError, ValueError:
            unavailable.append(_key(record["source_record_id"]))
    return movements, points, unavailable


def _source_interval(  # noqa: PLR0913 - retained coordinates and independent membership
    project: Project,
    extract: Mapping[str, object],
    section: Mapping[str, object],
    part: Mapping[str, object],
    *,
    movements: list[SourceMovement],
    points: list[SourcePoint],
) -> SourceInterval:
    scope, section_id = _key(extract["source_scope_id"]), _key(section["section_id"])
    if part["date_role"] != "posted":
        code = "source_interval_role_unsupported"
        raise ContractError(code, "source-checks")
    account, zone, _ = source_civil_context(
        project,
        scope=scope,
        section=section_id,
        time=document(part["start"]),
    )
    start, end = (
        model_boundary(document(part["start"]), point=False, civil_zone=zone),
        model_boundary(document(part["end"]), point=False, civil_zone=zone),
    )
    locators = strings(part["record_locators"], "record_locators")
    members = {
        _key(record["source_record_id"])
        for record in objects(extract["records"], "records")
        if record["kind"] == "transaction"
        and record["source_section"] == section_id.value
        and record["record_locator"] in locators
    }
    return SourceInterval(
        scope,
        section_id,
        start.date,
        end.date,
        _interval_key(extract, section, part),
        tuple(m for m in movements if m.evidence in members),
        tuple(p for p in points if start.date <= p.when <= end.date),
        account=account,
    )


def _interval_key(
    extract: Mapping[str, object],
    section: Mapping[str, object],
    part: Mapping[str, object],
) -> SemanticKey:
    return SemanticKey(
        projection_key(
            "interval",
            extract["source_blob_digest"],
            extract["source_scope_id"],
            section["section_id"],
            part["part_key"],
        )
    )


def retained_source_checks(
    project: Project, extracts: tuple[ExtractInput, ...]
) -> tuple[Check, ...]:
    """Check each retained extract independently, including unavailable evidence."""
    points: list[SourcePoint] = []
    intervals: list[SourceInterval] = []
    unavailable: list[SemanticKey] = []
    ignored = {
        (entry["source_scope_id"], entry["source_section"])
        for entry in objects(
            project.configuration_document().get("ignored_sections", []),
            "ignored_sections",
        )
    }
    documents = {item.payload for item in extracts}
    for encoded in sorted(documents):
        extract = document(decode_document(encoded, "source-checks"))
        scope = SemanticKey(text(extract["source_scope_id"], "scope"))
        for section in objects(
            document(extract["source_scope"])["sections"], "sections"
        ):
            section_id = _key(section["section_id"])
            if (scope.value, section_id.value) in ignored:
                continue
            movements, closing, missing = _section_evidence(
                project, extract, section_id
            )
            points.extend(closing)
            unavailable.extend(missing)
            for part in objects(section["evidence_parts"], "parts"):
                if part["kind"] != "movement_interval":
                    continue
                try:
                    interval = _source_interval(
                        project,
                        extract,
                        section,
                        part,
                        movements=movements,
                        points=closing,
                    )
                except ContractError:
                    unavailable.append(_interval_key(extract, section, part))
                    continue
                intervals.append(interval)
    return source_checks(
        tuple(intervals), tuple(points), unavailable=tuple(unavailable)
    )


type Tables = Mapping[str, Sequence[Mapping[str, object]]]


def _key(value: object) -> SemanticKey:
    return SemanticKey(text(value, "check.identifier"))


def _amount(row: Mapping[str, object]) -> ExactDecimal:
    return exact(
        {
            "coefficient": row["amount_coefficient"],
            "scale": row["amount_scale"],
            "source_scale": row["amount_source_scale"],
        }
    )


def decode_model_evidence(tables: Tables) -> ModelEvidence:
    """Project table grains into typed immutable check inputs, without model state."""
    transactions = {r["txn_id"]: r for r in tables["transactions"]}
    positions = tuple(
        ProjectedPosition(
            _key(p["position_id"]),
            _key(p["account_id"]),
            _key(p["commodity_id"]),
            text(p["purpose"], "purpose"),
        )
        for p in tables["positions"]
    )
    postings = tuple(
        ProjectedPosting(
            _key(p["txn_id"]),
            _key(p["position_id"]) if p["position_id"] else None,
            _key(p["commodity_id"]),
            _amount(p),
            EventState(text(transactions[p["txn_id"]]["event_state"], "event_state")),
            LegKind(text(p["leg_kind"], "leg_kind")),
            _key(p["category_id"]) if p.get("category_id") else None,
            date.fromisoformat(text(p["date_posted"], "date_posted"))
            if p.get("date_posted_mode") == "value"
            else None
            if p.get("date_posted_mode") == "unknown"
            or transactions[p["txn_id"]]["date_posted"] is None
            else date.fromisoformat(
                text(transactions[p["txn_id"]]["date_posted"], "date_posted")
            ),
            identity=_key(p["posting_id"]),
        )
        for p in tables["postings"]
    )
    assertions = []
    for a in tables["balance_assertions"]:
        scope = next(
            s for s in tables["assertion_scopes"] if s["scope_id"] == a["scope_id"]
        )
        selected = tuple(
            p
            for p in positions
            if p.account.value == scope["account_id"]
            and (
                scope["selection"] == "account_all"
                or p.identity.value in strings(scope["position_ids"], "position_ids")
            )
        )
        when = date.fromisoformat(text(a["date"], "assertion.date"))
        if a["assertion_kind"] == "closing":
            when += timedelta(days=1)
        local = nullable_text(a.get("timestamp_local"), "assertion.timestamp_local")
        evaluable = local is None or is_midnight(local)
        assertions.append(
            ProjectedAssertion(
                _key(a["assertion_set_id"]),
                _key(scope["account_id"]),
                ScopeKind(text(scope["scope_kind"], "scope_kind")),
                Measurement(text(scope["measurement"], "measurement")),
                when,
                boolean(a["is_complete"], "is_complete"),
                selected,
                tuple(
                    ProjectedBalance(
                        _key(b["position_id"]) if b["position_id"] else None,
                        _key(b["commodity_id"]),
                        _amount(b),
                    )
                    for b in tables["balances"]
                    if b["assertion_set_id"] == a["assertion_set_id"]
                ),
                evaluable,
            )
        )
    sources = {r["source_record_id"]: r for r in tables.get("source_records", ())}
    required = {
        _key(json.loads(text(o["record_id"], "record_id"))[0])
        for o in tables.get("observations", ())
        if o["field_name"] == "source.status"
        and o["record_kind"] == "source_records"
        and sources[json.loads(text(o["record_id"], "record_id"))[0]]["ignored_reason"]
        is None
        and sources[json.loads(text(o["record_id"], "record_id"))[0]]["model_eligible"]
        is True
    }
    supported = tuple(
        _key(r["source_record_id"]) for r in tables.get("event_support", ())
    )
    transfers = defaultdict(list)
    for row in tables.get("links", ()):
        transfers[text(row["link_id"], "link_id")].append(row)
    return ModelEvidence(
        tuple(assertions),
        postings,
        tuple(sorted(required, key=lambda k: k.value)),
        supported,
        positions,
        tuple(
            ProjectedTransfer(
                SemanticKey(identifier),
                tuple(
                    ProjectedTransferMember(
                        _key(row["member_id"]),
                        text(row["member_role"], "member_role"),
                        text(row["member_kind"], "member_kind"),
                    )
                    for row in sorted(
                        rows,
                        key=lambda row: (
                            row["member_role"],
                            row["member_kind"],
                            row["member_id"],
                        ),
                    )
                ),
                all(
                    row["link_kind"] == "transfer"
                    and row["origin"] == "correction"
                    and row["terms_declaration_id"] is None
                    for row in rows
                ),
            )
            for identifier, rows in sorted(transfers.items())
            if any(row["link_kind"] == "transfer" for row in rows)
        ),
    )


def projected_model_checks(
    tables: Tables, selected: frozenset[CheckKind]
) -> tuple[Check, ...]:
    """Evaluate selected checks from projected table facts, never model internals."""
    return model_checks(decode_model_evidence(tables), selected)


def _date(value: object) -> date | None:
    return (
        date.fromisoformat(text(value, "account.date")) if value is not None else None
    )


def _account_policies(project: Project) -> tuple[Policy, ...]:
    bases = {
        text(basis["basis_id"], "basis_id"): TimeRole(
            text(basis["date_role"], "date_role")
        )
        for basis in objects(
            document(decode_document(project.registries, "registries"))[
                "coverage_bases"
            ],
            "coverage_bases",
        )
    }
    results = []
    for declaration in project.declarations:
        if declaration.kind != "account":
            continue
        account = declaration.decode()
        selected = strings(account["coverage_basis_ids"], "coverage_basis_ids")
        require(
            set(selected) <= bases.keys(), "account_coverage_basis", declaration.key
        )
        results.append(
            Policy(
                _key(account["account_id"]),
                SemanticKey(declaration.revision),
                declaration.valid_from,
                declaration.valid_to,
                _date(account["opened_date"]),
                _date(account["closed_date"]),
                _date(account["coverage_start_date"]),
                Cadence(text(account["statement_cadence"], "cadence"))
                if account["statements_expected"]
                else None,
                tuple((SemanticKey(basis), bases[basis]) for basis in selected),
                integer(account["publication_lag_days"], "publication_lag_days"),
                tuple(
                    ExpectedPeriod(
                        Period(
                            date.fromisoformat(text(period["start"], "start")),
                            date.fromisoformat(text(period["end"], "end"))
                            + timedelta(days=1),
                        ),
                        date.fromisoformat(text(period["due"], "due")),
                    )
                    for period in objects(
                        account["expected_periods"], "expected_periods"
                    )
                ),
                integer(account["cycle_end_day"], "cycle_end_day")
                if account["period_rule"] == "monthly_cycle"
                else None,
                integer(account["closing_tolerance_days"], "closing_tolerance_days")
                if account["period_rule"] == "monthly_cycle"
                else 0,
            )
        )
    return tuple(results)


def projected_account_checks(project: Project, tables: Tables) -> tuple[Check, ...]:
    """Always expose due coverage and enforce account lifecycle independently."""
    policies = _account_policies(project)
    statements = {row["statement_id"]: row for row in tables["statements"]}
    claims = tuple(
        Claim(
            _key(row["statement_id"]),
            _key(statements[row["statement_id"]]["account_id"]),
            _key(row["coverage_basis"]),
            TimeRole(text(row["date_role"], "date_role")),
            Period(
                date.fromisoformat(text(row["period_start"], "period_start")),
                date.fromisoformat(text(row["period_end"], "period_end")),
            ),
        )
        for row in tables["statement_coverage"]
        if row["is_complete"]
        and not row["is_eclipsed"]
        and not statements[row["statement_id"]]["is_eclipsed"]
    )
    evidence = decode_model_evidence(tables)
    positions = {row.identity: row.account for row in evidence.positions}
    initiated = {
        _key(row["record_id"]): date.fromisoformat(
            text(row["timestamp_date"], "timestamp_date")
        )
        for row in tables["event_times"]
        if row["record_kind"] == "transactions"
        and row["date_role"] == "initiated"
        and row["time_mode"] == "value"
    }
    transactions = {_key(row["txn_id"]): row for row in tables["transactions"]}
    movements = tuple(
        Movement(
            posting.identity,
            positions[posting.position],
            posting.when
            if posting.state == EventState.RECOGNIZED
            else initiated.get(posting.event)
            or _date(transactions[posting.event]["date_authorized"]),
        )
        for posting in evidence.postings
        if posting.position in positions
        and posting.state in {EventState.RECOGNIZED, EventState.PENDING}
        and posting.identity is not None
    )
    return (
        *coverage_checks(
            policies,
            claims,
            horizon=date.fromisoformat(
                text(project.configuration_document()["as_of"], "as_of")
            ),
        ),
        *lifecycle_checks(policies, movements),
    )
