"""Adapt project evidence and project quantity determinations into catalog rows."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from jbt.artifacts.integrity import decode_document
from jbt.artifacts.projection import Evidence, Rows, projection_key
from jbt.contracts.declarations import rule_match_fields
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.publication import validate_extraction
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    EventReviewAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    EventTextField,
    LegCategoryAssignment,
)
from jbt.domain.errors import TemporalError
from jbt.domain.identity import (
    AcquiredAnchor,
    AuthoredAnchor,
    Occurrence,
    SourceKeyAnchor,
    child_id,
    event_id,
    source_record_digest,
)
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind, LegKind, ReviewState
from jbt.domain.rules import MatchType, MatchValue, Operator, Predicate, RuleField
from jbt.domain.time import (
    DateMode,
    LegDate,
    Precision,
    SourceTimeObservation,
    TimeObservation,
    TimeRole,
    is_midnight,
)
from jbt.model.merge import balance_dispositions, coverage_segments
from jbt.model.quantity import (
    DATE_ROLES,
    AuthorityMode,
    BalanceEvidence,
    Binding,
    Event,
    EventKind,
    Finding,
    FindingCode,
    IgnoredSection,
    Inputs,
    Leg,
    Movement,
    Position,
    PositionPurpose,
    Resolution,
    RevisionAction,
    SourceAuthority,
    SourceClass,
    SourceLeg,
    SourceRevision,
    SourceStatementRevision,
    Statement,
    StatementPart,
    SupportKind,
    Symbol,
    Validity,
    event_dates,
    leg_dates,
    resolve_source_location,
    source_section_key,
)
from jbt.model.rules import MATCH_FIELDS, Rule, RuleAssignment
from jbt.runtime.project import (
    Declaration,
    Project,
    SourceSelection,
    document,
    integer,
    nullable_integer,
    nullable_text,
    objects,
    strings,
    text,
)
from jbt.runtime.temporal import convert_time, validate_zone

if TYPE_CHECKING:
    from collections.abc import Mapping

type Declarations = tuple[tuple[Declaration, Mapping[str, object]], ...]
QUANTITY_DERIVATIONS = {
    "quantity-v1": {
        "version": 1,
        "position": "Stated signed quantity, with identity retained across zero.",
        "boundary": "Exact opposite boundary quantity; classification is independent.",
        "ordering": "Posted civil date, opening first, then stable event identity.",
        "identity": "Source-key family plus component, or exact retained occurrence.",
    },
    "enrichment-rule-v1": {
        "version": 1,
        "predicate": "Typed pre-enrichment facts; exact equals, in, literal contains.",
        "selection": "Every rule evaluated in order; first scalars win, tags union.",
    },
    "transfer-v1": {
        "version": 1,
        "transit": "Exact opposite source units in the dated transit position.",
        "identity": "Balancing leg identity survives boundary/transit treatment.",
    },
}


@dataclass(frozen=True, slots=True)
class ExtractInput:
    """Captured retained bytes paired with the selected custody accession."""

    selection: SourceSelection
    payload: bytes


@dataclass(frozen=True, slots=True)
class Prepared:
    """Inputs, base rows, and catalog-only supplemental evidence.

    Transaction fields use source-record or authored-event IDs. Posting
    fields use authored-event IDs and native leg keys.
    """

    inputs: Inputs
    rows: Rows
    transaction_fields: Mapping[RecordId, Mapping[str, object]]
    posting_fields: Mapping[RecordId, Mapping[SemanticKey, Mapping[str, object]]]


def exact(value: object) -> ExactDecimal:
    """Decode retained decimal units without rounding or scale coercion."""
    row = document(value)
    scale, source = row["scale"], row["source_scale"]
    if type(scale) is not int or (source is not None and type(source) is not int):
        code = "decimal_scale"
        raise ContractError(code, "resolution")
    return ExactDecimal(text(row["coefficient"], "decimal"), scale, source)


def _match_wire(value: MatchValue) -> object:
    if isinstance(value, ExactDecimal):
        return {
            "coefficient": value.coefficient,
            "scale": value.scale,
            "source_scale": value.source_scale,
        }
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, SemanticKey):
        return value.value
    return list(value) if isinstance(value, tuple) else value


def rule_evidence(resolution: Resolution) -> list[dict[str, object]]:
    """Project the actual fold, without re-evaluating predicates or policy."""
    postings: dict[RecordId, list[str]] = {}
    for event in resolution.events:
        for leg in event.legs:
            if leg.rule_basis is not None:
                postings.setdefault(leg.rule_basis, []).append(
                    child_id(event.identity, RecordKind.LEG, leg.key).value
                )
    return [
        {
            "txn_id": evaluation.event.value,
            "posting_id": evaluation.leg.value,
            "target_posting_ids": sorted(postings.get(evaluation.leg, ())),
            "rule_id": evaluation.rule.declaration.value,
            "rule_key": evaluation.rule.key.value,
            "eligible": evaluation.eligible,
            "matched": evaluation.matched,
            "predicates": [
                {
                    "field": result.predicate.field.value,
                    "operator": result.predicate.operator.value,
                    "value_type": result.predicate.kind.value,
                    "expected": [
                        _match_wire(value) for value in result.predicate.operands
                    ],
                    "observed": _match_wire(result.observed),
                    "matches": result.matches,
                }
                for result in evaluation.predicates
            ],
            "assignments": [
                {
                    "field": result.field.value,
                    "mode": result.field.mode.value,
                    "value": _match_wire(result.value),
                    "outcome": result.outcome.value,
                    "selected": result.selected,
                }
                for result in evaluation.assignments
            ],
        }
        for evaluation in resolution.rules
    ]


def model_time(value: Mapping[str, object], *, civil_zone: str) -> TimeObservation:
    """Derive the account-local clock without changing source evidence."""
    source = SourceTimeObservation(
        text(value["timestamp_date"], "timestamp_date"),
        nullable_text(value["timestamp_local"], "timestamp_local"),
        nullable_integer(value["timestamp_offset_seconds"], "timestamp_offset"),
        nullable_text(value["timestamp_zone"], "timestamp_zone"),
        Precision(text(value["timestamp_precision"], "timestamp_precision")),
        nullable_integer(value["timestamp_fraction_digits"], "timestamp_fraction"),
    )
    try:
        return convert_time(source, civil_zone=civil_zone)
    except TemporalError as error:
        raise ContractError(error.constraint, "time") from error


def model_boundary(
    value: Mapping[str, object], *, point: bool, civil_zone: str
) -> TimeObservation:
    """Require midnight in the comparison's civil zone, not UTC."""
    time = model_time(value, civil_zone=civil_zone)
    if time.precision == Precision.DATE and not point:
        return time
    require(
        is_midnight(time.local),
        "source_boundary_time_unavailable",
        "time",
    )
    return time


def _revision(project: Project, declaration: Declaration) -> RecordId:
    return RecordId(
        project.entity, RecordKind.DECLARATION_REVISION, declaration.revision
    )


def _key(value: object) -> SemanticKey:
    return SemanticKey(text(value, "semantic_key"))


def _nullable_key(value: object) -> SemanticKey | None:
    return None if value is None else _key(value)


def _movement_interval(
    section: Mapping[str, object],
    *,
    record_locator: str | None,
    civil_zone: str,
) -> Validity | None:
    intervals = tuple(
        p
        for p in objects(section["evidence_parts"], "parts")
        if p["kind"] == "movement_interval"
        and (
            record_locator is None
            or record_locator in strings(p["record_locators"], "record_locators")
        )
    )
    if record_locator is not None:
        require(len(intervals) <= 1, "source_interval_ambiguous", "extract")
    if not intervals:
        return None
    return Validity(
        min(
            model_boundary(
                document(p["start"]), point=False, civil_zone=civil_zone
            ).date
            for p in intervals
        ),
        max(
            model_boundary(document(p["end"]), point=False, civil_zone=civil_zone).date
            for p in intervals
        ),
    )


def source_civil_context(
    project: Project,
    *,
    scope: SemanticKey,
    section: SemanticKey,
    time: Mapping[str, object],
) -> tuple[SemanticKey, str, TimeObservation]:
    """Select an account revision against its converted civil day.

    Candidate revisions convert independently. A clock that fits multiple
    revisions after conversion is ambiguous, not an arrival-order choice.
    """
    return _source_civil_context(
        tuple(
            (declaration, declaration.decode())
            for declaration in project.declarations
            if declaration.kind == "account"
        ),
        scope=scope,
        section=section,
        time=time,
    )


