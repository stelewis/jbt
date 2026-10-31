"""Source correspondence, revisions, and scoped authority before enrichment."""

from collections import defaultdict
from dataclasses import replace
from datetime import date
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise
from typing import TYPE_CHECKING

from jbt.domain.identity import Occurrence, SourceKeyAnchor, event_id
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.time import TimeRole
from jbt.model.quantity import (
    DATE_ROLES,
    AuthorityMode,
    Determination,
    Event,
    EventKind,
    Finding,
    FindingCode,
    Inputs,
    Movement,
    OccurrenceEvidence,
    ResolutionStatus,
    RevisionAction,
    SourceAuthority,
    SourceClass,
    Statement,
    StatementPart,
    StatementReplacement,
    Support,
    SupportKind,
    _bind,
    _facts,
    resolve_binding,
    source_section_key,
)

if TYPE_CHECKING:
    from jbt.domain.ids import RecordId, SemanticKey

type Groups = dict[RecordId, list[Movement]]
type Dispositions = dict[RecordId, SupportKind]


def _native(movement: Movement) -> Occurrence:
    return Occurrence(movement.source_key or movement.anchor, movement.event_key)


def _revision_families(  # noqa: C901 - validate the causal family graph
    inputs: Inputs,
) -> tuple[dict[SourceKeyAnchor, SourceKeyAnchor], set[RecordId], list[Finding]]:
    records = {m.source_record: m for m in inputs.movements}
    families = {
        *(m.source_key for m in inputs.movements if m.source_key is not None),
        *(revision.target for revision in inputs.source_revisions),
    }
    predecessors: dict[SourceKeyAnchor, set[SourceKeyAnchor]] = defaultdict(set)
    successors: dict[SourceKeyAnchor, set[SourceKeyAnchor | None]] = defaultdict(set)
    deleted: set[RecordId] = set()
    findings: list[Finding] = []
    for revision in inputs.source_revisions:
        movement = records.get(revision.source_record)
        if (
            movement is None
            or movement.source_key is None
            or revision.target.namespace != movement.anchor.namespace
            or revision.target.section != movement.anchor.section
            or revision.target.key_domain != movement.source_key.key_domain
        ):
            findings.append(
                Finding(
                    FindingCode.SOURCE_REVISION, revision.source_record, blocking=True
                )
            )
            continue
        successors[revision.target].add(
            movement.source_key if revision.action == RevisionAction.REPLACE else None
        )
        if movement.source_key != revision.target:
            predecessors[movement.source_key].add(revision.target)
        if revision.action == RevisionAction.DELETE:
            deleted.add(revision.source_record)
    roots: dict[SourceKeyAnchor, SourceKeyAnchor] = {}
    for family in families:
        current = family
        visited: set[SourceKeyAnchor] = set()
        malformed = False
        while current in predecessors:
            if current in visited or len(predecessors[current]) != 1:
                malformed = True
                break
            visited.add(current)
            current = next(iter(predecessors[current]))
        visited.add(current)
        if any(len(successors.get(key, ())) > 1 for key in visited):
            malformed = True
        if malformed:
            findings.extend(
                Finding(FindingCode.SOURCE_REVISION, m.source_record, blocking=True)
                for m in inputs.movements
                if m.source_key in visited
            )
        else:
            roots[family] = current
    for movement in inputs.movements:
        if movement.source_key in successors and not any(
            revision.source_record == movement.source_record
            and revision.action == RevisionAction.REPLACE
            and revision.target == movement.source_key
            for revision in inputs.source_revisions
        ):
            deleted.add(movement.source_record)
    return roots, deleted, findings


def source_membership(inputs: Inputs) -> dict[RecordId, Occurrence]:
    """Expose current canonical membership even when facts cannot resolve."""
    roots, _, _ = _revision_families(inputs)
    members = {}
    for movement in inputs.movements:
        native = _native(movement)
        canonical = Occurrence(
            roots.get(movement.source_key, native.anchor), movement.event_key
        )
        canonical, _ = _identity_occurrence(inputs, movement, canonical)
        members[movement.source_record] = canonical
    return members


