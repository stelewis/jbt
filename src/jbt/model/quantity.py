"""Dated quantity resolution independent of adapters, checks, and storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from jbt.domain.canonical import encode_json
from jbt.domain.ids import EntityId, RecordId, SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import (
    EventState,
    FactKind,
    LegKind,
    ReviewState,
    negate,
    total,
)
from jbt.domain.time import DateMode, LegDate, TimeRole
from jbt.model.rules import Evaluation, Rule, enrich

if TYPE_CHECKING:
    from jbt.domain.identity import AcquiredAnchor, Occurrence, SourceKeyAnchor
    from jbt.domain.rules import MatchValue, RuleField
    from jbt.domain.time import TimeObservation

DATE_ROLES = ("authorized", "traded", "posted", "settled", "value")
_EVENT_TIME_ROLES = frozenset(TimeRole(role) for role in DATE_ROLES)


class FindingCode(StrEnum):
    """Closed reasons that prevent or qualify a quantity determination."""

    UNBOUND = "unbound_section"
    AMBIGUOUS_BINDING = "ambiguous_section_binding"
    SYMBOL = "symbol_unresolved"
    SYMBOL_DISAGREEMENT = "symbol_scope_disagreement"
    SYMBOL_AMBIGUOUS = "symbol_ambiguous"
    SYMBOL_VALIDITY = "symbol_out_of_validity"
    POSITION = "position_unresolved"
    POSITION_AMBIGUOUS = "position_ambiguous"
    SCOPE = "assertion_scope_unresolved"
    SCOPE_AMBIGUOUS = "assertion_scope_ambiguous"
    KEY_CONFLICT = "source_key_conflict"
    OVERLAP = "unresolved_overlap"
    SOURCE_REVISION = "source_revision_conflict"
    AUTHORITY = "source_authority_conflict"
    IDENTITY = "identity_decision_unsupported"
    RULE = "rule_semantics_unsupported"
    RULE_UNUSED = "rule_unused"
    RULE_SHADOWED = "rule_shadowed"
    RULE_REDUNDANT = "rule_redundant"
    TIME = "source_time_interpretation_unavailable"
    DATE = "event_date_unavailable"
    CONTENT = "source_content_unsupported"
    UNCLASSIFIED = "unclassified"
    GUARD = "correction_guard_failed"
    CONFLICT = "correction_assignment_conflict"
    REDUNDANT = "correction_redundant"
    SPLIT = "correction_split_invalid"
    CORRECTION_DATE = "correction_effective_date_required"
    CATEGORY_TARGET = "category_requires_boundary_leg"
    CATEGORY_UNKNOWN = "category_not_registered"
    DECISION = "decision_unsupported"
    DECISION_INVALID = "decision_invalid"
    PENDING = "pending_unconfirmed"
    TRANSFER_INVALID = "transfer_invalid"
    TRANSFER_OWNERSHIP = "transfer_leg_claimed"
    TRANSIT = "transit_position_unresolved"
    LINK = "link_invalid"


class EventKind(StrEnum):
    """Distinguish authored initialization and notes from movements."""

    TRANSACTION = "transaction"
    OPENING = "opening"
    NOTE = "note"


class SupportKind(StrEnum):
    """One chosen source occurrence and its compatible confirmations."""

    SELECTED = "selected"
    CONFIRMING = "confirming"
    ECLIPSED = "eclipsed"
    REPLACED = "replaced"


class AuthorityMode(StrEnum):
    """Select competing facts, optionally with complete scoped eclipse."""

    CONFIRMING = "confirming"
    COMPLETE_REPLACEMENT = "complete_replacement"


class RevisionAction(StrEnum):
    """Source-stated causal operations, never arrival precedence."""

    REPLACE = "replace"
    DELETE = "delete"


class SourceClass(StrEnum):
    """Importer-stated envelope class, separate from source namespace."""

    STATEMENT = "statement"
    ROLLING = "rolling"
    DOCUMENT = "document"


class ResolutionStatus(StrEnum):
    """Resolution outcome; blocking is never an economic event state."""

    RESOLVED = "resolved"
    BLOCKED = "blocked"


class PositionPurpose(StrEnum):
    """Distinguish source holdings from a declared internal transit holding."""

    GENERAL = "general"
    COLLATERAL = "collateral"
    IN_TRANSIT = "in_transit"


@dataclass(frozen=True, slots=True)
class Validity:
    """A half-open policy interval with optional unbounded endpoints."""

    start: date | None
    end: date | None

    def __post_init__(self) -> None:
        """Reject empty intervals and datetime values masquerading as dates."""
        if any(
            value is not None and type(value) is not date
            for value in (self.start, self.end)
        ):
            message = "dated_policy_required"
            raise TypeError(message)
        if self.start is not None and self.end is not None and self.start >= self.end:
            message = "empty_dated_policy"
            raise ValueError(message)

    def applies(self, when: date) -> bool:
        """Select by effective date, including start but excluding end."""
        return (self.start is None or self.start <= when) and (
            self.end is None or when < self.end
        )


@dataclass(frozen=True, slots=True)
class Binding:
    """One effective account owner of a source namespace and section."""

    scope: SemanticKey
    section: SemanticKey
    account: SemanticKey
    validity: Validity
    declaration: RecordId


@dataclass(frozen=True, slots=True)
class IgnoredSection:
    """Explicit exclusion retains evidence and its authored reason."""

    scope: SemanticKey
    section: SemanticKey
    reason: str


@dataclass(frozen=True, slots=True)
class Symbol:
    """A dated source token alias, optionally restricted to one namespace."""

    token: str
    commodity: SemanticKey
    scope: SemanticKey | None
    validity: Validity
    declaration: RecordId


@dataclass(frozen=True, slots=True)
class Position:
    """Stable account/commodity identity with dated source-location policy."""

    key: SemanticKey
    account: SemanticKey
    commodity: SemanticKey
    source_position_key: SemanticKey | None
    scope: SemanticKey | None
    section: SemanticKey | None
    validity: Validity
    declaration: RecordId
    purpose: PositionPurpose = PositionPurpose.GENERAL


@dataclass(frozen=True, slots=True)
class Finding:
    """A scoped concern; blocking prevents resolution, not evidence retention."""

    code: FindingCode
    scope: SemanticKey | RecordId
    blocking: bool

    @property
    def severity(self) -> str:
        """Separate informational redundancy from actionable review warnings."""
        if self.blocking:
            return "error"
        return (
            "info"
            if self.code in {FindingCode.REDUNDANT, FindingCode.RULE_REDUNDANT}
            else "warning"
        )


@dataclass(frozen=True, slots=True)
class SourceLeg:
    """Stated signed units before commodity and position identity resolution."""

    key: SemanticKey
    symbol: str
    amount: ExactDecimal
    source_position_key: SemanticKey | None
    original_symbol: str | None = None
    fx_rate: ExactDecimal | None = None
    fx_base_symbol: str | None = None
    fx_quote_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class Movement:
    """Source facts with explicit identity and effective/binding-date role."""

    source_record: RecordId
    anchor: AcquiredAnchor
    source_key: SourceKeyAnchor | None
    event_key: SemanticKey
    when: date
    effective_role: TimeRole = field(kw_only=True)
    state: EventState
    payee: str | None
    narration: str | None
    legs: tuple[SourceLeg, ...]
    is_transfer: bool
    dates: tuple[date | None, ...]
    times: tuple[tuple[TimeRole, TimeObservation], ...]
    transaction_type: str | None
    movement_interval: Validity | None
    description: str | None = None
    observations: tuple[tuple[SemanticKey, MatchValue], ...] = ()
    statement: SemanticKey | None = None


@dataclass(frozen=True, slots=True)
class SourceAuthority:
    """Account, basis, civil role, and period governed by source scopes."""

    account: SemanticKey
    fact_kind: FactKind
    basis: SemanticKey
    date_role: TimeRole
    validity: Validity
    source_scope: SemanticKey
    mode: AuthorityMode


@dataclass(frozen=True, slots=True)
class BalanceEvidence:
    """One bound exact source assertion observation for native review."""

    source_record: RecordId
    observation_key: SemanticKey
    account: SemanticKey
    position: SemanticKey | None
    commodity: SemanticKey
    amount: ExactDecimal


@dataclass(frozen=True, slots=True)
class StatementPart:
    """A scoped completeness claim with exact retained membership."""

    key: SemanticKey
    fact_kind: FactKind
    basis: SemanticKey
    date_role: TimeRole
    validity: Validity
    complete: bool
    source_records: tuple[RecordId, ...]
    point: TimeObservation | None = None
    balances: tuple[BalanceEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class Statement:
    """Source envelope identity and its independent evidence parts."""

    identity: SemanticKey
    scope: SemanticKey
    section: SemanticKey
    blob_digest: str
    account: SemanticKey
    parts: tuple[StatementPart, ...]
    source_class: SourceClass = SourceClass.STATEMENT


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """A retained correction occurrence targeting a keyed family."""

    source_record: RecordId
    target: SourceKeyAnchor
    action: RevisionAction


@dataclass(frozen=True, slots=True)
class IdentitySelection:
    """Reviewed family redirects or exact occurrence reassignment."""

    canonical: Occurrence
    source_records: tuple[RecordId, ...] = ()
    families: tuple[SourceKeyAnchor, ...] = ()


@dataclass(frozen=True, slots=True)
class StatementReplacement:
    """Supersession confined to one account, fact, basis, and period."""

    replaced: tuple[SemanticKey, ...]
    replacement: tuple[SemanticKey, ...]
    account: SemanticKey
    fact_kind: FactKind
    basis: SemanticKey
    date_role: TimeRole
    validity: Validity


@dataclass(frozen=True, slots=True)
class SourceStatementRevision:
    """Importer-stated correspondence to another retained envelope."""

    statement: SemanticKey
    target_blob_digest: str
    replaces: bool


@dataclass(frozen=True, slots=True)
class OccurrenceEvidence:
    """Native facts and current membership remain reviewable while blocked."""

    movement: Movement
    canonical: Occurrence
    identity: RecordId
    legs: tuple[Leg, ...]
    disposition: SupportKind | None
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class Leg:
    """Resolved signed units; boundary legs need not be classified."""

    key: SemanticKey
    kind: LegKind
    commodity: SemanticKey
    amount: ExactDecimal
    account: SemanticKey | None
    position: SemanticKey | None
    category: SemanticKey | None
    symbol: str | None
    policy: RecordId | None
    corrections: tuple[tuple[str, RecordId], ...] = ()
    dates: tuple[LegDate, ...] = ()
    rules: tuple[RecordId, ...] = ()
    rule_basis: RecordId | None = None
    source_leg: SemanticKey | None = None
    transfer: RecordId | None = None
    transfer_key: SemanticKey | None = None
    original_commodity: SemanticKey | None = None
    fx_rate: ExactDecimal | None = None
    fx_base_commodity: SemanticKey | None = None
    fx_quote_commodity: SemanticKey | None = None


@dataclass(frozen=True, slots=True)
class Support:
    """Retained source occurrence selected for or confirming an event."""

    source_record: RecordId
    kind: SupportKind
    decision: RecordId | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """Resolved facts whose effective role is independent of participation."""

    identity: RecordId
    occurrence: Occurrence
    when: date
    effective_role: TimeRole = field(kw_only=True)
    kind: EventKind
    state: EventState
    review: ReviewState
    payee: str | None
    narration: str | None
    legs: tuple[Leg, ...]
    support: tuple[Support, ...]
    declaration: RecordId | None
    corrections: tuple[tuple[str, RecordId], ...] = ()
    dates: tuple[date | None, ...] = ()
    tags: tuple[str, ...] = ()
    times: tuple[tuple[TimeRole, TimeObservation], ...] = ()
    description: str | None = None
    rules: tuple[tuple[RuleField, tuple[RecordId, ...]], ...] = ()
    identity_decision: RecordId | None = None


def event_dates(event: Event) -> tuple[date | None, ...]:
    """Use normalized civil roles, including explicit absent dates."""
    if event.dates:
        return event.dates
    return tuple(
        event.when if role == event.effective_role.value else None
        for role in DATE_ROLES
    )


def leg_dates(leg: Leg) -> tuple[LegDate, ...]:
    """Expose the five explicit inheritance modes."""
    return leg.dates or tuple(LegDate(DateMode.INHERIT, None) for _ in DATE_ROLES)


@dataclass(frozen=True, slots=True)
class Determination:
    """Canonical identity survives blocking even when no event can be emitted."""

    identity: RecordId
    status: ResolutionStatus
    event: Event | None
    candidates: tuple[Event, ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    """Complete immutable context for one bounded quantity resolution."""

    entity: EntityId
    horizon: date
    bindings: tuple[Binding, ...]
    ignored: tuple[IgnoredSection, ...]
    symbols: tuple[Symbol, ...]
    positions: tuple[Position, ...]
    rules: tuple[Rule, ...]
    movements: tuple[Movement, ...]
    authored: tuple[Event, ...]
    findings: tuple[Finding, ...]
    blocked: tuple[RecordId, ...]
    categories: tuple[SemanticKey, ...] = ()
    authorities: tuple[SourceAuthority, ...] = ()
    statements: tuple[Statement, ...] = ()
    source_revisions: tuple[SourceRevision, ...] = ()
    source_statement_revisions: tuple[SourceStatementRevision, ...] = ()
    identity_selections: tuple[IdentitySelection, ...] = ()
    statement_replacements: tuple[StatementReplacement, ...] = ()

    def __post_init__(self) -> None:
        """Reject mutable or undecoded values before entering the pure core."""
        if not isinstance(self.entity, EntityId) or type(self.horizon) is not date:
            message = "typed_model_context_required"
            raise TypeError(message)
        for values, kind in (
            (self.bindings, Binding),
            (self.ignored, IgnoredSection),
            (self.symbols, Symbol),
            (self.positions, Position),
            (self.rules, Rule),
            (self.movements, Movement),
            (self.authored, Event),
            (self.findings, Finding),
            (self.categories, SemanticKey),
            (self.blocked, RecordId),
            (self.authorities, SourceAuthority),
            (self.statements, Statement),
            (self.source_revisions, SourceRevision),
            (self.source_statement_revisions, SourceStatementRevision),
            (self.identity_selections, IdentitySelection),
            (self.statement_replacements, StatementReplacement),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(value, kind) for value in values
            ):
                message = "immutable_typed_model_inputs_required"
                raise TypeError(message)


@dataclass(frozen=True, slots=True)
class Resolution:
    """All canonical determinations and findings, including blocked identities."""

    determinations: tuple[Determination, ...]
    findings: tuple[Finding, ...]
    rules: tuple[Evaluation, ...] = ()
    retired: tuple[RecordId, ...] = ()
    redirects: tuple[tuple[RecordId, RecordId], ...] = ()
    links: tuple[Link, ...] = ()

    @property
    def events(self) -> tuple[Event, ...]:
        """Expose only resolved events inside the supplied horizon."""
        return tuple(d.event for d in self.determinations if d.event is not None)


class LinkKind(StrEnum):
    """Reviewed quantity relations, independent of event kind."""

    TRANSFER = "transfer"
    DOCUMENT = "document"
    LIFECYCLE = "lifecycle"


class MemberRole(StrEnum):
    """Participant meanings in the supported reviewed relations."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"
    SUBJECT = "subject"
    EVIDENCE = "evidence"
    PREDECESSOR = "predecessor"
    FEE = "fee"