def _source_civil_context(
    declarations: Declarations,
    *,
    scope: SemanticKey,
    section: SemanticKey,
    time: Mapping[str, object],
) -> tuple[SemanticKey, str, TimeObservation]:
    candidates = []
    errors = []
    for declaration, payload in declarations:
        if declaration.kind != "account":
            continue
        bindings = tuple(
            binding
            for binding in objects(payload["bindings"], "bindings")
            if binding["source_scope_id"] == scope.value
            and binding["source_section"] == section.value
        )
        if not bindings:
            continue
        zone = text(payload["civil_zone"], "civil_zone")
        try:
            converted = model_time(time, civil_zone=zone)
        except ContractError as error:
            errors.append(error)
            continue
        if declaration.applies(converted.date):
            candidates.extend(
                (_key(payload["account_id"]), zone, converted) for _ in bindings
            )
    if not candidates and errors:
        raise errors[0]
    require(bool(candidates), "source_account_unavailable", "time")
    require(len(candidates) == 1, "source_account_ambiguous", "time")
    return candidates[0]


def _source_temporal_finding(error: ValueError) -> FindingCode:
    return (
        {
            "source_account_unavailable": FindingCode.UNBOUND,
            "source_account_ambiguous": FindingCode.AMBIGUOUS_BINDING,
            "source_interval_role_unsupported": FindingCode.CONTENT,
        }.get(error.code, FindingCode.TIME)
        if isinstance(error, ContractError)
        else FindingCode.TIME
    )