def _identity_occurrence(
    inputs: Inputs,
    movement: Movement,
    canonical: Occurrence,
) -> tuple[Occurrence, bool]:
    exact = {
        selection.canonical
        for selection in inputs.identity_selections
        if movement.source_record in selection.source_records
    }
    if exact:
        return (next(iter(exact)), False) if len(exact) == 1 else (canonical, True)
    visited = set()
    while isinstance(canonical.anchor, SourceKeyAnchor):
        if canonical in visited:
            return canonical, True
        visited.add(canonical)
        destinations = {
            selection.canonical
            for selection in inputs.identity_selections
            if canonical.anchor in selection.families
            and canonical.event_key == selection.canonical.event_key
            and selection.canonical != canonical
        }
        if not destinations:
            return canonical, False
        if len(destinations) != 1:
            return canonical, True
        canonical = next(iter(destinations))
    return canonical, False


def retired_identities(
    inputs: Inputs,
) -> tuple[tuple[RecordId, ...], tuple[tuple[RecordId, RecordId], ...]]:
    """Retire only identities wholly reassigned by reviewed correspondence."""
    before = source_membership(replace(inputs, identity_selections=()))
    after = source_membership(inputs)
    destinations: dict[RecordId, set[RecordId]] = defaultdict(set)
    for record, occurrence in before.items():
        destinations[event_id(inputs.entity, occurrence)].add(
            event_id(inputs.entity, after[record])
        )
    retired = tuple(
        sorted(
            (
                identity
                for identity, targets in destinations.items()
                if identity not in targets
            ),
            key=lambda identity: identity.value,
        )
    )
    return retired, tuple(
        (identity, next(iter(destinations[identity])))
        for identity in retired
        if len(destinations[identity]) == 1
    )


def _role_date(movement: Movement, part: StatementPart) -> date | None:
    if not movement.dates:
        return movement.when if part.date_role.value == "posted" else None
    return movement.dates[DATE_ROLES.index(part.date_role.value)]


def _replacements(
    inputs: Inputs,
) -> tuple[list[StatementReplacement], list[Finding]]:
    statements = {statement.identity: statement for statement in inputs.statements}
    replacements = list(inputs.statement_replacements)
    findings: list[Finding] = []
    for revision in inputs.source_statement_revisions:
        statement = statements.get(revision.statement)
        targets = tuple(
            other
            for other in inputs.statements
            if other.blob_digest == revision.target_blob_digest
            and statement is not None
            and other.scope == statement.scope
            and other.section == statement.section
        )
        if statement is None or len(targets) != 1 or targets[0] == statement:
            findings.append(
                Finding(FindingCode.SOURCE_REVISION, revision.statement, blocking=True)
            )
            continue
        if revision.replaces:
            replacements.extend(
                StatementReplacement(
                    (targets[0].identity,),
                    (statement.identity,),
                    statement.account,
                    part.fact_kind,
                    part.basis,
                    part.date_role,
                    part.validity,
                )
                for part in statement.parts
            )
    for replacement in replacements:
        if (
            not replacement.replaced
            or not replacement.replacement
            or set(replacement.replaced) & set(replacement.replacement)
            or any(
                key not in statements or statements[key].account != replacement.account
                for key in (*replacement.replaced, *replacement.replacement)
            )
        ):
            findings.extend(
                Finding(FindingCode.SOURCE_REVISION, key, blocking=True)
                for key in (*replacement.replaced, *replacement.replacement)
            )
    findings.extend(_replacement_graph_findings(replacements))
    return replacements, findings


def _graph_outcome(
    origin: SemanticKey,
    graph: dict[SemanticKey, set[frozenset[SemanticKey]]],
) -> tuple[set[SemanticKey], bool]:
    pending = [origin]
    dependencies = {}
    while pending:
        current = pending.pop()
        if current in dependencies:
            continue
        successors = {
            successor
            for replacement in graph.get(current, ())
            for successor in replacement
        }
        dependencies[current] = successors
        pending.extend(successors)
    affected = set(dependencies)
    try:
        ordered = tuple(TopologicalSorter(dependencies).static_order())
    except CycleError:
        return affected, True
    terminals: dict[SemanticKey, frozenset[SemanticKey]] = {}
    conflicting = False
    for current in ordered:
        if current not in graph:
            terminals[current] = frozenset({current})
            continue
        outcomes = {
            frozenset().union(*(terminals[successor] for successor in replacement))
            for replacement in graph[current]
        }
        terminals[current] = frozenset().union(*outcomes)
        conflicting |= len(outcomes) > 1
    return affected, conflicting