class MemberKind(StrEnum):
    """Published address spaces for supported link participants."""

    TRANSACTION = "transaction"
    POSTING = "posting"
    BLOB = "blob"


@dataclass(frozen=True, slots=True)
class LinkMember:
    """One explicitly addressed participant in a reviewed relation."""

    role: MemberRole
    kind: MemberKind
    identity: RecordId | str


@dataclass(frozen=True, slots=True)
class Link:
    """A relation with no independent participation or quantity effects."""

    key: SemanticKey
    kind: LinkKind
    members: tuple[LinkMember, ...]
    decision: RecordId


def _commodity(
    inputs: Inputs, scope: SemanticKey, when: date, token: str
) -> tuple[SemanticKey | None, FindingCode | None]:
    candidates = tuple(
        symbol
        for symbol in inputs.symbols
        if symbol.token == token and symbol.scope in {None, scope}
    )
    applicable = tuple(s for s in candidates if s.validity.applies(when))
    global_ids = {s.commodity for s in applicable if s.scope is None}
    scoped_ids = {s.commodity for s in applicable if s.scope is not None}
    if global_ids and scoped_ids and global_ids != scoped_ids:
        return None, FindingCode.SYMBOL_DISAGREEMENT
    keys = global_ids | scoped_ids
    if len(keys) != 1:
        return None, (
            FindingCode.SYMBOL_AMBIGUOUS
            if keys
            else FindingCode.SYMBOL_VALIDITY
            if candidates
            else FindingCode.SYMBOL
        )
    return next(iter(keys)), None


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Dated account/commodity ownership and all applicable position candidates."""

    account: SemanticKey
    commodity: SemanticKey
    positions: tuple[SemanticKey, ...]
    declaration: RecordId


def source_section_key(scope: SemanticKey, section: SemanticKey) -> SemanticKey:
    """Name section findings without conflating independent source namespaces."""
    return SemanticKey(
        encode_json([scope.value, section.value], integer_strings=False)
        .decode()
        .strip()
    )


def resolve_source_location(  # noqa: PLR0913 - complete dated source coordinates
    inputs: Inputs,
    *,
    scope: SemanticKey,
    section: SemanticKey,
    when: date,
    token: str,
    source_position_key: SemanticKey | None,
) -> SourceLocation | FindingCode:
    """Use the same dated identity policy for movements and assertions."""
    binding = resolve_binding(inputs, scope=scope, section=section, when=when)
    if isinstance(binding, FindingCode):
        return binding
    commodity, error = _commodity(inputs, scope, when, token)
    if error is not None or commodity is None:
        return error or FindingCode.SYMBOL
    positions = {
        p.key
        for p in inputs.positions
        if p.account == binding.account
        and p.purpose != PositionPurpose.IN_TRANSIT
        and p.commodity == commodity
        and p.validity.applies(when)
        and p.source_position_key == source_position_key
        and p.scope in {None, scope}
        and p.section in {None, section}
    }
    return SourceLocation(
        binding.account,
        commodity,
        tuple(sorted(positions, key=lambda key: key.value)),
        binding.declaration,
    )


def resolve_binding(
    inputs: Inputs,
    *,
    scope: SemanticKey,
    section: SemanticKey,
    when: date,
) -> Binding | FindingCode:
    """Require exactly one applicable owner of a source section."""
    bindings = tuple(
        b
        for b in inputs.bindings
        if b.scope == scope and b.section == section and b.validity.applies(when)
    )
    if len(bindings) != 1:
        return FindingCode.AMBIGUOUS_BINDING if bindings else FindingCode.UNBOUND
    return bindings[0]


def _bind(
    inputs: Inputs, movement: Movement
) -> tuple[tuple[Leg, ...], tuple[Finding, ...]]:
    bindings = tuple(
        binding
        for binding in inputs.bindings
        if binding.scope == movement.anchor.namespace
        and binding.section == movement.anchor.section
        and binding.validity.applies(movement.when)
    )
    if len(bindings) != 1:
        return (), (
            Finding(
                FindingCode.AMBIGUOUS_BINDING if bindings else FindingCode.UNBOUND,
                movement.source_record,
                blocking=True,
            ),
        )
    account = bindings[0].account
    policy_findings = tuple(
        finding
        for finding in inputs.findings
        if finding.blocking and finding.scope == bindings[0].declaration
    )
    if policy_findings:
        return (), policy_findings
    findings: list[Finding] = []
    legs: list[Leg] = []
    for source in movement.legs:
        location = resolve_source_location(
            inputs,
            scope=movement.anchor.namespace,
            section=movement.anchor.section,
            when=movement.when,
            token=source.symbol,
            source_position_key=source.source_position_key,
        )
        if isinstance(location, FindingCode):
            findings.append(Finding(location, movement.source_record, blocking=True))
            continue
        positions = location.positions
        commodity = location.commodity
        currencies: list[SemanticKey | None] = []
        currency_findings: list[Finding] = []
        for token in (
            source.original_symbol,
            source.fx_base_symbol,
            source.fx_quote_symbol,
        ):
            currency, error = (
                _commodity(inputs, movement.anchor.namespace, movement.when, token)
                if token is not None
                else (None, None)
            )
            currencies.append(currency)
            if error is not None:
                currency_findings.append(
                    Finding(error, movement.source_record, blocking=True)
                )
        if currency_findings:
            findings.extend(currency_findings)
            continue
        if len(positions) != 1:
            findings.append(
                Finding(
                    FindingCode.POSITION_AMBIGUOUS
                    if positions
                    else FindingCode.POSITION,
                    movement.source_record,
                    blocking=True,
                )
            )
            continue
        legs.extend(
            (
                Leg(
                    source.key,
                    LegKind.POSITION,
                    commodity,
                    source.amount,
                    account,
                    next(iter(positions)),
                    None,
                    source.symbol,
                    bindings[0].declaration,
                    original_commodity=currencies[0],
                    fx_rate=source.fx_rate,
                    fx_base_commodity=currencies[1],
                    fx_quote_commodity=currencies[2],
                ),
                Leg(
                    SemanticKey(f"boundary:{source.key.value}"),
                    LegKind.BOUNDARY,
                    commodity,
                    negate(source.amount),
                    None,
                    None,
                    None,
                    None,
                    None,
                    source_leg=source.key,
                ),
            )
        )
    return tuple(legs), tuple(findings)


def _facts(inputs: Inputs, movement: Movement) -> tuple:
    match_fields = {
        predicate.field for rule in inputs.rules for predicate in rule.predicates
    }
    return (
        movement.when,
        movement.effective_role,
        movement.state,
        movement.payee,
        movement.narration,
        tuple(
            (
                leg.key,
                leg.symbol,
                leg.amount.rational(),
                leg.source_position_key,
                leg.original_symbol,
                leg.fx_rate.rational() if leg.fx_rate is not None else None,
                leg.fx_base_symbol,
                leg.fx_quote_symbol,
            )
            for leg in movement.legs
        ),
        movement.is_transfer,
        movement.dates,
        movement.times,
        movement.transaction_type,
        movement.description
        if SemanticKey("transactions.description_as_stated") in match_fields
        else None,
        tuple(
            (field, value.rational() if isinstance(value, ExactDecimal) else value)
            for field, value in movement.observations
            if field in match_fields
        ),
    )


def _require_balanced(event: Event) -> None:
    for commodity in {leg.commodity for leg in event.legs}:
        if (
            total(
                leg.amount for leg in event.legs if leg.commodity == commodity
            ).coefficient
            != "0"
        ):
            message = "unbalanced_quantity_event"
            raise ValueError(message)


def merge(inputs: Inputs) -> Resolution:
    """Bind and select source facts before enrichment or reviewed assignments."""
    from jbt.model.merge import resolve_sources, retired_identities  # noqa: PLC0415

    determinations, findings = resolve_sources(inputs)
    retired, redirects = retired_identities(inputs)
    findings.extend(inputs.findings)
    determinations.extend(
        Determination(event.identity, ResolutionStatus.RESOLVED, event)
        for event in inputs.authored
        if event.when <= inputs.horizon
    )
    determinations.extend(
        Determination(identity, ResolutionStatus.BLOCKED, None)
        for identity in set(inputs.blocked)
        - {determination.identity for determination in determinations}
    )
    for event in (d.event for d in determinations if d.event is not None):
        _require_balanced(event)
    return Resolution(
        tuple(sorted(determinations, key=lambda d: d.identity.value)),
        tuple(
            sorted(
                set(findings),
                key=lambda f: (f.code.value, f.scope.value, f.blocking),
            )
        ),
        retired=retired,
        redirects=redirects,
    )


def resolve(inputs: Inputs) -> Resolution:
    """Return every determination; conflicts never select an arrival winner."""
    return enrich(inputs, merge(inputs))