def _dimensions(rows: Rows, declarations: Declarations, *, horizon: date) -> None:
    dimensions = {
        "account": "accounts",
        "category": "categories",
        "commodity": "commodities",
        "counterparty": "counterparties",
        "position": "positions",
        "assertion_scope": "assertion_scopes",
    }
    selected: dict[tuple[str, str], tuple[Declaration, Mapping[str, object]]] = {}
    for item, payload in sorted(
        declarations, key=lambda revision: revision[0].valid_from or date.min
    ):
        rows.add(
            "declarations",
            {
                "declaration_id": item.revision,
                "declaration_key": item.key,
                "declaration_kind": item.kind,
                "revision_digest": item.revision,
                "document_path": "project.json",
                "document_locator": f"json:declarations/{item.key}/{item.revision}",
                "payload_json": item.payload.decode().strip(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            },
            evidence=None,
        )
        if item.kind in dimensions:
            identifier = payload[
                f"{item.kind}_id" if item.kind != "assertion_scope" else "scope_id"
            ]
            key = (item.kind, str(identifier))
            previous = selected.get(key)
            if previous is None or (
                (item.valid_from or date.min) <= horizon
                and (item.valid_from or date.min) > (previous[0].valid_from or date.min)
            ):
                selected[key] = item, payload
    for item, payload in selected.values():
        table = dimensions[item.kind]
        fields: dict[str, object] = {
            k: v for k, v in payload.items() if k in rows.specs[table]["logical_fields"]
        }
        if "declaration_id" in rows.specs[table]["logical_fields"]:
            fields["declaration_id"] = item.revision
        for group in rows.specs[table]["numeric_groups"]:
            if fields.get(group["name"]) is not None:
                fields[group["name"]] = exact(fields[group["name"]])
        evidence = ("declaration", item.revision, "declaration", None)
        rows.add(table, fields, evidence=evidence)
    _dimension_symbols(rows, declarations)


def _dimension_symbols(rows: Rows, declarations: Declarations) -> None:
    symbols: dict[str, tuple[dict, Declaration]] = {}
    for item, payload in declarations:
        if item.kind != "commodity":
            continue
        for symbol in objects(payload["symbols"], item.key):
            identifier = text(symbol["symbol_id"], item.key)
            fields = {**symbol, "commodity_id": payload["commodity_id"]}
            if identifier in symbols:
                require(
                    symbols[identifier][0] == fields,
                    "symbol_revision_conflict",
                    item.key,
                )
            symbols[identifier] = (fields, item)
    for fields, item in symbols.values():
        rows.add(
            "commodity_symbols",
            fields,
            evidence=("declaration", item.revision, "declaration", None),
        )


def _position_policies(
    project: Project, item: Declaration, payload: Mapping[str, object]
) -> tuple[tuple[Position, ...], tuple[Finding, ...]]:
    revision = _revision(project, item)
    validity = Validity(item.valid_from, item.valid_to)
    findings = (
        (Finding(FindingCode.CONTENT, revision, blocking=True),)
        if payload["measurement_kind"] != "quantity"
        or payload["inventory_method"] != "quantity"
        else ()
    )
    positions = tuple(
        Position(
            _key(payload["position_id"]),
            _key(payload["account_id"]),
            _key(payload["commodity_id"]),
            _nullable_key(binding["source_position_key"]) if binding else None,
            _key(binding["source_scope_id"]) if binding else None,
            _key(binding["source_section"]) if binding else None,
            validity,
            revision,
            PositionPurpose(text(payload["purpose"], "position.purpose")),
        )
        for binding in objects(payload["bindings"], item.key) or (None,)
    )
    return positions, findings


def _commodity_symbols(
    project: Project, item: Declaration, payload: Mapping[str, object]
) -> tuple[Symbol, ...]:
    validity = Validity(item.valid_from, item.valid_to)
    symbols: list[Symbol] = []
    for symbol in objects(payload["symbols"], item.key):
        start = (
            date.fromisoformat(text(symbol["valid_from"], "symbol.valid_from"))
            if symbol["valid_from"]
            else None
        )
        end = (
            date.fromisoformat(text(symbol["valid_to"], "symbol.valid_to"))
            if symbol["valid_to"]
            else None
        )
        starts = tuple(d for d in (start, validity.start) if d is not None)
        ends = tuple(d for d in (end, validity.end) if d is not None)
        symbols.append(
            Symbol(
                text(symbol["symbol"], item.key),
                _key(payload["commodity_id"]),
                _nullable_key(symbol["source_scope_id"]),
                Validity(max(starts) if starts else None, min(ends) if ends else None),
                _revision(project, item),
            )
        )
    return tuple(symbols)


def _require_rule_orders(rules: list[Rule]) -> None:
    for first in rules:
        for second in rules:
            if first.key != second.key and first.order == second.order:
                require(
                    (
                        first.validity.end is not None
                        and second.validity.start is not None
                        and first.validity.end <= second.validity.start
                    )
                    or (
                        second.validity.end is not None
                        and first.validity.start is not None
                        and second.validity.end <= first.validity.start
                    ),
                    "rule_order_ambiguous",
                    "rules",
                )


def _match_value(value: object, definition: Mapping[str, object]) -> MatchValue:
    if value is None:
        return None
    column = document(definition["column"])
    match definition["value_type"]:
        case "decimal":
            return exact(value)
        case "date":
            return date.fromisoformat(text(value, "rule_date"))
        case "list":
            return strings(value, "rule_list")
        case "integer":
            return integer(value, "rule_integer")
        case "boolean":
            if type(value) is not bool:
                code = "rule_boolean"
                raise ContractError(code, "rule")
            return value
        case "string":
            return (
                _key(value)
                if column.get("text_semantics") == "identifier"
                else text(value, "rule_text")
            )
        case _:
            code = "rule_match_type"
            raise ContractError(code, "rule")


def _predicate(
    value: Mapping[str, object], definitions: Mapping[str, dict]
) -> Predicate:
    name = text(value["field"], "rule_field")
    definition = definitions[name]
    operator = Operator(text(value["operator"], "rule_operator"))
    raw = value["value"]
    operands = raw if operator == Operator.IN and isinstance(raw, list) else [raw]
    kind = (
        MatchType.KEY
        if definition["column"].get("text_semantics") == "identifier"
        else MatchType(definition["value_type"])
    )
    return Predicate(
        _key(name),
        kind,
        operator,
        tuple(_match_value(item, definition) for item in operands),
    )


def _rule_assignment(value: Mapping[str, object]) -> RuleAssignment:
    field = RuleField(text(value["field"], "rule_assignment"))
    raw = value["value"]
    match field:
        case RuleField.PAYEE | RuleField.NARRATION:
            return EventTextAssignment(
                EventTextField(field.value.split(".")[1]),
                nullable_text(raw, "rule_text"),
            )
        case RuleField.TAGS:
            return EventTagsAssignment(strings(raw, "rule_tags"))
        case RuleField.REVIEW:
            return EventReviewAssignment(ReviewState(text(raw, "rule_review")))
        case RuleField.CATEGORY:
            return LegCategoryAssignment(_nullable_key(raw))


def _policies(
    project: Project,
    declarations: Declarations,
) -> tuple[
    tuple[Binding, ...],
    tuple[Position, ...],
    tuple[Symbol, ...],
    tuple[Rule, ...],
    tuple[Finding, ...],
]:
    bindings, positions, symbols, rules, findings = [], [], [], [], []
    observations = tuple(
        objects(
            document(decode_document(project.registries, "registries"))[
                "observation_fields"
            ],
            "observation_fields",
        )
    )
    match_definitions = rule_match_fields(observations)
    supported_fields = MATCH_FIELDS | {field["field_name"] for field in observations}
    for item, payload in declarations:
        validity = Validity(item.valid_from, item.valid_to)
        revision = _revision(project, item)
        if item.kind == "account":
            try:
                validate_zone(text(payload["civil_zone"], "civil_zone"))
            except TemporalError:
                findings.append(Finding(FindingCode.TIME, revision, blocking=True))
            for binding in objects(payload["bindings"], item.key):
                require(
                    binding["source_position_key"] is None,
                    "account_binding_position",
                    item.key,
                )
                bindings.append(
                    Binding(
                        _key(binding["source_scope_id"]),
                        _key(binding["source_section"]),
                        _key(payload["account_id"]),
                        validity,
                        revision,
                    )
                )
        elif item.kind == "position":
            stated_positions, position_findings = _position_policies(
                project, item, payload
            )
            positions.extend(stated_positions)
            findings.extend(position_findings)
        elif item.kind == "commodity":
            symbols.extend(_commodity_symbols(project, item, payload))
        elif item.kind == "rule":
            assignments = objects(payload["assignments"], item.key)
            matches = objects(payload["matches"], item.key)
            if payload["split"] or any(
                p["field"] not in supported_fields for p in matches
            ):
                findings.append(Finding(FindingCode.RULE, revision, blocking=True))
                continue
            rules.append(
                Rule(
                    _key(item.key),
                    revision,
                    integer(payload["order"], item.key),
                    validity,
                    tuple(_predicate(p, match_definitions) for p in matches),
                    tuple(_rule_assignment(assignment) for assignment in assignments),
                )
            )
        elif item.kind in {"identity", "correction", "link", "ordering", "booking"}:
            findings.append(Finding(FindingCode.IDENTITY, revision, blocking=True))
    _require_rule_orders(rules)
    return (
        tuple(bindings),
        tuple(positions),
        tuple(symbols),
        tuple(rules),
        tuple(findings),
    )


def _observation(rows: Rows, record: Mapping[str, object]) -> None:
    payload = document(record["payload"])
    identifier = text(record["source_record_id"], "source_record")
    subject = payload["subject_source_record_id"] or identifier
    rows.add(
        "observations",
        {
            "observation_id": projection_key(
                "observation", identifier, payload["field_name"]
            ),
            "record_kind": "source_records",
            "record_id": f'["{subject}"]',
            "field_name": payload["field_name"],
            "observation_kind": payload["observation_kind"],
            "value_type": payload["value_type"],
            "decimal_value": exact(payload["decimal_value"])
            if payload["decimal_value"] is not None
            else None,
            "text_value": payload["text_value"],
            "date_value": payload["date_value"],
            "source_record_id": identifier,
        },
        evidence=None,
    )


def _source_assertion(  # noqa: PLR0913 - assertion context and output grain
    rows: Rows,
    project: Project,
    inputs: Inputs,
    record: Mapping[str, object],
    *,
    declarations: Declarations,
    statement: str | None,
    scope: str,
) -> tuple[tuple[Finding, ...], tuple[BalanceEvidence, ...]]:
    payload = document(record["payload"])
    source_id = RecordId(
        project.entity,
        RecordKind.SOURCE_RECORD,
        text(record["source_record_id"], "source_record"),
    )
    try:
        source_date = date.fromisoformat(text(payload["timestamp_date"], "time"))
        account, zone, _ = _source_civil_context(
            declarations,
            scope=_key(scope),
            section=_key(record["source_section"]),
            time=payload,
        )
        time = model_boundary(payload, point=True, civil_zone=zone)
    except (ContractError, ValueError) as error:
        return (Finding(_source_temporal_finding(error), source_id, blocking=True),), ()
    scopes = tuple(
        (item, declaration_payload)
        for item, declaration_payload in declarations
        if item.kind == "assertion_scope"
        and declaration_payload["account_id"] == account.value
        and Validity(item.valid_from, item.valid_to).applies(time.date)
        and declaration_payload["measurement"] == payload["measurement"]
        and declaration_payload["scope_kind"] == payload["scope_kind"]
    )
    if len(scopes) != 1:
        return (
            Finding(
                FindingCode.SCOPE_AMBIGUOUS if scopes else FindingCode.SCOPE,
                source_id,
                blocking=True,
            ),
        ), ()
    assertion_scope, scope_payload = scopes[0]
    assertion_id = projection_key("assertion", source_id.value)
    extracted = ("source_record", source_id.value, "extracted", None)
    fields = {
        "assertion_set_id": assertion_id,
        "scope_id": scope_payload["scope_id"],
        "date": (
            date.fromisoformat(text(payload["date"], "assertion_date"))
            + (time.date - source_date)
        ).isoformat(),
        "assertion_kind": payload["assertion_kind"],
        "is_complete": payload["is_complete"],
        "statement_id": statement,
        "timestamp_date": time.date.isoformat(),
        "timestamp_local": time.local,
        "timestamp_zone": time.zone,
        "timestamp_precision": time.precision.value,
        "timestamp_fraction_digits": time.fraction_digits,
        "timestamp_offset_minutes": time.offset_minutes,
    }
    rows.add(
        "balance_assertions",
        fields,
        evidence={
            key: (("declaration", assertion_scope.revision, "declaration", None),)
            if key == "scope_id"
            else (extracted,)
            for key in fields
        },
    )
    findings = []
    balances = []
    for observation in objects(payload["observations"], "assertion"):
        token = text(observation["commodity_token"], "assertion")
        location = resolve_source_location(
            inputs,
            scope=SemanticKey(scope),
            section=_key(record["source_section"]),
            when=time.date,
            token=token,
            source_position_key=_nullable_key(observation["source_position_key"]),
        )
        if isinstance(location, FindingCode):
            findings.append(Finding(location, source_id, blocking=True))
            continue
        commodity = location.commodity
        candidates = location.positions
        if scope_payload["scope_kind"] == "positions" and len(candidates) != 1:
            findings.append(
                Finding(FindingCode.POSITION_AMBIGUOUS, source_id, blocking=True)
            )
            continue
        balances.append(
            BalanceEvidence(
                source_id,
                _key(observation["observation_key"]),
                account,
                candidates[0] if scope_payload["scope_kind"] == "positions" else None,
                commodity,
                exact(observation["amount"]),
            )
        )
        rows.add(
            "balances",
            {
                "balance_id": projection_key(
                    "balance", source_id.value, observation["observation_key"]
                ),
                "assertion_set_id": assertion_id,
                "position_id": next(iter(candidates)).value
                if scope_payload["scope_kind"] == "positions"
                else None,
                "commodity_id": commodity.value,
                "amount": exact(observation["amount"]),
            },
            evidence=extracted,
        )
    return tuple(findings), tuple(balances)


def _initial_inputs(
    project: Project, declarations: Declarations
) -> tuple[Inputs, list[Finding]]:
    bindings, positions, symbols, rules, policy_findings = _policies(
        project, declarations
    )
    configuration = project.configuration_document()
    ignored = tuple(
        IgnoredSection(
            _key(s["source_scope_id"]),
            _key(s["source_section"]),
            text(s["reason"], "ignore"),
        )
        for s in objects(configuration.get("ignored_sections", []), "ignored_sections")
    )
    findings = list(policy_findings)
    bases = {
        text(basis["basis_id"], "basis_id"): basis
        for basis in objects(
            document(decode_document(project.registries, "registries"))[
                "coverage_bases"
            ],
            "bases",
        )
    }
    authorities = []
    for authority in objects(configuration["source_authority"], "authority"):
        basis_key = text(authority["coverage_basis_id"], "authority_basis")
        require(basis_key in bases, "authority_basis", "source_authority")
        authorities.append(
            SourceAuthority(
                _key(authority["account_id"]),
                FactKind(text(authority["fact_kind"], "authority_fact_kind")),
                _key(basis_key),
                TimeRole(text(bases[basis_key]["date_role"], "basis_date_role")),
                Validity(
                    date.fromisoformat(text(authority["period_start"], "start")),
                    date.fromisoformat(text(authority["period_end"], "end")),
                ),
                _key(authority["source_scope_id"]),
                AuthorityMode(text(authority["mode"], "authority_mode")),
            )
        )
    inputs = Inputs(
        project.entity,
        date.fromisoformat(text(configuration["as_of"], "as_of")),
        bindings,
        ignored,
        symbols,
        positions,
        rules,
        (),
        (),
        (),
        (),
        authorities=tuple(authorities),
    )
    return inputs, findings


def _source_section(
    inputs: Inputs,
    rows: Rows,
    extract: Mapping[str, object],
    section: Mapping[str, object],
    declarations: Declarations,
) -> tuple[str | None, list[Finding]]:
    scope, section_id = _key(extract["source_scope_id"]), _key(section["section_id"])
    finding_scope = source_section_key(scope, section_id)
    if any(s.scope == scope and s.section == section_id for s in inputs.ignored):
        return None, []
    findings: list[Finding] = []
    try:
        parts = objects(section["evidence_parts"], "parts")
        if not parts:
            return None, findings
        anchor = document(
            parts[0]["start"]
            if parts[0]["kind"] == "movement_interval"
            else parts[0]["point"]
        )
        account, zone, _ = _source_civil_context(
            declarations,
            scope=scope,
            section=section_id,
            time=anchor,
        )
        interval = _movement_interval(section, record_locator=None, civil_zone=zone)
    except (ContractError, ValueError) as error:
        return None, [
            *findings,
            Finding(
                _source_temporal_finding(error),
                finding_scope,
                blocking=True,
            ),
        ]
    if interval is None:
        try:
            points = tuple(
                model_time(document(part["point"]), civil_zone=zone).date
                for part in objects(section["evidence_parts"], "parts")
                if "point" in part
            )
        except ContractError:
            return None, [Finding(FindingCode.TIME, finding_scope, blocking=True)]
        if not points:
            return None, findings
        when = min(points)
        start, end = None, None
    else:
        start, end = interval.start, interval.end
        if start is None or end is None:
            code = "source_interval_bounds_required"
            raise ContractError(code, "extract")
        when = start
    statement_id = projection_key(
        "statement", extract["source_blob_digest"], scope.value, section_id.value
    )
    scopes = tuple(
        payload
        for d, payload in declarations
        if d.kind == "assertion_scope"
        and payload["account_id"] == account.value
        and d.applies(when)
    )
    rows.add(
        "statements",
        {
            "statement_id": statement_id,
            "account_id": account.value,
            "institution": section["institution"],
            "source_class": section["source_class"],
            "period_start": start.isoformat() if start is not None else None,
            "period_end": (end - timedelta(days=1)).isoformat()
            if end is not None
            else None,
            "stated_currency": section["stated_currency"],
            "importer_id": extract["importer_id"],
            "is_eclipsed": False,
            "coverage_basis": scopes[0]["coverage_basis"] if len(scopes) == 1 else None,
        },
        evidence=None,
    )
    return statement_id, findings


def _source_anchors(
    record: Mapping[str, object], extract: Mapping[str, object]
) -> tuple[AcquiredAnchor, SourceKeyAnchor | None]:
    anchor = AcquiredAnchor(
        text(extract["source_blob_digest"], "blob"),
        _key(extract["source_scope_id"]),
        _key(record["source_section"]),
        _key(record["record_locator"]),
    )
    key = (
        SourceKeyAnchor(
            anchor.namespace,
            anchor.section,
            _key(record["source_key_domain"]),
            _key(record["source_key"]),
        )
        if record["source_key"] is not None
        else None
    )
    return anchor, key


def _source_legs(
    source_id: RecordId, payload: Mapping[str, object]
) -> tuple[tuple[SourceLeg, ...], list[Finding]]:
    findings: list[Finding] = []
    legs: list[SourceLeg] = []
    for leg in objects(payload["legs"], "legs"):
        if leg["components"] or any(
            leg[field] is not None
            for field in (
                "price_per_unit",
                "original_amount",
                "broker_lot_id",
            )
        ):
            findings.append(Finding(FindingCode.CONTENT, source_id, blocking=True))
        legs.append(
            SourceLeg(
                _key(leg["posting_key"]),
                text(leg["commodity_token"], "commodity"),
                exact(leg["amount"]),
                _nullable_key(leg["source_position_key"]),
                original_symbol=nullable_text(
                    leg["original_commodity_token"], "original_commodity"
                ),
                fx_rate=exact(leg["fx_rate_as_stated"])
                if leg["fx_rate_as_stated"] is not None
                else None,
                fx_base_symbol=nullable_text(
                    leg["fx_base_commodity_token"], "fx_base_commodity"
                ),
                fx_quote_symbol=nullable_text(
                    leg["fx_quote_commodity_token"], "fx_quote_commodity"
                ),
            )
        )
    return tuple(legs), findings


def _source_text(
    observations: Mapping[tuple[str | None, str], tuple[object, ...]],
    identifier: str,
    field: str,
) -> str | None:
    values = observations.get((identifier, field), ())
    require(len(values) <= 1, "source_observation_ambiguous", field)
    return nullable_text(values[0], field) if values else None


def source_role_time(
    payload: Mapping[str, object], role: TimeRole
) -> Mapping[str, object]:
    """Prefer a role's exact observation to its unqualified civil date."""
    for observation in objects(payload["times"], "times"):
        if observation["date_role"] == role.value:
            return observation
    return {
        "timestamp_date": payload.get(f"date_{role.value}"),
        "timestamp_local": None,
        "timestamp_offset_seconds": None,
        "timestamp_zone": None,
        "timestamp_precision": "date",
        "timestamp_fraction_digits": None,
    }


def _movement_times(
    declarations: Declarations,
    source_id: RecordId,
    anchor: AcquiredAnchor,
    payload: Mapping[str, object],
    required: frozenset[TimeRole],
) -> tuple[dict[TimeRole, TimeObservation], list[Finding]]:
    times = {}
    findings = []
    roles = {
        TimeRole(text(observation["date_role"], "date_role"))
        for observation in objects(payload["times"], "times")
    } | {TimeRole(role) for role in DATE_ROLES if payload[f"date_{role}"] is not None}
    for role in sorted(roles):
        try:
            _, _, converted = _source_civil_context(
                declarations,
                scope=anchor.namespace,
                section=anchor.section,
                time=source_role_time(payload, role),
            )
        except ContractError as error:
            findings.append(
                Finding(
                    _source_temporal_finding(error),
                    source_id,
                    blocking=role in required,
                )
            )
        else:
            times[role] = converted
    return times, findings


def _source_movement(  # noqa: PLR0913 - decoded source identity, facts, and context
    source_id: RecordId,
    record: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    extract: Mapping[str, object],
    section: Mapping[str, object],
    observations: Mapping[tuple[str | None, str], tuple[object, ...]],
    declarations: Declarations,
) -> tuple[RecordId, Movement | None, list[Finding]]:
    identifier = source_id.value
    anchor, source_key = _source_anchors(record, extract)
    event_key = _key(record["event_key"])
    identity = event_id(source_id.entity, Occurrence(source_key or anchor, event_key))
    status = tuple(
        m
        for m in objects(extract["status_mappings"], "status")
        if m["source_field"] == "source.status"
        and m["source_token"] == payload["status_token"]
        and m["event_key"] == record["event_key"]
    )
    if len(status) != 1 or status[0]["candidate_state"] not in EventState:
        return identity, None, [Finding(FindingCode.CONTENT, source_id, blocking=True)]
    state = EventState(text(status[0]["candidate_state"], "candidate_state"))
    observations_by_role = {
        TimeRole(text(t["date_role"], "date_role")): t
        for t in objects(payload["times"], "times")
    }
    role_order = (
        (TimeRole.INITIATED, TimeRole.AUTHORIZED)
        if state == EventState.PENDING
        else (TimeRole.POSTED,)
    )
    primary = next(
        (
            role
            for role in role_order
            if role in observations_by_role
            or payload.get(f"date_{role.value}") is not None
        ),
        role_order[0],
    )
    try:
        _, zone, primary_time = _source_civil_context(
            declarations,
            scope=anchor.namespace,
            section=anchor.section,
            time=source_role_time(payload, primary),
        )
        required = frozenset(
            {primary}
            | {
                TimeRole(text(part["date_role"], "date_role"))
                for part in objects(section["evidence_parts"], "parts")
                if part["kind"] == "movement_interval"
                and record["record_locator"]
                in strings(part["record_locators"], "locators")
            }
        )
        role_times, time_findings = _movement_times(
            declarations,
            source_id,
            anchor,
            payload,
            required,
        )
        if any(finding.blocking for finding in time_findings):
            return identity, None, time_findings
        dates = tuple(
            role_times[TimeRole(role)].date if TimeRole(role) in role_times else None
            for role in DATE_ROLES
        )
        interval = _movement_interval(
            section,
            record_locator=text(record["record_locator"], "record_locator"),
            civil_zone=zone,
        )
    except (ContractError, ValueError) as error:
        return (
            identity,
            None,
            [Finding(_source_temporal_finding(error), source_id, blocking=True)],
        )
    legs, findings = _source_legs(source_id, payload)
    findings.extend(time_findings)
    txn_type = _source_text(observations, identifier, "source.trntype")
    return (
        identity,
        Movement(
            source_id,
            anchor,
            source_key,
            event_key,
            primary_time.date,
            state,
            nullable_text(payload["payee_as_stated"], "payee"),
            _source_text(observations, identifier, "source.memo"),
            legs,
            txn_type in {"XFER", "TRANSFER"},
            dates,
            tuple(
                (role, time)
                for role, time in sorted(role_times.items())
                if role in observations_by_role
            ),
            txn_type,
            interval,
            effective_role=primary,
            description=nullable_text(payload["description_as_stated"], "description"),
        ),
        findings,
    )


def _prepare_sources(  # noqa: C901, PLR0912, PLR0915 - one retained-evidence traversal
    project: Project,
    inputs: Inputs,
    rows: Rows,
    extracts: tuple[ExtractInput, ...],
    declarations: Declarations,
) -> tuple[
    list[Movement],
    list[Finding],
    dict[RecordId, Mapping[str, object]],
    dict[RecordId, RecordId],
    list[Statement],
    list[SourceRevision],
    list[SourceStatementRevision],
]:
    findings: list[Finding] = []
    movements: list[Movement] = []
    transaction_fields: dict[RecordId, Mapping[str, object]] = {}
    occurrence_ids: dict[RecordId, RecordId] = {}
    model_statements: list[Statement] = []
    source_revisions: list[SourceRevision] = []
    balance_facts: dict[RecordId, tuple[BalanceEvidence, ...]] = {}
    statement_revisions: list[SourceStatementRevision] = []
    bases = objects(
        document(decode_document(project.registries, "registries"))["coverage_bases"],
        "bases",
    )
    status_profiles: dict[tuple[str, str, str, str], str] = {}
    seen_payloads = set()
    ignored_sections: dict[tuple[str, str], str] = {}
    definitions = rule_match_fields(
        tuple(
            objects(
                document(decode_document(project.registries, "registries"))[
                    "observation_fields"
                ],
                "observation_fields",
            )
        )
    )
    source_match_fields = {
        predicate.field.value
        for rule in inputs.rules
        for predicate in rule.predicates
        if predicate.field.value not in MATCH_FIELDS
    }
    for item in inputs.ignored:
        ignored_sections.setdefault((item.scope.value, item.section.value), item.reason)
    for selected in extracts:
        if selected.payload in seen_payloads:
            continue
        seen_payloads.add(selected.payload)
        extract = document(decode_document(selected.payload, "extract"))
        validate_extraction(extract)
        scope = text(extract["source_scope_id"], "scope")
        blob = text(extract["source_blob_digest"], "blob")
        records = objects(extract["records"], "records")
        for mapping in objects(extract["status_mappings"], "status_mappings"):
            key = (
                scope,
                text(mapping["source_field"], "field"),
                text(mapping["source_token"], "token"),
                text(mapping["event_key"], "event_key"),
            )
            state = text(mapping["candidate_state"], "candidate_state")
            if key in status_profiles and status_profiles[key] != state:
                findings.append(
                    Finding(FindingCode.CONTENT, _key(scope), blocking=True)
                )
            status_profiles[key] = state
        observations: dict[tuple[str | None, str], tuple[object, ...]] = {}
        match_observations: dict[str, dict[SemanticKey, MatchValue]] = {}
        for record in records:
            if record["kind"] == "observation":
                payload = document(record["payload"])
                key = (
                    nullable_text(
                        payload["subject_source_record_id"], "observation.subject"
                    ),
                    text(payload["field_name"], "observation.field"),
                )
                observations[key] = (*observations.get(key, ()), payload["text_value"])
                subject, field = key
                if subject is not None and field in source_match_fields:
                    definition = definitions[field]
                    value_field = {
                        "string": "text_value",
                        "decimal": "decimal_value",
                        "date": "date_value",
                    }[definition["value_type"]]
                    require(
                        payload["value_type"]
                        == {"string": "text", "decimal": "decimal", "date": "date"}[
                            definition["value_type"]
                        ],
                        "rule_observation_type",
                        field,
                    )
                    require(
                        payload[value_field] is not None,
                        "rule_observation_value",
                        field,
                    )
                    values = match_observations.setdefault(subject, {})
                    require(
                        _key(field) not in values, "source_observation_ambiguous", field
                    )
                    values[_key(field)] = _match_value(payload[value_field], definition)
        sections = {
            text(s["section_id"], "section_id"): s
            for s in objects(document(extract["source_scope"])["sections"], "sections")
        }
        statements: dict[str, str] = {}
        for section in sections.values():
            statement_id, section_findings = _source_section(
                inputs, rows, extract, section, declarations
            )
            findings.extend(section_findings)
            if statement_id is not None:
                statements[text(section["section_id"], "section_id")] = statement_id
                statement_key = _key(statement_id)
                account = _key(
                    next(
                        row["account_id"]
                        for row in rows.tables["statements"]
                        if row["statement_id"] == statement_id
                    )
                )
                parts = []
                for part in objects(section["evidence_parts"], "parts"):
                    if part["kind"] not in {
                        "movement_interval",
                        "balance_point",
                        "available_balance",
                    }:
                        continue
                    boundary = document(
                        part["start"]
                        if part["kind"] == "movement_interval"
                        else part["point"]
                    )
                    try:
                        _, zone, _ = _source_civil_context(
                            declarations,
                            scope=_key(scope),
                            section=_key(section["section_id"]),
                            time=boundary,
                        )
                        point = (
                            model_time(document(part["point"]), civil_zone=zone)
                            if part["kind"] != "movement_interval"
                            else None
                        )
                        interval = (
                            Validity(
                                model_boundary(
                                    document(part["start"]),
                                    point=False,
                                    civil_zone=zone,
                                ).date,
                                model_boundary(
                                    document(part["end"]), point=False, civil_zone=zone
                                ).date,
                            )
                            if point is None
                            else Validity(point.date, point.date + timedelta(days=1))
                        )
                    except ContractError:
                        findings.append(
                            Finding(
                                FindingCode.TIME,
                                source_section_key(
                                    _key(scope), _key(section["section_id"])
                                ),
                                blocking=True,
                            )
                        )
                        continue
                    locators = strings(part["record_locators"], "locators")
                    for basis in bases:
                        if (
                            (point is None and basis["date_role"] != part["date_role"])
                            or basis["measurement"] != "units"
                            or basis["balance_view"]
                            != (
                                "available"
                                if part["kind"] == "available_balance"
                                else "settled"
                            )
                        ):
                            continue
                        parts.append(
                            StatementPart(
                                _key(part["part_key"]),
                                FactKind.MOVEMENT
                                if point is None
                                else FactKind.BALANCE,
                                _key(basis["basis_id"]),
                                TimeRole(text(basis["date_role"], "role")),
                                interval,
                                part["completeness"] == "complete",
                                tuple(
                                    RecordId(
                                        project.entity,
                                        RecordKind.SOURCE_RECORD,
                                        text(
                                            record["source_record_id"], "source_record"
                                        ),
                                    )
                                    for record in records
                                    if record["source_section"] == section["section_id"]
                                    and record["record_locator"] in locators
                                    and record["kind"]
                                    == ("transaction" if point is None else "assertion")
                                ),
                                point=point,
                            )
                        )
                model_statements.append(
                    Statement(
                        statement_key,
                        _key(scope),
                        _key(section["section_id"]),
                        blob,
                        account,
                        tuple(parts),
                        source_class=SourceClass(
                            text(section["source_class"], "source_class")
                        ),
                    )
                )
                statement_revisions.extend(
                    SourceStatementRevision(
                        statement_key,
                        text(revision["source_blob_digest"], "revision_blob"),
                        revision["relationship"] == "replaces",
                    )
                    for revision in objects(section["revisions"], "revisions")
                )
        unique_sources = set()
        for record in records:
            identifier = text(record["source_record_id"], "source_record")
            if identifier not in unique_sources:
                unique_sources.add(identifier)
                rows.add(
                    "source_records",
                    {
                        "source_record_id": identifier,
                        "source_blob_digest": blob,
                        "source_scope_id": scope,
                        "source_section": record["source_section"],
                        "record_locator": record["record_locator"],
                        "statement_id": statements.get(
                            text(record["source_section"], "source_section")
                        ),
                        "input_kind": "bank_statement",
                        "external_record_id": record["external_record_id"],
                        "source_key_domain": record["source_key_domain"],
                        "source_key": record["source_key"],
                        "ignored_reason": ignored_sections.get(
                            (scope, text(record["source_section"], "source_section"))
                        ),
                    },
                    evidence=None,
                )
            if record["kind"] == "observation":
                _observation(rows, record)
                continue
            if (scope, record["source_section"]) in ignored_sections:
                continue
            source_id = RecordId(project.entity, RecordKind.SOURCE_RECORD, identifier)
            if record["source_correction"] is not None:
                correction = document(record["source_correction"])
                source_revisions.append(
                    SourceRevision(
                        source_id,
                        SourceKeyAnchor(
                            _key(scope),
                            _key(record["source_section"]),
                            _key(correction["source_key_domain"]),
                            _key(correction["target_source_key"]),
                        ),
                        RevisionAction(text(correction["action"], "revision_action")),
                    )
                )
            if record["kind"] == "assertion":
                assertion_findings, assertion_balances = _source_assertion(
                    rows,
                    project,
                    inputs,
                    record,
                    declarations=declarations,
                    statement=statements.get(
                        text(record["source_section"], "source_section")
                    ),
                    scope=scope,
                )
                findings.extend(assertion_findings)
                balance_facts[source_id] = assertion_balances
                continue
            if record["kind"] != "transaction":
                findings.append(Finding(FindingCode.CONTENT, source_id, blocking=True))
                continue
            payload = document(record["payload"])
            transaction_fields[source_id] = {
                "description_as_stated": payload["description_as_stated"]
            }
            identity, movement, movement_findings = _source_movement(
                source_id,
                record,
                payload,
                extract=extract,
                section=sections[text(record["source_section"], "source_section")],
                observations=observations,
                declarations=declarations,
            )
            if movement is not None:
                movements.append(
                    replace(
                        movement,
                        observations=tuple(
                            sorted(
                                match_observations.get(identifier, {}).items(),
                                key=lambda item: item[0].value,
                            )
                        ),
                        statement=_nullable_key(
                            statements.get(text(record["source_section"], "section"))
                        ),
                    )
                )
            occurrence_ids[source_id] = identity
            findings.extend(movement_findings)
        findings.extend(
            Finding(
                FindingCode.CONTENT,
                source_section_key(_key(scope), _key(unsupported["source_section"])),
                blocking=True,
            )
            for unsupported in objects(extract["unsupported_content"], "unsupported")
        )
    conflicting_scopes = {
        finding.scope
        for finding in findings
        if finding.code == FindingCode.CONTENT
        and isinstance(finding.scope, SemanticKey)
    }
    findings.extend(
        Finding(FindingCode.CONTENT, movement.source_record, blocking=True)
        for movement in movements
        if movement.anchor.namespace in conflicting_scopes
    )
    model_statements = [
        replace(
            statement,
            parts=tuple(
                replace(
                    part,
                    balances=tuple(
                        balance
                        for record in part.source_records
                        for balance in balance_facts.get(record, ())
                    ),
                )
                for part in statement.parts
            ),
        )
        for statement in model_statements
    ]
    return (
        movements,
        findings,
        transaction_fields,
        occurrence_ids,
        model_statements,
        source_revisions,
        statement_revisions,
    )


def _authored_assertion(
    rows: Rows, item: Declaration, record: Mapping[str, object]
) -> None:
    assertion = {k: v for k, v in record.items() if k != "observations"}
    assertion["assertion_set_id"] = projection_key("authored-assertion", item.key)
    rows.add(
        "balance_assertions",
        assertion,
        evidence=("declaration", item.revision, "declaration", None),
    )
    for observation in objects(record["observations"], item.key):
        rows.add(
            "balances",
            {
                **{
                    k: v
                    for k, v in observation.items()
                    if k not in {"observation_key", "amount"}
                },
                "balance_id": projection_key(
                    "authored-balance", item.key, observation["observation_key"]
                ),
                "assertion_set_id": assertion["assertion_set_id"],
                "amount": exact(observation["amount"]),
            },
            evidence=("declaration", item.revision, "declaration", None),
        )


def _authored_effective_role(record: Mapping[str, object]) -> TimeRole:
    if record["event_state"] == EventState.RECOGNIZED:
        return TimeRole.POSTED
    if record["event_state"] == EventState.PENDING:
        return TimeRole.AUTHORIZED
    roles = tuple(
        role
        for role in (TimeRole.POSTED, TimeRole.AUTHORIZED)
        if record[f"date_{role.value}"] is not None
    )
    require(len(roles) == 1, "unique_authored_effective_role_required", "authored_fact")
    return roles[0]


def _prepare_authored(
    project: Project, inputs: Inputs, rows: Rows, declarations: Declarations
) -> tuple[
    list[Event],
    list[Finding],
    dict[RecordId, Mapping[str, object]],
    dict[RecordId, Mapping[SemanticKey, Mapping[str, object]]],
    list[RecordId],
]:
    authored: list[Event] = []
    findings: list[Finding] = []
    transaction_fields: dict[RecordId, Mapping[str, object]] = {}
    posting_fields: dict[RecordId, Mapping[SemanticKey, Mapping[str, object]]] = {}
    authored_blocked: list[RecordId] = []
    retained_ids = {row["source_record_id"] for row in rows.tables["source_records"]}
    for item, payload in declarations:
        if item.kind != "authored_fact" or not item.applies(inputs.horizon):
            continue
        record = document(payload["record"])
        if payload["fact_kind"] == "note":
            occurrence = Occurrence(
                AuthoredAnchor(_key(item.key)), _key(payload["event_key"])
            )
            identity = event_id(project.entity, occurrence)
            when = date.fromisoformat(text(record["date"], item.key))
            authored.append(
                Event(
                    identity,
                    occurrence,
                    when,
                    EventKind.NOTE,
                    EventState.RECOGNIZED,
                    ReviewState.UNREVIEWED,
                    None,
                    nullable_text(record["narration"], "narration"),
                    (),
                    (),
                    _revision(project, item),
                    effective_role=TimeRole.POSTED,
                    times=(
                        (
                            TimeRole.OCCURRED,
                            TimeObservation(
                                date.fromisoformat(
                                    text(record["timestamp_date"], "timestamp_date")
                                ),
                                nullable_text(
                                    record["timestamp_local"], "timestamp_local"
                                ),
                                nullable_integer(
                                    record["timestamp_offset_minutes"],
                                    "timestamp_offset",
                                ),
                                nullable_text(
                                    record["timestamp_zone"], "timestamp_zone"
                                ),
                                Precision(
                                    text(
                                        record["timestamp_precision"],
                                        "timestamp_precision",
                                    )
                                ),
                                nullable_integer(
                                    record["timestamp_fraction_digits"],
                                    "timestamp_fraction",
                                ),
                            ),
                        ),
                    )
                    if record.get("timestamp_date") is not None
                    else (),
                )
            )
            continue
        if payload["fact_kind"] == "assertion":
            _authored_assertion(rows, item, record)
            continue
        if payload["fact_kind"] not in {"transaction", "note"}:
            findings.append(
                Finding(FindingCode.CONTENT, _revision(project, item), blocking=True)
            )
            continue
        occurrence = Occurrence(
            AuthoredAnchor(_key(item.key)), _key(payload["event_key"])
        )
        identity = event_id(project.entity, occurrence)
        if (
            record["event_kind"] not in EventKind
            or record["event_state"] not in EventState
        ):
            findings.append(
                Finding(FindingCode.CONTENT, _revision(project, item), blocking=True)
            )
            authored_blocked.append(identity)
            continue
        stated_legs = objects(record["legs"], item.key)
        if any(
            leg[field] is not None
            for leg in stated_legs
            for field in (
                "price_per_unit",
                "original_amount",
                "fx_rate_as_stated",
                "broker_lot_id",
            )
        ):
            findings.append(
                Finding(FindingCode.CONTENT, _revision(project, item), blocking=True)
            )
            authored_blocked.append(identity)
            continue
        evidence = objects(payload["evidence"], item.key)
        if record["event_kind"] == "opening":
            require(
                any(
                    e["kind"] == "source_record" and e["id"] in retained_ids
                    for e in evidence
                ),
                "opening_retained_evidence_required",
                item.key,
            )
        try:
            effective_role = _authored_effective_role(record)
            when = date.fromisoformat(
                text(record[f"date_{effective_role.value}"], item.key)
            )
        except ContractError, ValueError:
            findings.append(
                Finding(FindingCode.DATE, _revision(project, item), blocking=True)
            )
            authored_blocked.append(identity)
            continue
        transaction_fields[identity] = {
            "description_as_stated": record["description_as_stated"],
            "links": record["links"],
            "tags": record["tags"],
        }
        legs = tuple(
            Leg(
                _key(leg["posting_key"]),
                LegKind(text(leg["leg_kind"], "leg_kind")),
                _key(leg["commodity_id"]),
                exact(leg["amount"]),
                _nullable_key(leg["account_id"]),
                _nullable_key(leg["position_id"]),
                _nullable_key(leg["category_id"]),
                nullable_text(leg["symbol_as_stated"], "symbol_as_stated"),
                _revision(project, item),
                dates=tuple(
                    LegDate(
                        DateMode(text(leg[f"date_{role}_mode"], "date_mode")),
                        date.fromisoformat(text(leg[f"date_{role}"], "date"))
                        if leg[f"date_{role}"] is not None
                        else None,
                    )
                    for role in DATE_ROLES
                ),
                original_commodity=_nullable_key(leg["original_commodity_id"]),
                fx_rate=exact(leg["fx_rate_as_stated"])
                if leg["fx_rate_as_stated"] is not None
                else None,
                fx_base_commodity=_nullable_key(leg["fx_base_commodity_id"]),
                fx_quote_commodity=_nullable_key(leg["fx_quote_commodity_id"]),
            )
            for leg in stated_legs
        )
        posting_fields[identity] = {
            leg.key: {
                field: stated[field]
                for field in (
                    "posting_role",
                    "broker_lot_id",
                    "price_per_unit",
                    "price_commodity_id",
                    "original_amount",
                )
            }
            for leg, stated in zip(legs, stated_legs, strict=True)
        }
        authored.append(
            Event(
                identity,
                occurrence,
                when,
                EventKind(text(record["event_kind"], "event_kind")),
                EventState(text(record["event_state"], "event_state")),
                ReviewState(text(record["review_state"], "review_state")),
                nullable_text(record["payee"], "payee"),
                nullable_text(record["narration"], "narration"),
                legs,
                (),
                _revision(project, item),
                effective_role=effective_role,
                dates=tuple(
                    date.fromisoformat(text(record[f"date_{role}"], "date"))
                    if record[f"date_{role}"] is not None
                    else None
                    for role in DATE_ROLES
                ),
                tags=strings(record["tags"], "tags"),
                description=nullable_text(
                    record["description_as_stated"], "description"
                ),
            )
        )
    return authored, findings, transaction_fields, posting_fields, authored_blocked


def prepare(project: Project, extracts: tuple[ExtractInput, ...]) -> Prepared:
    """Validate wire documents once, then construct immutable semantic inputs."""
    declarations = tuple((item, item.decode()) for item in project.declarations)
    inputs, findings = _initial_inputs(project, declarations)
    rows = Rows(project.entity.value)
    _dimensions(rows, declarations, horizon=inputs.horizon)
    (
        movements,
        source_findings,
        source_fields,
        occurrence_ids,
        statements,
        source_revisions,
        statement_revisions,
    ) = _prepare_sources(project, inputs, rows, extracts, declarations)
    authored, authored_findings, authored_fields, posting_fields, authored_blocked = (
        _prepare_authored(project, inputs, rows, declarations)
    )
    interpreted_sources = {m.source_record for m in movements}
    return Prepared(
        replace(
            inputs,
            movements=tuple(movements),
            authored=tuple(authored),
            findings=(*findings, *source_findings, *authored_findings),
            blocked=(
                *authored_blocked,
                *(
                    identity
                    for source_id, identity in occurrence_ids.items()
                    if source_id not in interpreted_sources
                ),
            ),
            categories=tuple(
                _key(row["category_id"]) for row in rows.tables["categories"]
            ),
            statements=tuple(statements),
            source_revisions=tuple(source_revisions),
            source_statement_revisions=tuple(statement_revisions),
        ),
        rows,
        {**source_fields, **authored_fields},
        posting_fields,
    )


def _project_event_times(
    rows: Rows,
    event: Event,
    *,
    evidence: Evidence,
) -> None:
    for role, time in event.times:
        rows.add(
            "event_times",
            {
                "record_kind": "transactions",
                "record_id": event.identity.value,
                "date_role": role.value,
                "time_mode": "value",
                "timestamp_date": time.date.isoformat(),
                "timestamp_local": time.local,
                "timestamp_offset_minutes": time.offset_minutes,
                "timestamp_zone": time.zone,
                "timestamp_precision": time.precision.value,
                "timestamp_fraction_digits": time.fraction_digits,
            },
            evidence=evidence,
        )


def _project_transaction(
    rows: Rows,
    event: Event,
    *,
    retained: Mapping[str, object],
    evidence: Evidence,
) -> None:
    fields = {
        "description_as_stated": event.description,
        "links": [],
        **retained,
        "txn_id": event.identity.value,
        "event_kind": event.kind.value,
        "event_state": event.state.value,
        "payee": event.payee,
        "narration": event.narration,
        "review_state": event.review.value,
        "tags": list(event.tags),
    }
    fields.update(
        {
            f"date_{role}": value.isoformat() if value else None
            for role, value in zip(DATE_ROLES, event_dates(event), strict=True)
        }
    )
    corrections = dict(event.corrections)
    rules: dict[str, tuple[RecordId, ...]] = {
        field.value.split(".")[1]: revisions for field, revisions in event.rules
    }
    transaction_evidence = {
        key: (
            (
                "declaration",
                corrections[
                    key.removeprefix("date_") + "_date"
                    if key.startswith("date_")
                    else key
                ].value,
                "correction",
                None,
            ),
        )
        if (key.removeprefix("date_") + "_date" if key.startswith("date_") else key)
        in corrections
        else tuple(
            ("declaration", revision.value, "rule", "enrichment-rule-v1")
            for revision in rules[key]
        )
        + ((evidence,) if key == "tags" and bool(retained.get("tags")) else ())
        if key in rules
        else (evidence,)
        for key in fields
    }
    rows.add("transactions", fields, evidence=transaction_evidence)
    _project_event_times(rows, event, evidence=evidence)


def _retained_record(event: Event) -> RecordId:
    if event.declaration is not None:
        return event.identity
    selected = tuple(
        support.source_record
        for support in event.support
        if support.kind == SupportKind.SELECTED
    )
    if event.state == EventState.RETRACTED:
        require(
            bool(event.support),
            "retracted_source_support",
            "projection",
        )
        # Retraction has no live selection; retain the deterministic historical facts.
        return min(
            event.support, key=lambda support: support.source_record.value
        ).source_record
    require(len(selected) == 1, "selected_source_required", "projection")
    return selected[0]


def _project_coverage(rows: Rows, inputs: Inputs) -> None:
    for statement in inputs.statements:
        for part in statement.parts:
            if part.fact_kind != FactKind.MOVEMENT or part.point is not None:
                continue
            for start, end, eclipsed in coverage_segments(inputs, statement, part):
                rows.add(
                    "statement_coverage",
                    {
                        "statement_id": statement.identity.value,
                        "part_key": part.key.value,
                        "coverage_basis": part.basis.value,
                        "date_role": part.date_role.value,
                        "period_start": start.isoformat(),
                        "period_end": end.isoformat(),
                        "is_complete": part.complete,
                        "is_eclipsed": eclipsed,
                    },
                    evidence=None,
                )


def project_resolution(  # noqa: C901 - catalog mapping with field-level provenance
    prepared: Prepared, resolution: Resolution
) -> dict[str, list[dict[str, object]]]:
    """Emit catalog grains from determinations while retaining source evidence."""
    rows = deepcopy(prepared.rows)
    _project_coverage(rows, prepared.inputs)
    dispositions = balance_dispositions(prepared.inputs)
    excluded = {projection_key("assertion", record.value) for record in dispositions}
    excluded_balances = {
        text(row["balance_id"], "balance_id")
        for row in rows.tables["balances"]
        if row["assertion_set_id"] in excluded
    }
    rows.tables["balance_assertions"] = [
        row
        for row in rows.tables["balance_assertions"]
        if row["assertion_set_id"] not in excluded
    ]
    rows.tables["balances"] = [
        row
        for row in rows.tables["balances"]
        if row["assertion_set_id"] not in excluded
    ]
    rows.tables["provenance"] = [
        row
        for row in rows.tables["provenance"]
        if not (
            row["record_kind"] in {"balance_assertions", "balances"}
            and any(
                identifier in text(row["record_id"], "record_id")
                for identifier in excluded | excluded_balances
            )
        )
    ]
    candidates = {
        m.source_record.value: m.when <= prepared.inputs.horizon
        for m in prepared.inputs.movements
    }
    for source in rows.tables["source_records"]:
        source_identifier = text(source["source_record_id"], "source_record_id")
        if source_identifier in candidates:
            source["model_eligible"] = candidates[source_identifier]
        elif source["ignored_reason"] is not None:
            source["model_eligible"] = False
    sequence = 0
    for event in sorted(
        resolution.events,
        key=lambda e: (e.when, e.kind != EventKind.OPENING, e.identity.value),
    ):
        identifier = event.identity.value
        retained_id = _retained_record(event)
        evidence: Evidence = (
            ("declaration", event.declaration.value, "declaration", None)
            if event.declaration is not None
            else (
                "source_record",
                retained_id.value,
                "extracted",
                None,
            )
        )
        derived: Evidence = (evidence[0], evidence[1], "booking", "quantity-v1")
        anchor = event.occurrence.anchor
        rows.add(
            "economic_identities",
            {
                "record_kind": "transaction",
                "record_id": identifier,
                "anchor_kind": "declaration"
                if isinstance(anchor, AuthoredAnchor)
                else "source_key"
                if isinstance(anchor, SourceKeyAnchor)
                else "source_record",
                "anchor_id": anchor.record_key.value
                if isinstance(anchor, AuthoredAnchor)
                else encode_json(
                    [
                        anchor.namespace.value,
                        anchor.section.value,
                        anchor.key_domain.value,
                        anchor.source_key.value,
                    ],
                    integer_strings=False,
                )
                .decode()
                .strip()
                if isinstance(anchor, SourceKeyAnchor)
                else source_record_digest(anchor),
                "event_key": event.occurrence.event_key.value,
                "identity_state": "retired"
                if event.identity in resolution.retired
                else "active",
                "decision_declaration_id": event.identity_decision.value
                if event.identity_decision is not None
                else None,
            },
            evidence=("declaration", event.identity_decision.value, "correction", None)
            if event.identity_decision is not None
            else None,
        )
        for support in () if event.identity in resolution.retired else event.support:
            rows.add(
                "event_support",
                {
                    "txn_id": identifier,
                    "source_record_id": support.source_record.value,
                    "event_key": event.occurrence.event_key.value,
                    "support_kind": support.kind.value,
                },
                evidence=("declaration", support.decision.value, "correction", None)
                if support.decision is not None
                else None,
            )
            if support.decision is not None:
                key = [
                    identifier,
                    support.source_record.value,
                    event.occurrence.event_key.value,
                ]
                rows.add(
                    "provenance",
                    {
                        "provenance_id": projection_key(
                            "event_support",
                            key,
                            "txn_id",
                            "declaration",
                            support.decision.value,
                        ),
                        "record_kind": "event_support",
                        "record_id": encode_json(key, integer_strings=False)
                        .decode()
                        .strip(),
                        "field_name": "txn_id",
                        "value_origin": "correction",
                        "evidence_kind": "declaration",
                        "evidence_id": support.decision.value,
                        "derivation_id": None,
                    },
                    evidence=None,
                )
        _project_transaction(
            rows,
            event,
            retained=prepared.transaction_fields.get(retained_id, {}),
            evidence=evidence,
        )
        recognized = (
            event.state == EventState.RECOGNIZED and event.kind != EventKind.NOTE
        )
        step_id = (
            child_id(
                event.identity,
                RecordKind.STEP,
                SemanticKey(
                    "opening" if event.kind == EventKind.OPENING else "quantity"
                ),
            ).value
            if recognized
            else None
        )
        if recognized:
            rows.add(
                "book_steps",
                {
                    "step_id": step_id,
                    "txn_id": identifier,
                    "step_key": "opening"
                    if event.kind == EventKind.OPENING
                    else "quantity",
                    "sequence": sequence,
                    "operation_kind": "opening"
                    if event.kind == EventKind.OPENING
                    else "quantity",
                    "effective_date": event.when.isoformat(),
                },
                evidence=derived,
            )
            sequence += 1
        for index, leg in enumerate(event.legs):
            leg_derived: Evidence = (
                ("declaration", leg.transfer.value, "booking", "transfer-v1")
                if leg.transfer is not None
                else derived
            )
            posting_id = child_id(event.identity, RecordKind.LEG, leg.key).value
            retained_leg = prepared.posting_fields.get(event.identity, {}).get(
                leg.key, {}
            )
            fields = {
                **retained_leg,
                "posting_id": posting_id,
                "posting_index": index,
                "txn_id": identifier,
                "commodity_id": leg.commodity.value,
                "amount": leg.amount,
                "account_id": leg.account.value if leg.account else None,
                "position_id": leg.position.value if leg.position else None,
                "category_id": leg.category.value if leg.category else None,
                "step_id": step_id,
                "posting_role": retained_leg.get(
                    "posting_role",
                    "cash" if leg.kind == LegKind.POSITION else "principal",
                ),
                "leg_kind": leg.kind.value,
                "symbol_as_stated": leg.symbol,
                "original_commodity_id": leg.original_commodity.value
                if leg.original_commodity is not None
                else None,
                "fx_rate_as_stated": leg.fx_rate,
                "fx_base_commodity_id": leg.fx_base_commodity.value
                if leg.fx_base_commodity is not None
                else None,
                "fx_quote_commodity_id": leg.fx_quote_commodity.value
                if leg.fx_quote_commodity is not None
                else None,
                **{
                    name: value
                    for role, leg_date in zip(DATE_ROLES, leg_dates(leg), strict=True)
                    for name, value in (
                        (f"date_{role}_mode", leg_date.mode.value),
                        (
                            f"date_{role}",
                            leg_date.value.isoformat() if leg_date.value else None,
                        ),
                    )
                },
            }
            corrections = dict(leg.corrections)
            rows.add(
                "postings",
                fields,
                evidence={
                    key: (leg_derived,)
                    + (
                        (("declaration", leg.policy.value, "declaration", None),)
                        if leg.policy is not None
                        and key in {"account_id", "position_id"}
                        else ()
                    )
                    if leg.transfer is not None
                    and leg.source_leg is not None
                    and key
                    in {
                        "leg_kind",
                        "account_id",
                        "position_id",
                        "category_id",
                        "amount",
                    }
                    else (("declaration", corrections[key].value, "correction", None),)
                    if key in corrections
                    else (
                        (
                            "declaration",
                            corrections[
                                key.removeprefix("date_").removesuffix("_mode")
                                + "_date"
                            ].value,
                            "correction",
                            None,
                        ),
                    )
                    if key.startswith("date_")
                    and key.removeprefix("date_").removesuffix("_mode") + "_date"
                    in corrections
                    else tuple(
                        ("declaration", revision.value, "rule", "enrichment-rule-v1")
                        for revision in leg.rules
                    )
                    if key == "category_id" and leg.rules
                    else (evidence,)
                    if key
                    in {
                        "amount",
                        "original_commodity_id",
                        "fx_rate_as_stated",
                        "fx_base_commodity_id",
                        "fx_quote_commodity_id",
                    }
                    and leg.kind == LegKind.POSITION
                    else (leg_derived,)
                    for key in fields
                },
            )
            if recognized:
                rows.add(
                    "posting_weights",
                    {
                        "posting_id": posting_id,
                        "component_key": "consideration",
                        "weight_kind": "consideration",
                        "commodity_id": leg.commodity.value,
                        "amount": leg.amount,
                    },
                    evidence=leg_derived,
                )
    for link in resolution.links:
        identifier = projection_key("reviewed-link", link.key.value)
        for member in link.members:
            rows.add(
                "links",
                {
                    "link_id": identifier,
                    "link_kind": link.kind.value,
                    "member_role": member.role.value,
                    "member_kind": member.kind.value,
                    "member_id": member.identity.value
                    if isinstance(member.identity, RecordId)
                    else member.identity,
                    "origin": "correction",
                    "terms_declaration_id": None,
                },
                evidence=("declaration", link.decision.value, "correction", None),
            )
    return rows.finish()