def _replacement_graph_findings(
    replacements: list[StatementReplacement],
) -> list[Finding]:
    boundaries = {
        date.min,
        *(
            endpoint
            for revision in replacements
            for endpoint in (revision.validity.start, revision.validity.end)
            if endpoint is not None
        ),
    }
    coordinates = {
        (revision.account, revision.fact_kind, revision.basis, revision.date_role)
        for revision in replacements
    }
    findings = []
    for coordinates_at in coordinates:
        for when in boundaries:
            graph: dict[SemanticKey, set[frozenset[SemanticKey]]] = defaultdict(set)
            for revision in replacements:
                if coordinates_at == (
                    revision.account,
                    revision.fact_kind,
                    revision.basis,
                    revision.date_role,
                ) and revision.validity.applies(when):
                    for old in revision.replaced:
                        graph[old].add(frozenset(revision.replacement))
            for origin in graph:
                affected, conflicting = _graph_outcome(origin, graph)
                if conflicting:
                    findings.extend(
                        Finding(FindingCode.SOURCE_REVISION, key, blocking=True)
                        for key in {origin, *affected}
                    )
    return findings


def _parts(statement: Statement, movement: Movement) -> tuple[StatementPart, ...]:
    return tuple(
        part
        for part in statement.parts
        if part.fact_kind == FactKind.MOVEMENT
        and part.point is None
        and movement.source_record in part.source_records
    )


def _replaced(
    statement: Statement,
    part: StatementPart,
    when: date,
    replacements: list[StatementReplacement],
) -> bool:
    return any(
        statement.identity in revision.replaced
        and statement.account == revision.account
        and part.fact_kind == revision.fact_kind
        and part.basis == revision.basis
        and part.date_role == revision.date_role
        and revision.validity.applies(when)
        for revision in replacements
    )


def _authority_matches(
    authority: SourceAuthority,
    statement: Statement,
    part: StatementPart,
    when: date,
) -> bool:
    return (
        authority.account == statement.account
        and authority.fact_kind == part.fact_kind
        and authority.basis == part.basis
        and authority.date_role == part.date_role
        and authority.validity.applies(when)
    )


def _authoritative(
    authority: SourceAuthority,
    statement: Statement,
    part: StatementPart,
    when: date,
) -> bool:
    return (
        _authority_matches(authority, statement, part, when)
        and statement.scope == authority.source_scope
        and (
            authority.mode == AuthorityMode.CONFIRMING
            or (statement.source_class == SourceClass.STATEMENT and part.complete)
        )
    )


def _covered(
    inputs: Inputs,
    authority: SourceAuthority,
    when: date,
    replacements: list[StatementReplacement],
) -> bool:
    return any(
        _authoritative(authority, other, candidate, when)
        and candidate.complete
        and candidate.point is None
        and candidate.fact_kind == authority.fact_kind
        and candidate.basis == authority.basis
        and candidate.date_role == authority.date_role
        and candidate.validity.applies(when)
        and not _replaced(other, candidate, when, replacements)
        for other in inputs.statements
        for candidate in other.parts
    )


def coverage_segments(
    inputs: Inputs, statement: Statement, part: StatementPart
) -> tuple[tuple[date, date, bool], ...]:
    """Partition a movement claim at scoped revision and authority bounds."""
    start, end = part.validity.start, part.validity.end
    if start is None or end is None:
        message = "coverage_bounds_required"
        raise ValueError(message)
    replacements, _ = _replacements(inputs)
    boundaries = sorted(
        {
            start,
            end,
            *(
                boundary
                for validity in (
                    *(revision.validity for revision in replacements),
                    *(authority.validity for authority in inputs.authorities),
                    *(
                        candidate.validity
                        for other in inputs.statements
                        for candidate in other.parts
                    ),
                )
                for boundary in (validity.start, validity.end)
                if boundary is not None and start < boundary < end
            ),
        }
    )
    segments = tuple(
        (
            left,
            right,
            _replaced(statement, part, left, replacements)
            or any(
                authority.mode == AuthorityMode.COMPLETE_REPLACEMENT
                and authority.source_scope != statement.scope
                and _authority_matches(authority, statement, part, left)
                and _covered(inputs, authority, left, replacements)
                for authority in inputs.authorities
            ),
        )
        for left, right in pairwise(boundaries)
    )
    merged: list[tuple[date, date, bool]] = []
    for left, right, eclipsed in segments:
        if merged and merged[-1][2] == eclipsed:
            merged[-1] = merged[-1][0], right, eclipsed
        else:
            merged.append((left, right, eclipsed))
    return tuple(merged)


