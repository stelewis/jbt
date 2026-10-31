"""Retained review commands and authentication of selected journal inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from jbt.artifacts.attempt import (
    decode_review_report,
    read_target,
    review_boundary,
    write_target,
)
from jbt.artifacts.decisions import decode_decision_body, encode_decision_body
from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.artifacts.journal import decode_head
from jbt.contracts.declarations import field_registry
from jbt.contracts.primitives import ContractError, require
from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.decisions import (
    AssignBody,
    Assignment,
    AssignmentDateRole,
    DocumentLinkBody,
    EventDateAssignment,
    EventReviewAssignment,
    EventReviewRef,
    EventStateAssignment,
    EventTagsAssignment,
    EventTextAssignment,
    EventTextField,
    IdentityBody,
    IdentityMode,
    LegCategoryAssignment,
    LegDateAssignment,
    LegReviewRef,
    LifecycleLinkBody,
    ReviewBasisRef,
    SplitBody,
    SplitChild,
    StatementRevisionBody,
    StructuralBody,
    TransferBody,
    decision_kind,
    structural_references,
)
from jbt.domain.errors import DomainError
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    DecisionStage,
    JournalEntry,
    JournalOperation,
    entry_digest,
)
from jbt.domain.numbers import ExactDecimal
from jbt.domain.quantity import EventState, FactKind, ReviewState
from jbt.domain.review import compound_candidates, compound_references, compound_target
from jbt.domain.time import DateMode, LegDate
from jbt.model.corrections import Decision
from jbt.runtime.inputs import capture_journal, retained_journal_heads
from jbt.runtime.project import document, objects, strings, text
from jbt.storage.attempts import read_report
from jbt.storage.journal import append_journal
from jbt.storage.local import put_retained, read_retained, writer_lock

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jbt.domain.guards import GuardTarget
    from jbt.domain.journal import JournalSelection
    from jbt.runtime.inputs import InputSnapshot

_MINIMUM_ID_PREFIX = 12


@dataclass(frozen=True, slots=True)
class IdentityRequest:
    """CLI addresses; resolution and validation happen against retained facts."""

    mode: IdentityMode
    canonical: str
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevisionRequest:
    """A requested scoped replacement of retained statement evidence."""

    replaced: tuple[str, ...]
    replacement: tuple[str, ...]
    account: str
    fact_kind: str
    basis: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class TransferRequest:
    """Explicit outgoing and incoming source leg addresses."""

    outgoing: tuple[str, ...]
    incoming: tuple[str, ...]
    commodity: str


@dataclass(frozen=True, slots=True)
class DocumentLinkRequest:
    """Native target and retained archive-only blob digests."""

    target: str
    documents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LifecycleLinkRequest:
    """Explicit event addresses, without inferred failure evidence."""

    attempt: str
    fee: str


type DecisionRequest = (
    IdentityRequest
    | RevisionRequest
    | TransferRequest
    | DocumentLinkRequest
    | LifecycleLinkRequest
)


def report(root: Path, attempt: str) -> dict[str, JsonValue]:
    """Read the interpretation the caller actually selected, without rerunning."""
    return _reviewed_report(root, attempt, byte_digest(read_report(root, attempt)))


def _reviewed_report(root: Path, attempt: str, digest: str) -> dict[str, JsonValue]:
    result = document(decode_review_report(read_retained(root, digest)))
    require(result["attempt_digest"] == attempt, "attempt_report_lineage", "review")
    witness = document(decode_document(read_retained(root, attempt), "attempt.witness"))
    require(
        document(result["selection"])["input_digest"] == witness["input_digest"],
        "attempt_input_lineage",
        "review",
    )
    return result


def _boundary(
    report_document: Mapping[str, object],
    key: str | None,
    *,
    stage: DecisionStage | None = None,
) -> tuple[GuardTarget, ...]:
    require(
        report_document["model"] is not None,
        "attempt_has_no_financial_determinations",
        "review",
    )
    model = document(report_document["model"])
    if key is None and stage is not None:
        kinds = {
            text(row["declaration_key"], "decision_key"): DecisionKind(
                text(row["declaration_kind"], "declaration_kind")
            )
            for row in objects(
                document(model["tables"])["declarations"], "declarations"
            )
            if row["declaration_kind"] in {kind.value for kind in DecisionKind}
        }
        receipts = objects(document(model["review"])["receipts"], "receipts")
        require(
            all(receipt["decision_key"] in kinds for receipt in receipts),
            "decision_stage_binding",
            "review",
        )
        key = next(
            (
                text(receipt["decision_key"], "decision_key")
                for receipt in receipts
                if kinds[text(receipt["decision_key"], "decision_key")].stage > stage
            ),
            None,
        )
    return review_boundary(model["review"], key)


def _selected_target(
    candidates: tuple[GuardTarget, ...], identifier: str
) -> tuple[GuardTarget, ...]:
    addresses = {
        candidate.reference
        for candidate in candidates
        if candidate.reference.key == (identifier,)
        or (
            len(identifier) >= _MINIMUM_ID_PREFIX
            and all(character in "0123456789abcdef" for character in identifier)
            and str(candidate.reference.key[0]).startswith(identifier)
        )
    }
    require(len(addresses) == 1, "review_target_missing_or_ambiguous", "review")
    return tuple(
        candidate for candidate in candidates if candidate.reference in addresses
    )


def selected_decisions(root: Path, snapshot: InputSnapshot) -> tuple[Decision, ...]:
    """Authenticate all committed bodies and bases from captured immutable refs."""
    return authenticate_decisions(
        root,
        journal=snapshot.journal,
        bases=snapshot.journal_bases,
        reports=snapshot.journal_reports,
    )


def authenticate_decisions(
    root: Path,
    *,
    journal: JournalSelection,
    bases: tuple[str, ...],
    reports: tuple[tuple[str, str], ...],
) -> tuple[Decision, ...]:
    """Bind every reviewed boundary to its captured immutable report role."""
    references = dict(reports)
    require(
        reports == tuple(sorted(references.items()))
        and set(references)
        == {entry.reviewed_attempt_digest for entry in journal.committed_entries},
        "decision_report_inventory",
        "journal",
    )
    reviewed_reports = {
        attempt: _reviewed_report(root, attempt, digest) for attempt, digest in reports
    }
    decoded: dict[str, Decision] = {}
    selected_bases: set[str] = set()
    for entry in journal.committed_entries:
        if entry.payload_digest is None:
            continue
        body = decode_decision_body(read_retained(root, entry.payload_digest))
        require(
            body.entity == entry.entity
            and decision_kind(body) == entry.kind
            and body.review.attempt_digest == entry.reviewed_attempt_digest,
            "decision_header_binding",
            "journal",
        )
        if entry.operation is JournalOperation.SUPERSEDE:
            prior = decoded.get(entry.prior_entry_digest or "")
            require(
                prior is not None and type(prior.body) is type(body),
                "decision_variant_mismatch",
                "journal",
            )
        selected_bases.add(body.review.basis_digest)
        basis = document(
            decode_document(read_retained(root, body.review.basis_digest), "basis")
        )
        require(
            set(basis)
            == {
                "schema_version",
                "attempt_digest",
                "report_digest",
                "before_key",
                "target",
            }
            and type(basis["schema_version"]) is int
            and basis["schema_version"] == 1
            and basis["attempt_digest"] == entry.reviewed_attempt_digest,
            "decision_basis_binding",
            "journal",
        )
        require(
            basis["report_digest"] == references[entry.reviewed_attempt_digest],
            "decision_report_binding",
            "journal",
        )
        reviewed = reviewed_reports[entry.reviewed_attempt_digest]
        key = basis["before_key"]
        key = None if key is None else text(key, "decision_boundary")
        target = read_target(basis["target"])
        if not isinstance(body, AssignBody | SplitBody):
            require(
                compound_references(target) == structural_references(body),
                "decision_basis_operands",
                "journal",
            )
        boundary = _boundary(reviewed, key, stage=decision_kind(body).stage)
        candidates = (
            compound_candidates(target, boundary)
            if target.reference.table.value == "review_merge"
            else tuple(item for item in boundary if item.reference == target.reference)
        )
        require(
            len(candidates) == 1 and candidates[0] == target and target.active,
            "decision_basis_not_reviewed",
            "journal",
        )
        decoded[entry_digest(entry)] = Decision(entry, body, target)
    require(selected_bases == set(bases), "decision_basis_inventory", "journal")
    return tuple(
        decoded[entry_digest(active.entry)] for active in journal.active_decisions
    )


def show(root: Path, *, attempt: str, identifier: str | None) -> dict[str, object]:
    """Expose complete findings, lineage and receipts even for failed builds."""
    retained = report(root, attempt)
    if identifier is None:
        return dict(retained)
    candidates = _selected_target(_boundary(retained, None), identifier)
    identifier = str(candidates[0].reference.key[0])
    model = document(retained["model"])
    tables = document(model["tables"])
    if candidates[0].reference.table.value == "review_documents":
        return {
            "attempt_digest": attempt,
            "targets": [write_target(item) for item in candidates],
            "findings": model["findings"],
            "review": model["review"],
        }
    if candidates[0].reference.table.value in {"review_sources", "review_statements"}:
        return {
            "attempt_digest": attempt,
            "targets": [write_target(item) for item in candidates],
            "findings": model["findings"],
            "review": model["review"],
            "source_records": [
                row
                for row in objects(tables["source_records"], "source_records")
                if row["source_record_id"] == identifier
            ],
            "statements": [
                row
                for row in objects(tables["statements"], "statements")
                if row["statement_id"] == identifier
            ],
        }
    event_identifier = (
        identifier
        if candidates[0].reference.table.value == "review_events"
        else next(
            str(field.value) for field in candidates[0].fields if field.name == "parent"
        )
    )
    support = [
        row
        for row in objects(tables["event_support"], "event_support")
        if row["txn_id"] == event_identifier
    ]
    source_ids = {row["source_record_id"] for row in support}
    postings = [
        row
        for row in objects(tables["postings"], "postings")
        if row["txn_id"] == event_identifier
    ]
    related = {
        event_identifier,
        *(text(row["posting_id"], "posting_id") for row in postings),
    }
    related_keys = {
        encode_json([key], integer_strings=False).decode().strip() for key in related
    }
    provenance = [
        row
        for row in objects(tables["provenance"], "provenance")
        if row["record_id"] in related_keys
    ]
    evidence_ids = {
        row["evidence_id"]
        for row in provenance
        if row["evidence_kind"] == "declaration"
    }
    journal = [
        (digest, decode_document(read_retained(root, digest), "journal.entry"))
        for digest in strings(
            document(retained["selection"])["journal_headers"], "headers"
        )
    ]
    return {
        "attempt_digest": attempt,
        "targets": [write_target(item) for item in candidates],
        "findings": model["findings"],
        "review": {
            **document(model["review"]),
            "rules": [
                row
                for row in objects(document(model["review"])["rules"], "rules")
                if row["txn_id"] == event_identifier
                and (
                    candidates[0].reference.table.value == "review_events"
                    or identifier in strings(row["target_posting_ids"], "rule_targets")
                )
            ],
        },
        "event_support": support,
        "source_records": [
            row
            for row in objects(tables["source_records"], "source_records")
            if row["source_record_id"] in source_ids
        ],
        "observations": [
            row
            for row in objects(tables["observations"], "observations")
            if row["source_record_id"] in source_ids
            or row["record_id"]
            in {
                encode_json([source_id], integer_strings=False).decode().strip()
                for source_id in source_ids
            }
        ],
        "transactions": [
            row
            for row in objects(tables["transactions"], "transactions")
            if row["txn_id"] == event_identifier
        ],
        "postings": postings,
        "event_times": [
            row
            for row in objects(tables["event_times"], "event_times")
            if row["record_id"] in related
        ],
        "journal_entries": [entry for _, entry in journal],
        "journal_bodies": {
            digest: decode_document(
                read_retained(root, text(entry["payload_digest"], "payload_digest")),
                "decision.body",
            )
            for digest, entry in journal
            if entry["payload_digest"] is not None
        },
        "provenance": provenance,
        "declarations": [
            row
            for row in objects(tables["declarations"], "declarations")
            if row["declaration_id"] in evidence_ids
        ],
    }


def _assignment(field: str, value: str | None) -> Assignment:
    registry = {row["field_name"]: row for row in field_registry()}
    require(
        field in registry and registry[field]["correction_allowed"],
        "correction_field_prohibited",
        "correct",
    )
    table, name = field.split(".", 1)
    match table, name:
        case "transactions", "payee" | "narration":
            return EventTextAssignment(EventTextField(name), value)
        case "transactions", "review_state":
            require(value is not None, "correction_value_required", "correct")
            return EventReviewAssignment(ReviewState(value))
        case "transactions", "event_state":
            require(value is not None, "correction_value_required", "correct")
            return EventStateAssignment(EventState(value))
        case "transactions", "tags":
            require(value is not None, "correction_value_required", "correct")
            tags = json.loads(text(value, "tags"))
            require(
                type(tags) is list and all(type(tag) is str for tag in tags),
                "correction_tags",
                "correct",
            )
            return EventTagsAssignment(tuple(tags))
        case "postings", "category_id":
            return LegCategoryAssignment(
                SemanticKey(value) if value is not None else None
            )
        case "transactions", name if name.startswith("date_") and not name.endswith(
            "_mode"
        ):
            return EventDateAssignment(
                AssignmentDateRole(name.removeprefix("date_")),
                date.fromisoformat(value) if value is not None else None,
            )
        case "postings", name if name.startswith("date_") and not name.endswith(
            "_mode"
        ):
            role = AssignmentDateRole(name.removeprefix("date_"))
            mode = (
                DateMode(value)
                if value in {"inherit", "unknown"}
                else DateMode.VALUE
                if value is not None
                else DateMode.UNKNOWN
            )
            return LegDateAssignment(
                role,
                LegDate(
                    mode,
                    date.fromisoformat(value)
                    if mode == DateMode.VALUE and value is not None
                    else None,
                ),
            )
        case _:
            code = "correction_operation_unsupported"
            raise ContractError(code, "correct")


def _operand(
    boundary: tuple[GuardTarget, ...], tables: tuple[str, ...], identifier: str
) -> GuardTarget:
    selected = _selected_target(
        tuple(item for item in boundary if item.reference.table.value in tables),
        identifier,
    )
    require(len(selected) == 1, "decision_operand_ambiguous", "decide")
    return selected[0]


def _structural_target(
    boundary: tuple[GuardTarget, ...], request: DecisionRequest
) -> GuardTarget:
    if isinstance(request, IdentityRequest):
        require(
            len(request.members) == len(set(request.members)),
            "duplicate_decision_member",
            "decide",
        )
        addresses = tuple(
            (("review_sources",), value)
            for value in sorted({request.canonical, *request.members})
        )
    elif isinstance(request, RevisionRequest):
        addresses = tuple(
            (("review_statements",), value)
            for value in (*request.replaced, *request.replacement)
        )
    elif isinstance(request, TransferRequest):
        addresses = tuple(
            (("review_legs",), value)
            for value in (*request.outgoing, *request.incoming)
        )
    elif isinstance(request, DocumentLinkRequest):
        addresses = (
            (("review_events", "review_legs"), request.target),
            *((("review_documents",), value) for value in request.documents),
        )
    else:
        addresses = tuple(
            (("review_events",), value) for value in (request.attempt, request.fee)
        )
    require(bool(addresses), "decision_operands_required", "decide")
    selected = tuple(_operand(boundary, tables, value) for tables, value in addresses)
    if isinstance(request, DocumentLinkRequest):
        for target in selected[1:]:
            fields = {field.name: field for field in target.fields}
            require(
                "archive_only" in fields
                and fields["archive_only"].kind == "boolean"
                and not fields["archive_only"].nullable
                and fields["archive_only"].value is True
                and "blob_digest" in fields
                and fields["blob_digest"].kind == "string"
                and not fields["blob_digest"].nullable
                and fields["blob_digest"].value == target.reference.key[0],
                "archive_only_document_required",
                "decide",
            )
    return compound_target(selected)


def _structural_body(
    boundary: tuple[GuardTarget, ...],
    request: DecisionRequest,
    reviewed: ReviewBasisRef,
    target: GuardTarget,
) -> StructuralBody:
    entity = target.reference.entity

    def identifier(table: str, value: str) -> str:
        return str(_operand(boundary, (table,), value).reference.key[0])

    if isinstance(request, TransferRequest):

        def legs(values: tuple[str, ...]) -> tuple[RecordId, ...]:
            return tuple(
                RecordId(entity, RecordKind.LEG, value)
                for value in sorted(
                    identifier("review_legs", value) for value in values
                )
            )

        return TransferBody(
            entity,
            reviewed,
            legs(request.outgoing),
            legs(request.incoming),
            SemanticKey(request.commodity),
        )
    if isinstance(request, DocumentLinkRequest):
        native = _operand(boundary, ("review_events", "review_legs"), request.target)
        reference = (
            EventReviewRef(
                RecordId(entity, RecordKind.EVENT, str(native.reference.key[0]))
            )
            if native.reference.table.value == "review_events"
            else LegReviewRef(
                RecordId(entity, RecordKind.LEG, str(native.reference.key[0]))
            )
        )
        return DocumentLinkBody(
            entity,
            reviewed,
            reference,
            tuple(
                sorted(
                    identifier("review_documents", value) for value in request.documents
                )
            ),
        )
    if isinstance(request, LifecycleLinkRequest):
        return LifecycleLinkBody(
            entity,
            reviewed,
            EventReviewRef(
                RecordId(
                    entity,
                    RecordKind.EVENT,
                    identifier("review_events", request.attempt),
                )
            ),
            EventReviewRef(
                RecordId(
                    entity, RecordKind.EVENT, identifier("review_events", request.fee)
                )
            ),
        )
    if isinstance(request, IdentityRequest):
        operands = tuple(
            item for item in boundary if item.reference.table.value == "review_sources"
        )
        canonical = _selected_target(operands, request.canonical)[0]
        return IdentityBody(
            entity,
            reviewed,
            request.mode,
            RecordId(entity, RecordKind.SOURCE_RECORD, str(canonical.reference.key[0])),
            tuple(
                sorted(
                    (
                        RecordId(
                            entity, RecordKind.SOURCE_RECORD, str(reference.key[0])
                        )
                        for reference in compound_references(target)
                    ),
                    key=lambda value: value.value,
                )
            ),
        )
    operands = tuple(
        item for item in boundary if item.reference.table.value == "review_statements"
    )
    return StatementRevisionBody(
        entity,
        reviewed,
        tuple(
            sorted(
                str(_selected_target(operands, value)[0].reference.key[0])
                for value in request.replaced
            )
        ),
        tuple(
            sorted(
                str(_selected_target(operands, value)[0].reference.key[0])
                for value in request.replacement
            )
        ),
        SemanticKey(request.account),
        FactKind(request.fact_kind),
        SemanticKey(request.basis),
        request.start,
        request.end,
    )


def _correction_body(  # noqa: PLR0913 - exact operation and reviewed target
    target: GuardTarget,
    reviewed: ReviewBasisRef,
    tables: Mapping[str, object],
    *,
    field: str | None,
    value: str | None,
    children: tuple[str, ...],
) -> AssignBody | SplitBody:
    entity = target.reference.entity
    identifier = str(target.reference.key[0])
    is_leg = target.reference.table.value == "review_legs"
    require(
        target.reference.table.value in {"review_events", "review_legs"},
        "correction_target_kind",
        "correct",
    )
    reference = (
        LegReviewRef(RecordId(entity, RecordKind.LEG, identifier))
        if is_leg
        else EventReviewRef(RecordId(entity, RecordKind.EVENT, identifier))
    )
    if children:
        if not isinstance(reference, LegReviewRef) or field is not None:
            code = "split_leg_required"
            raise ContractError(code, "correct")
        decoded = []
        for child in children:
            child_key, separator, amount = child.partition("=")
            require(bool(separator), "split_child_syntax", "correct")
            decoded.append(
                SplitChild(SemanticKey(child_key), ExactDecimal.parse(amount))
            )
        return SplitBody(
            entity,
            reviewed,
            reference,
            tuple(sorted(decoded, key=lambda child: child.child_key.value)),
        )
    field = text(field, "assignment_field")
    try:
        assignment = _assignment(field, value)
    except (ValueError, RecursionError) as error:
        if isinstance(error, DomainError | ContractError):
            raise
        code = "correction_value_invalid"
        raise ContractError(code, "correct") from error
    require(
        field.startswith("postings." if is_leg else "transactions."),
        "assignment_target_kind",
        "correct",
    )
    if (
        isinstance(assignment, LegCategoryAssignment)
        and assignment.category is not None
    ):
        require(
            any(
                row["category_id"] == assignment.category.value
                for row in objects(tables["categories"], "categories")
            ),
            "category_not_registered",
            "correct",
        )
    return AssignBody(entity, reviewed, reference, (assignment,))


def _record(  # noqa: PLR0913 - explicit guarded command transaction
    root: Path,
    *,
    attempt: str,
    identifier: str,
    decision_key: str,
    reason: str,
    field: str | None,
    value: str | None,
    children: tuple[str, ...],
    supersede: str | None,
    request: DecisionRequest | None,
) -> dict[str, object]:
    """Append only against the exact head and native boundary the user saw."""
    require(bool(reason), "nonempty_reason_required", "correct")
    retained = report(root, attempt)
    require(
        retained["model"] is not None,
        "attempt_has_no_financial_determinations",
        "correct",
    )
    key = SemanticKey(decision_key)
    expected_head = decode_head(
        encode_json(
            document(retained["selection"])["journal_head"], integer_strings=False
        )
    )
    with writer_lock(root) as lock:
        journal = capture_journal(root, lock=lock)
        require(journal.head == expected_head, "review_head_stale", "correct")
        active = tuple(
            item.entry for item in journal.active_decisions if item.entry.key == key
        )
        require(
            len(active) == (1 if supersede else 0),
            "decision_active_revision",
            "correct",
        )
        require(
            supersede is not None
            or all(item.key != key for item in journal.committed_entries),
            "decision_key_already_recorded",
            "correct",
        )
        if supersede:
            require(
                supersede
                in {
                    active[0].key.value,
                    entry_digest(active[0]),
                    active[0].revision_id.value,
                },
                "supersede_active_entry_required",
                "correct",
            )
        stage = (
            DecisionStage.ENRICH
            if request is None
            else DecisionStage.MERGE
            if isinstance(request, IdentityRequest | RevisionRequest)
            else DecisionStage.LINK
        )
        boundary = _boundary(retained, key.value if supersede else None, stage=stage)
        if request is None:
            targets = _selected_target(boundary, identifier)
        else:
            targets = (_structural_target(boundary, request),)
        require(
            len(targets) == 1 and targets[0].active,
            "correction_target_not_resolved",
            "correct",
        )
        target = targets[0]
        model = document(retained["model"])
        tables = document(model["tables"])
        require(
            all(
                row["declaration_key"] != key.value
                or row["declaration_kind"]
                in {
                    "assign",
                    "split",
                    "identity",
                    "statement_revision",
                    "transfer",
                    "link",
                }
                for row in objects(tables["declarations"], "declarations")
            ),
            "decision_key_reserved",
            "correct",
        )
        entity = target.reference.entity
        basis = {
            "schema_version": 1,
            "attempt_digest": attempt,
            "report_digest": byte_digest(read_report(root, attempt)),
            "before_key": key.value if supersede else None,
            "target": write_target(target),
        }
        basis_payload = encode_json(basis, integer_strings=False)
        basis_digest = byte_digest(basis_payload)
        reviewed = ReviewBasisRef(attempt, basis_digest)
        if request is not None:
            body = _structural_body(boundary, request, reviewed, target)
            require(
                compound_references(target) == structural_references(body),
                "decision_basis_operands",
                "decide",
            )
        else:
            body = _correction_body(
                target, reviewed, tables, field=field, value=value, children=children
            )
        if supersede:
            prior_body = decode_decision_body(
                read_retained(root, text(active[0].payload_digest, "payload_digest"))
            )
            require(
                active[0].kind == decision_kind(body)
                and type(prior_body) is type(body),
                "decision_variant_mismatch",
                "correct",
            )
        put_retained(root, basis_payload, lock=lock)
        payload_digest = put_retained(root, encode_decision_body(body), lock=lock)
        entry = JournalEntry(
            journal.head.sequence + 1,
            journal.head.entry_digest,
            entity,
            key,
            decision_kind(body),
            JournalOperation.SUPERSEDE if supersede else JournalOperation.RECORD,
            entry_digest(active[0]) if supersede else None,
            attempt,
            reason,
            payload_digest,
        )
        head = append_journal(
            root,
            entry,
            expected_head=expected_head,
            retained_heads=retained_journal_heads(root, lock=lock),
            lock=lock,
        )
    return {
        "attempt_digest": attempt,
        "decision_key": key.value,
        "entry_digest": entry_digest(entry),
        "sequence": head.sequence,
    }


def correct(  # noqa: PLR0913 - explicit user correction operation
    root: Path,
    *,
    attempt: str,
    identifier: str,
    decision_key: str,
    reason: str,
    field: str | None,
    value: str | None,
    children: tuple[str, ...],
    supersede: str | None,
) -> dict[str, object]:
    """Record an enrichment decision against the retained native boundary."""
    return _record(
        root,
        attempt=attempt,
        identifier=identifier,
        decision_key=decision_key,
        reason=reason,
        field=field,
        value=value,
        children=children,
        supersede=supersede,
        request=None,
    )


def decide(  # noqa: PLR0913 - explicit reviewed structural operation
    root: Path,
    *,
    attempt: str,
    decision_key: str,
    reason: str,
    request: DecisionRequest,
    supersede: str | None,
) -> dict[str, object]:
    """Record explicit structural operands against a retained review boundary."""
    return _record(
        root,
        attempt=attempt,
        identifier="",
        decision_key=decision_key,
        reason=reason,
        field=None,
        value=None,
        children=(),
        supersede=supersede,
        request=request,
    )


def retract(
    root: Path, *, attempt: str, decision_key: str, reason: str
) -> dict[str, object]:
    """Disable the active revision without removing events or custody bytes."""
    require(bool(reason), "nonempty_reason_required", "retract")
    retained = report(root, attempt)
    require(
        document(retained["selection"])["journal_head"] is not None,
        "attempt_has_no_journal_selection",
        "retract",
    )
    expected_head = decode_head(
        encode_json(
            document(retained["selection"])["journal_head"], integer_strings=False
        )
    )
    with writer_lock(root) as lock:
        journal = capture_journal(root, lock=lock)
        require(journal.head == expected_head, "review_head_stale", "retract")
        active = tuple(
            item.entry
            for item in journal.active_decisions
            if decision_key
            in {
                item.entry.key.value,
                entry_digest(item.entry),
                item.entry.revision_id.value,
            }
            or (
                len(decision_key) >= _MINIMUM_ID_PREFIX
                and all(character in "0123456789abcdef" for character in decision_key)
                and entry_digest(item.entry).startswith(decision_key)
            )
        )
        require(len(active) == 1, "decision_active_revision", "retract")
        previous = active[0]
        entry = JournalEntry(
            journal.head.sequence + 1,
            journal.head.entry_digest,
            previous.entity,
            previous.key,
            previous.kind,
            JournalOperation.RETRACT,
            entry_digest(previous),
            attempt,
            reason,
            None,
        )
        head = append_journal(
            root,
            entry,
            expected_head=expected_head,
            retained_heads=retained_journal_heads(root, lock=lock),
            lock=lock,
        )
    return {
        "attempt_digest": attempt,
        "decision_key": decision_key,
        "entry_digest": entry_digest(entry),
        "sequence": head.sequence,
    }