def _dispositions(  # noqa: C901, PLR0912 - replacement precedes authority and eclipse
    inputs: Inputs,
    groups: Groups,
    deleted: set[RecordId],
) -> tuple[Dispositions, set[RecordId], list[Finding]]:
    statements = {statement.identity: statement for statement in inputs.statements}
    replacements, findings = _replacements(inputs)
    dispositions = dict.fromkeys(deleted, SupportKind.REPLACED)
    for movements in groups.values():
        for movement in movements:
            statement = statements.get(movement.statement)
            if statement is None:
                continue
            for part in _parts(statement, movement):
                when = _role_date(movement, part)
                if when is None:
                    continue
                if _replaced(statement, part, when, replacements):
                    dispositions[movement.source_record] = SupportKind.REPLACED
    authoritative = set()
    for movements in groups.values():
        for movement in movements:
            statement = statements.get(movement.statement)
            if statement is None or movement.source_record in dispositions:
                continue
            for part in _parts(statement, movement):
                when = _role_date(movement, part)
                if when is not None and any(
                    _authoritative(authority, statement, part, when)
                    for authority in inputs.authorities
                ):
                    authoritative.add(movement.source_record)
    for movements in groups.values():
        for movement in movements:
            statement = statements.get(movement.statement)
            if statement is None or movement.source_record in dispositions:
                continue
            for part in _parts(statement, movement):
                when = _role_date(movement, part)
                if when is None:
                    continue
                for authority in inputs.authorities:
                    if (
                        authority.mode != AuthorityMode.COMPLETE_REPLACEMENT
                        or not _authority_matches(authority, statement, part, when)
                        or _authoritative(authority, statement, part, when)
                        or not authority.validity.applies(when)
                        or any(m.source_record in authoritative for m in movements)
                    ):
                        continue
                    if _covered(inputs, authority, when, replacements):
                        dispositions[movement.source_record] = SupportKind.ECLIPSED
    return dispositions, authoritative, findings


def _overlaps(
    inputs: Inputs,
    groups: Groups,
    dispositions: Dispositions,
) -> set[RecordId]:
    candidates = [
        (identity, movement)
        for identity, movements in groups.items()
        for movement in movements
        if movement.state == EventState.RECOGNIZED
        and movement.source_record not in dispositions
    ]
    keyless = [
        (identity, movement)
        for identity, movement in candidates
        if movement.source_key is None
    ]
    by_blob: dict[str, list[tuple[RecordId, Movement]]] = defaultdict(list)
    for candidate in candidates:
        by_blob[candidate[1].anchor.blob_digest].append(candidate)
    if not keyless or len(by_blob) <= 1:
        return set()
    bindings = {
        movement.source_record: resolve_binding(
            inputs,
            scope=movement.anchor.namespace,
            section=movement.anchor.section,
            when=movement.when,
        )
        for _, movement in candidates
    }
    statements = {statement.identity: statement for statement in inputs.statements}
    blocked = set()
    for identity, first in keyless:
        first_binding = bindings[first.source_record]
        for blob, others in by_blob.items():
            if blob == first.anchor.blob_digest:
                continue
            for other_id, second in others:
                if (
                    second.source_key is None
                    and second.source_record.value < first.source_record.value
                ):
                    continue
                second_binding = bindings[second.source_record]
                if (
                    identity != other_id
                    and not isinstance(first_binding, FindingCode)
                    and not isinstance(second_binding, FindingCode)
                    and first_binding.account == second_binding.account
                    and _intervals_overlap(statements, first, second)
                ):
                    blocked.add(identity)
                    if second.source_key is None:
                        blocked.add(other_id)
    return blocked


def _intervals_overlap(
    statements: dict[SemanticKey, Statement], first: Movement, second: Movement
) -> bool:
    first_statement, second_statement = (
        statements.get(movement.statement) for movement in (first, second)
    )
    if first_statement is None or second_statement is None:
        return (
            first.movement_interval is not None
            and second.movement_interval is not None
            and first.movement_interval.applies(second.when)
            and second.movement_interval.applies(first.when)
        )
    return any(
        left.basis == right.basis
        and left.fact_kind == right.fact_kind
        and left.date_role == right.date_role
        and (first_date := _role_date(first, left)) is not None
        and (second_date := _role_date(second, right)) is not None
        and left.validity.applies(second_date)
        and right.validity.applies(first_date)
        for left in _parts(first_statement, first)
        for right in _parts(second_statement, second)
    )


def _event(  # noqa: PLR0913 - selected identity, source facts, and participation
    inputs: Inputs,
    identity: RecordId,
    occurrence: Occurrence,
    movement: Movement,
    support: tuple[Support, ...],
    *,
    retracted: bool = False,
) -> tuple[Event, tuple[Finding, ...]]:
    legs, findings = _bind(inputs, movement)
    return Event(
        identity,
        occurrence,
        movement.when,
        EventKind.TRANSACTION,
        EventState.RETRACTED if retracted else movement.state,
        ReviewState.UNREVIEWED,
        movement.payee,
        movement.narration,
        legs,
        support,
        None,
        effective_role=movement.effective_role,
        dates=movement.dates,
        times=tuple(
            (role, time)
            for role, time in movement.times
            if role.value in DATE_ROLES or role == TimeRole.INITIATED
        ),
        description=movement.description,
    ), findings


def _bound_facts(inputs: Inputs, movement: Movement) -> tuple:
    legs, _ = _bind(inputs, movement)
    return (
        _facts(inputs, movement),
        tuple(
            (
                leg.key,
                leg.kind,
                leg.account,
                leg.position,
                leg.commodity,
                leg.amount.rational(),
                leg.original_commodity,
                leg.fx_rate.rational() if leg.fx_rate is not None else None,
                leg.fx_base_commodity,
                leg.fx_quote_commodity,
            )
            for leg in legs
        ),
    )


def _authority_findings(inputs: Inputs) -> list[Finding]:
    conflicts = tuple(
        (authority, other)
        for index, authority in enumerate(inputs.authorities)
        for other in inputs.authorities[index + 1 :]
        if other.account == authority.account
        and other.fact_kind == authority.fact_kind
        and other.basis == authority.basis
        and (
            other.validity.end is None
            or authority.validity.start is None
            or authority.validity.start < other.validity.end
        )
        and (
            authority.validity.end is None
            or other.validity.start is None
            or other.validity.start < authority.validity.end
        )
    )
    findings = [
        Finding(FindingCode.AUTHORITY, authority.account, blocking=True)
        for authority, _ in conflicts
    ]
    statements = {statement.identity: statement for statement in inputs.statements}
    for movement in inputs.movements:
        statement = statements.get(movement.statement)
        if statement is not None and any(
            (when := _role_date(movement, part)) is not None
            and _authority_matches(authority, statement, part, when)
            and _authority_matches(other, statement, part, when)
            for authority, other in conflicts
            for part in _parts(statement, movement)
        ):
            findings.append(
                Finding(FindingCode.AUTHORITY, movement.source_record, blocking=True)
            )
    return findings


def resolve_sources(
    inputs: Inputs,
) -> tuple[list[Determination], list[Finding]]:
    """Resolve causal source graphs before choosing source-derived facts."""
    _, deleted, findings = _revision_families(inputs)
    members = source_membership(inputs)
    native_members = source_membership(replace(inputs, identity_selections=()))
    groups = defaultdict(list)
    for movement in inputs.movements:
        if not any(
            ignored.scope == movement.anchor.namespace
            and ignored.section == movement.anchor.section
            for ignored in inputs.ignored
        ):
            groups[event_id(inputs.entity, members[movement.source_record])].append(
                movement
            )
        _, identity_conflict = _identity_occurrence(
            inputs,
            movement,
            native_members[movement.source_record],
        )
        if identity_conflict:
            findings.append(
                Finding(FindingCode.IDENTITY, movement.source_record, blocking=True)
            )
    dispositions, authoritative, revision_findings = _dispositions(
        inputs, groups, deleted
    )
    findings.extend(revision_findings)
    findings.extend(_authority_findings(inputs))
    overlaps = _overlaps(inputs, groups, dispositions)
    determinations = []
    for identity, movements in sorted(groups.items(), key=lambda item: item[0].value):
        movements.sort(key=lambda movement: movement.source_record.value)
        live = [m for m in movements if m.source_record not in dispositions]
        preferred = [m for m in live if m.source_record in authoritative] or live
        recognized = [m for m in preferred if m.state == EventState.RECOGNIZED]
        preferred = recognized or preferred
        blocked = identity in inputs.blocked
        if len({_bound_facts(inputs, m) for m in preferred}) > 1:
            findings.append(
                Finding(
                    FindingCode.AUTHORITY
                    if preferred
                    and all(m.source_record in authoritative for m in preferred)
                    else FindingCode.KEY_CONFLICT,
                    identity,
                    blocking=True,
                )
            )
            blocked = True
        if identity in overlaps:
            findings.append(Finding(FindingCode.OVERLAP, identity, blocking=True))
            blocked = True
        selected = min(preferred or movements, key=lambda m: m.source_record.value)
        support = tuple(
            Support(
                m.source_record,
                dispositions.get(
                    m.source_record,
                    SupportKind.SELECTED
                    if m == selected
                    else SupportKind.CONFIRMING
                    if _bound_facts(inputs, m) == _bound_facts(inputs, selected)
                    else SupportKind.ECLIPSED,
                ),
            )
            for m in sorted(movements, key=lambda m: m.source_record.value)
        )
        event, binding_findings = _event(
            inputs,
            identity,
            members[selected.source_record],
            selected,
            support,
            retracted=not live,
        )
        findings.extend(binding_findings)
        scopes = {
            identity,
            *(m.source_record for m in movements),
            *(m.statement for m in movements if m.statement is not None),
            *(
                source_section_key(m.anchor.namespace, m.anchor.section)
                for m in movements
            ),
        }
        blocked |= any(
            finding.blocking and finding.scope in scopes
            for finding in (*inputs.findings, *findings)
        )
        if live and not recognized and selected.state == EventState.PENDING:
            findings.append(Finding(FindingCode.PENDING, identity, blocking=False))
        candidates = (
            tuple(
                _event(
                    inputs,
                    identity,
                    members[m.source_record],
                    m,
                    (Support(m.source_record, SupportKind.SELECTED),),
                )[0]
                for m in movements
            )
            if blocked or selected.when > inputs.horizon
            else ()
        )
        determinations.append(
            Determination(
                identity,
                ResolutionStatus.BLOCKED if blocked else ResolutionStatus.RESOLVED,
                event if not blocked and selected.when <= inputs.horizon else None,
                candidates,
            )
        )
    return determinations, findings


def occurrence_evidence(inputs: Inputs) -> tuple[OccurrenceEvidence, ...]:
    """Retain native review targets independently of economic resolution."""
    members = source_membership(inputs)
    groups: Groups = defaultdict(list)
    for movement in inputs.movements:
        groups[event_id(inputs.entity, members[movement.source_record])].append(
            movement
        )
    _, deleted, findings = _revision_families(inputs)
    dispositions, _, statement_findings = _dispositions(inputs, groups, deleted)
    findings.extend(statement_findings)
    evidence = []
    for movement in sorted(inputs.movements, key=lambda m: m.source_record.value):
        legs, binding_findings = _bind(inputs, movement)
        evidence.append(
            OccurrenceEvidence(
                movement,
                members[movement.source_record],
                event_id(inputs.entity, members[movement.source_record]),
                legs,
                dispositions.get(movement.source_record),
                (
                    *binding_findings,
                    *(
                        finding
                        for finding in findings
                        if finding.scope in {movement.source_record, movement.statement}
                    ),
                ),
            )
        )
    return tuple(evidence)


def statement_evidence(
    inputs: Inputs,
) -> tuple[tuple[Statement, tuple[StatementReplacement, ...]], ...]:
    """Pair retained native statements with scoped upstream supersession."""
    replacements, _ = _replacements(inputs)
    return tuple(
        (
            statement,
            tuple(
                revision
                for revision in replacements
                if statement.identity in (*revision.replaced, *revision.replacement)
            ),
        )
        for statement in sorted(inputs.statements, key=lambda s: s.identity.value)
    )


def balance_dispositions(inputs: Inputs) -> Dispositions:
    """Select model balance constraints without changing per-extract checks."""
    replacements, _ = _replacements(inputs)
    dispositions: Dispositions = {}
    points = tuple(
        (statement, part)
        for statement in inputs.statements
        for part in statement.parts
        if part.fact_kind == FactKind.BALANCE and part.point is not None
    )
    for statement, part in points:
        if _replaced(statement, part, part.validity.start or date.min, replacements):
            dispositions.update(
                dict.fromkeys(part.source_records, SupportKind.REPLACED)
            )
    for statement, part in points:
        when = part.validity.start or date.min
        for authority in inputs.authorities:
            if not _authority_matches(
                authority, statement, part, when
            ) or _authoritative(authority, statement, part, when):
                continue
            if any(
                _authoritative(authority, other, candidate, when)
                and candidate.point == part.point
                and not _replaced(other, candidate, when, replacements)
                for other, candidate in points
            ):
                for record in part.source_records:
                    dispositions.setdefault(record, SupportKind.ECLIPSED)
    return dispositions
