import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.integration.conformance.attempt_reader import (
    _body_chain as independent_body_chain,
)
from tests.integration.conformance.attempt_reader import (
    _boundary as independent_boundary,
)
from tests.integration.conformance.attempt_reader import read_attempt, validate_receipt
from tests.integration.conformance.reader import ReaderError

from jbt.artifacts.attempt import write_target
from jbt.artifacts.decisions import decode_decision_body, encode_decision_body
from jbt.artifacts.integrity import byte_digest, decode_document
from jbt.artifacts.reader import read_pinned
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import (
    DocumentLinkBody,
    LifecycleLinkBody,
    ReviewBasisRef,
    TransferBody,
    structural_references,
)
from jbt.domain.errors import GuardError
from jbt.domain.guards import GuardField, GuardReference, GuardTarget
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalHead,
    JournalOperation,
    encode_entry,
    entry_digest,
    select_journal,
)
from jbt.domain.review import compound_references, compound_target
from jbt.model.corrections import Decision, fold, targets
from jbt.model.quantity import resolve
from jbt.runtime import decisions
from jbt.runtime.decisions import (
    DecisionRequest,
    DocumentLinkRequest,
    LifecycleLinkRequest,
    TransferRequest,
    _correction_body,
    _structural_body,
    _structural_target,
    correct,
    retract,
)
from jbt.runtime.decisions import report as read_report
from jbt.runtime.inputs import capture_journal
from jbt.runtime.resolution import prepare, project_resolution
from jbt.runtime.worker import BuildAttemptError, build
from jbt.storage.local import acquire, put_retained, read_retained, writer_lock

if TYPE_CHECKING:
    from collections.abc import Callable

    from jbt.model.quantity import Resolution
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.project import Project
    from jbt.runtime.resolution import ExtractInput, Prepared
    from jbt.runtime.worker import BuildReport


def report(root: Path, attempt: str) -> dict:
    return cast("dict", read_report(root, attempt))


@pytest.fixture
def correction_root(tmp_path: Path, project_document: dict) -> Path:
    source = Path(__file__).parents[1] / "importers/fixtures_synthetic"
    with writer_lock(tmp_path) as lock:
        for name in ("prior.ofx", "main.ofx"):
            acquire(
                tmp_path,
                source / name,
                retry_key=name,
                acquired_at="2026-02-02T00:00:00Z",
                source_scope_id="bank-feed",
                importer_id="ofx",
                lock=lock,
            )
    project_document["declarations"] = [
        item
        for item in project_document["declarations"]
        if item["key"] != "rule-expense"
    ]
    project_document["configuration"]["outputs"] = [
        item
        for item in project_document["configuration"]["outputs"]
        if item["sink"] != "beancount"
    ]
    (tmp_path / "project.json").write_bytes(
        encode_json(project_document, integer_strings=False)
    )
    return tmp_path


def _build(root: Path, execution: ObservedExecution) -> BuildReport:
    return build(
        root, project="project.json", execution=execution, comparison_baseline=None
    )


def _posting(retained: dict) -> dict:
    return next(
        row
        for row in retained["model"]["tables"]["postings"]
        if row["leg_kind"] == "boundary" and row["amount_coefficient"] == "5"
    )


@pytest.fixture
def assignment_context(
    project_document: dict,
    make_inputs: Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]],
) -> tuple[Prepared, Resolution]:
    project_document["declarations"] = [
        item
        for item in project_document["declarations"]
        if item["key"] != "rule-expense"
    ]
    project, extracts = make_inputs(project_document)
    prepared = prepare(project, extracts)
    return prepared, resolve(prepared.inputs)


def test_real_category_correction_and_rule_independent_guard(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    initial = _build(correction_root, release_execution)
    before = report(correction_root, initial["attempt_digest"])
    posting = _posting(before)
    assert posting["category_id"] is None
    head = (correction_root / "journal/head.json").read_bytes()
    reserved = next(
        row["declaration_key"]
        for row in before["model"]["tables"]["declarations"]
        if row["declaration_kind"] not in {"assign", "split"}
    )
    for key, field, value, code in (
        (reserved, "postings.category_id", "expense", "decision_key_reserved"),
        ("amount", "postings.amount", "6", "correction_field_prohibited"),
        ("date", "transactions.date_authorized", "", "correction_value_invalid"),
        (
            "review",
            "transactions.review_state",
            "not-a-review-state",
            "correction_value_invalid",
        ),
        ("tags", "transactions.tags", "[[]]", "correction_tags"),
    ):
        with pytest.raises(ContractError, match=code):
            correct(
                correction_root,
                attempt=initial["attempt_digest"],
                identifier=posting[
                    "txn_id" if field.startswith("transactions.") else "posting_id"
                ],
                decision_key=key,
                reason="invalid boundary request",
                field=field,
                value=value,
                children=(),
                supersede=None,
            )
        assert (correction_root / "journal/head.json").read_bytes() == head
    correct(
        correction_root,
        attempt=initial["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason=" Synthetic exact reason \n",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    head = (correction_root / "journal/head.json").read_bytes()
    with pytest.raises(ContractError, match="review_head_stale"):
        correct(
            correction_root,
            attempt=initial["attempt_digest"],
            identifier=posting["posting_id"],
            decision_key="stale",
            reason="must use a new reviewed attempt",
            field="postings.category_id",
            value="expense",
            children=(),
            supersede=None,
        )
    assert (correction_root / "journal/head.json").read_bytes() == head
    after = _build(correction_root, release_execution)
    retained = report(correction_root, after["attempt_digest"])
    assert read_attempt(correction_root, after["attempt_digest"]) == retained
    receipt = retained["model"]["review"]["receipts"][0]
    assert set(retained["model"]["review"]) == {
        "tail",
        "receipts",
        "rules",
        "counts",
        "proposals",
    }
    assert len(receipt["candidates"]) == 1
    assert receipt["candidates"][0]["key"] == receipt["expected"]["key"]
    weakened = deepcopy(receipt)
    weakened["expected"]["fields"] = [
        field for field in weakened["expected"]["fields"] if field["name"] != "accounts"
    ]
    with pytest.raises(ReaderError):
        validate_receipt(weakened)
    forged = deepcopy(receipt)
    forged["outcome"] = "changed"
    with pytest.raises(ReaderError):
        validate_receipt(forged)
    unscoped = deepcopy(receipt)
    unscoped["candidates"].append(
        next(
            target
            for target in retained["model"]["review"]["tail"]
            if target["key"] != receipt["expected"]["key"]
        )
    )
    with pytest.raises(ReaderError, match="target-scoped pre-entry candidates"):
        validate_receipt(unscoped)
    changed = _posting(retained)
    assert changed["posting_id"] == posting["posting_id"]
    assert changed["category_id"] == "expense"
    assert changed["amount_coefficient"] == posting["amount_coefficient"]
    assert retained["model"]["review"]["receipts"][0]["outcome"] == "applied"
    assert any(
        row["value_origin"] == "correction"
        for row in retained["model"]["tables"]["provenance"]
    )
    read_pinned(correction_root, after["descriptor_digest"])


def test_clearing_effective_date_has_specific_blocking_finding(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    posting = _posting(report(correction_root, first["attempt_digest"]))
    correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["txn_id"],
        decision_key="date",
        reason="Explicitly unknown effective date",
        field="transactions.date_posted",
        value=None,
        children=(),
        supersede=None,
    )
    current = (correction_root / "current.json").read_bytes()
    with pytest.raises(BuildAttemptError) as failed:
        _build(correction_root, release_execution)
    retained = read_attempt(correction_root, failed.value.attempt)
    findings = retained["model"]["findings"]
    assert any(
        item["code"] == "correction_effective_date_required" and item["blocking"]
        for item in findings
    )
    assert not any(item["code"] == "decision_unsupported" for item in findings)
    assert (
        next(
            item
            for item in retained["model"]["determinations"]
            if item["record_id"] == posting["txn_id"]
        )["status"]
        == "blocked"
    )
    assert (correction_root / "current.json").read_bytes() == current


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("transactions.payee", " e\u0301 \n", " e\u0301 \n"),
        ("transactions.tags", '[" padded ","é"]', [" padded ", "é"]),
        ("transactions.review_state", "reviewed", "reviewed"),
        ("transactions.date_posted", "2026-01-21", "2026-01-21"),
        ("postings.date_posted", "2026-01-21", "2026-01-21"),
    ],
)
def test_supported_assignments_update_native_projected_values(
    assignment_context: tuple[Prepared, Resolution],
    field: str,
    value: str,
    expected: object,
) -> None:
    prepared, resolution = assignment_context
    before = project_resolution(prepared, resolution)
    posting = next(
        row
        for row in before["postings"]
        if row["leg_kind"] == "boundary" and row["amount_coefficient"] == "5"
    )
    table, column = field.split(".")
    identifier = posting["txn_id"] if table == "transactions" else posting["posting_id"]
    target = next(
        target
        for target in targets(resolution)
        if target.reference.key == (identifier,)
    )
    body = _correction_body(
        target,
        ReviewBasisRef("a" * 64, "b" * 64),
        before,
        field=field,
        value=value,
        children=(),
    )
    entry = JournalEntry(
        1,
        None,
        prepared.inputs.entity,
        SemanticKey("assignment"),
        DecisionKind.ASSIGN,
        JournalOperation.RECORD,
        None,
        body.review.attempt_digest,
        "review synthetic value",
        "c" * 64,
    )
    corrected = fold(prepared.inputs, resolution, (Decision(entry, body, target),))
    assert corrected.receipts[0].evaluation.outcome.value == "applied"
    projected = project_resolution(prepared, corrected.resolution)
    row = next(
        item
        for item in projected[table]
        if item["txn_id" if table == "transactions" else "posting_id"] == identifier
    )
    assert row[column] == expected
    assert (
        next(
            row["amount_coefficient"]
            for row in projected["postings"]
            if row["posting_id"] == posting["posting_id"]
        )
        == posting["amount_coefficient"]
    )


def test_conflicting_assignments_are_complete_and_retractable(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    posting = _posting(report(correction_root, first["attempt_digest"]))
    correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="first",
        reason="one",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    second = _build(correction_root, release_execution)
    correct(
        correction_root,
        attempt=second["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="second",
        reason="two",
        field="postings.category_id",
        value="income",
        children=(),
        supersede=None,
    )
    current = (correction_root / "current.json").read_bytes()
    with pytest.raises(BuildAttemptError) as failure:
        _build(correction_root, release_execution)
    blocked = report(correction_root, failure.value.attempt)
    assert any(
        finding["code"] == "correction_assignment_conflict"
        for finding in blocked["model"]["findings"]
    )
    assert len(blocked["model"]["review"]["receipts"]) == 2
    assert (correction_root / "current.json").read_bytes() == current
    retract(
        correction_root,
        attempt=failure.value.attempt,
        decision_key="second",
        reason="remove competing classification",
    )
    final = _build(correction_root, release_execution)
    assert (
        _posting(report(correction_root, final["attempt_digest"]))["category_id"]
        == "expense"
    )
    head = (correction_root / "journal/head.json").read_bytes()
    with pytest.raises(ContractError, match="decision_key_already_recorded"):
        correct(
            correction_root,
            attempt=final["attempt_digest"],
            identifier=posting["posting_id"],
            decision_key="second",
            reason="a retired key cannot be reused",
            field="postings.category_id",
            value="income",
            children=(),
            supersede=None,
        )
    assert (correction_root / "journal/head.json").read_bytes() == head


def test_split_supersession_preserves_children_and_rechecks_dependents(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    parent = _posting(report(correction_root, first["attempt_digest"]))
    split = correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=parent["posting_id"],
        decision_key="split-A",
        reason="allocate",
        field=None,
        value=None,
        children=("a=2", "b=3"),
        supersede=None,
    )
    second = _build(correction_root, release_execution)
    divided = report(correction_root, second["attempt_digest"])
    children = [
        row
        for row in divided["model"]["tables"]["postings"]
        if row["txn_id"] == parent["txn_id"] and row["leg_kind"] == "boundary"
    ]
    child = next(row for row in children if row["amount_coefficient"] == "2")
    correct(
        correction_root,
        attempt=second["attempt_digest"],
        identifier=child["posting_id"],
        decision_key="child-B",
        reason="classify child",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    third = _build(correction_root, release_execution)
    revision = correct(
        correction_root,
        attempt=third["attempt_digest"],
        identifier=parent["posting_id"],
        decision_key="split-A",
        reason="same allocation",
        field=None,
        value=None,
        children=("a=2", "b=3"),
        supersede=cast("str", split["entry_digest"]),
    )
    fourth = _build(correction_root, release_execution)
    same = report(correction_root, fourth["attempt_digest"])
    assert read_attempt(correction_root, fourth["attempt_digest"]) == same
    assert [
        receipt["decision_key"] for receipt in same["model"]["review"]["receipts"]
    ] == ["split-A", "child-B"]
    assert [receipt["outcome"] for receipt in same["model"]["review"]["receipts"]] == [
        "applied",
        "applied",
    ]
    assert [
        [candidate["key"] for candidate in receipt["candidates"]]
        for receipt in same["model"]["review"]["receipts"]
    ] == [[[parent["posting_id"]]], [[child["posting_id"]]]]
    assert any(
        row["posting_id"] == child["posting_id"] and row["category_id"] == "expense"
        for row in same["model"]["tables"]["postings"]
    )
    correct(
        correction_root,
        attempt=fourth["attempt_digest"],
        identifier=parent["posting_id"],
        decision_key="split-A",
        reason="changed allocation",
        field=None,
        value=None,
        children=("a=1", "b=4"),
        supersede=cast("str", revision["entry_digest"]),
    )
    with pytest.raises(BuildAttemptError) as failed:
        _build(correction_root, release_execution)
    changed = report(correction_root, failed.value.attempt)
    assert read_attempt(correction_root, failed.value.attempt) == changed
    assert changed["model"]["review"]["receipts"][1]["outcome"] == "changed"
    assert any(
        row["posting_id"] == child["posting_id"] and row["amount_coefficient"] == "1"
        for row in changed["model"]["tables"]["postings"]
    )


def test_split_supersession_can_retarget_at_its_original_pre_entry_boundary(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    initial = report(correction_root, first["attempt_digest"])
    original = _posting(initial)
    destination = next(
        row
        for row in initial["model"]["tables"]["postings"]
        if row["leg_kind"] == "boundary" and row["amount_coefficient"] == "-20"
    )
    recorded = correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=original["posting_id"],
        decision_key="retarget",
        reason="initial selection",
        field=None,
        value=None,
        children=("a=2", "b=3"),
        supersede=None,
    )
    second = _build(correction_root, release_execution)
    later = correct(
        correction_root,
        attempt=second["attempt_digest"],
        identifier=destination["txn_id"],
        decision_key="later-date",
        reason="a later decision changes the destination's guarded facts",
        field="transactions.date_posted",
        value="2026-01-13",
        children=(),
        supersede=None,
    )
    third = _build(correction_root, release_execution)
    reviewed = read_attempt(correction_root, third["attempt_digest"])
    assert (
        next(
            row
            for row in reviewed["model"]["tables"]["transactions"]
            if row["txn_id"] == destination["txn_id"]
        )["date_posted"]
        == "2026-01-13"
    )
    child = next(
        row
        for row in reviewed["model"]["tables"]["postings"]
        if row["txn_id"] == original["txn_id"] and row["leg_kind"] == "boundary"
    )
    head = (correction_root / "journal/head.json").read_bytes()
    with pytest.raises(ContractError, match="review_target_missing_or_ambiguous"):
        correct(
            correction_root,
            attempt=third["attempt_digest"],
            identifier=child["posting_id"],
            decision_key="retarget",
            reason="a split cannot retarget onto its own output",
            field=None,
            value=None,
            children=("a=1", "b=1"),
            supersede=cast("str", recorded["entry_digest"]),
        )
    assert (correction_root / "journal/head.json").read_bytes() == head
    correct(
        correction_root,
        attempt=third["attempt_digest"],
        identifier=destination["posting_id"],
        decision_key="retarget",
        reason="deliberately select the other purchase without moving the decision",
        field=None,
        value=None,
        children=("a=-8", "b=-12"),
        supersede=cast("str", recorded["entry_digest"]),
    )
    with pytest.raises(BuildAttemptError) as failed:
        _build(correction_root, release_execution)
    blocked = read_attempt(correction_root, failed.value.attempt)
    assert [
        receipt["outcome"] for receipt in blocked["model"]["review"]["receipts"]
    ] == ["applied", "changed"]
    correct(
        correction_root,
        attempt=failed.value.attempt,
        identifier=destination["txn_id"],
        decision_key="later-date",
        reason="review the destination's changed split structure",
        field="transactions.date_posted",
        value="2026-01-13",
        children=(),
        supersede=cast("str", later["entry_digest"]),
    )
    final = _build(correction_root, release_execution)
    retained = read_attempt(correction_root, final["attempt_digest"])
    receipts = retained["model"]["review"]["receipts"]
    assert [receipt["decision_key"] for receipt in receipts] == [
        "retarget",
        "later-date",
    ]
    assert [receipt["outcome"] for receipt in receipts] == ["applied", "applied"]
    dates = next(
        field["value"]
        for field in receipts[0]["expected"]["fields"]
        if field["name"] == "dates"
    )
    assert json.loads(dates)[2] == "2026-01-12"
    tables = retained["model"]["tables"]
    assert _posting(retained)["category_id"] is None
    assert [
        row["amount_coefficient"]
        for row in tables["postings"]
        if row["txn_id"] == destination["txn_id"] and row["leg_kind"] == "boundary"
    ] == ["-8", "-12"]


def _native_operand(table: str, key: str) -> GuardTarget:
    fields = (
        (
            GuardField("archive_only", "boolean", False, True),  # noqa: FBT003
            GuardField("blob_digest", "string", False, key),  # noqa: FBT003
        )
        if table == "review_documents"
        else (GuardField("facts", "string", False, "native facts"),)  # noqa: FBT003
    )
    return GuardTarget(
        GuardReference(EntityId("synthetic"), SemanticKey(table), (key,)),
        fields,
        active=True,
    )


@pytest.fixture
def structural_review(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, tuple[GuardTarget, ...]]:
    inventory = tuple(
        _native_operand(table, digit * 64)
        for table, digit in (
            ("review_legs", "b"),
            ("review_legs", "c"),
            ("review_events", "d"),
            ("review_events", "e"),
            ("review_documents", "f"),
        )
    )
    reviewed = {
        "selection": {
            "journal_head": {"schema_version": 1, "sequence": 0, "entry_digest": None}
        },
        "model": {
            "tables": {"declarations": []},
            "findings": [],
            "review": {
                "tail": [write_target(item) for item in inventory],
                "receipts": [],
            },
        },
    }
    monkeypatch.setattr(decisions, "report", lambda *_args: reviewed)
    monkeypatch.setattr(decisions, "read_report", lambda *_args: b"synthetic report")
    monkeypatch.setattr(decisions, "_reviewed_report", lambda *_args: reviewed)
    return reviewed, inventory


@pytest.mark.parametrize(
    "operation",
    [
        TransferRequest(("b" * 12,), ("c" * 12,), "NZD"),
        TransferRequest((), ("c" * 64,), "NZD"),
        DocumentLinkRequest("b" * 64, ("f" * 64,)),
        DocumentLinkRequest("d" * 64, ("f" * 64,)),
        LifecycleLinkRequest("d" * 12, "e" * 12),
    ],
)
@pytest.mark.usefixtures("structural_review")
def test_structural_commands_persist_exact_authenticated_operands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: DecisionRequest,
) -> None:
    result = decisions.decide(
        tmp_path,
        attempt="a" * 64,
        decision_key="assertion",
        reason="explicit assertion",
        request=operation,
        supersede=None,
    )
    with writer_lock(tmp_path) as lock:
        journal = capture_journal(tmp_path, lock=lock)
    entry = journal.committed_entries[0]
    assert entry.payload_digest is not None
    body = decode_decision_body(read_retained(tmp_path, entry.payload_digest))
    selected = decisions.authenticate_decisions(
        tmp_path,
        journal=journal,
        bases=(body.review.basis_digest,),
        reports=(("a" * 64, byte_digest(b"synthetic report")),),
    )
    assert selected[0].body == body
    assert result["sequence"] == 1
    assert isinstance(body, TransferBody | DocumentLinkBody | LifecycleLinkBody)
    assert structural_references(body) == compound_references(selected[0].expected)
    assert entry.reason == "explicit assertion"
    if isinstance(body, TransferBody):
        forged = replace(
            body,
            outgoing=(),
            incoming=(replace((body.outgoing or body.incoming)[0], value="1" * 64),),
        )
    elif isinstance(body, DocumentLinkBody):
        forged = replace(body, documents=("1" * 64,))
    else:
        forged = replace(
            body, fee=replace(body.fee, event=replace(body.fee.event, value="1" * 64))
        )
    monkeypatch.setattr(decisions, "decode_decision_body", lambda _payload: forged)
    with pytest.raises(ContractError, match="decision_basis_operands"):
        decisions.authenticate_decisions(
            tmp_path,
            journal=journal,
            bases=(body.review.basis_digest,),
            reports=(("a" * 64, byte_digest(b"synthetic report")),),
        )


@pytest.mark.parametrize(
    "operation",
    [
        TransferRequest((), (), "NZD"),
        TransferRequest(("b" * 64, "b" * 12), (), "NZD"),
        TransferRequest(("b" * 64,), ("b" * 64,), "NZD"),
        TransferRequest(("d" * 64,), (), "NZD"),
        DocumentLinkRequest("d" * 64, ("b" * 64,)),
        LifecycleLinkRequest("d" * 64, "d" * 64),
        LifecycleLinkRequest("d" * 64, "b" * 64),
    ],
)
def test_structural_commands_do_not_infer_or_reuse_operands(
    structural_review: tuple[dict, tuple[GuardTarget, ...]],
    operation: DecisionRequest,
) -> None:
    with pytest.raises((ContractError, GuardError)):
        _structural_target(structural_review[1], operation)


def test_document_show_and_archive_only_selection(
    tmp_path: Path,
    structural_review: tuple[dict, tuple[GuardTarget, ...]],
) -> None:
    _, inventory = structural_review
    document = inventory[-1]
    shown = decisions.show(tmp_path, attempt="a" * 64, identifier="f" * 64)
    assert shown["targets"] == [write_target(document)]
    assert "transactions" not in shown
    invalid = replace(
        document,
        fields=(
            replace(document.fields[0], value=False),
            document.fields[1],
        ),
    )
    with pytest.raises(ContractError, match="archive_only_document_required"):
        _structural_target(
            (*inventory[:-1], invalid),
            DocumentLinkRequest("d" * 64, ("f" * 64,)),
        )


def test_link_subtype_cannot_change_at_the_same_key_even_in_retracted_history(
    tmp_path: Path,
    structural_review: tuple[dict, tuple[GuardTarget, ...]],
) -> None:
    reviewed, inventory = structural_review
    document = DocumentLinkRequest("d" * 64, ("f" * 64,))
    decisions.decide(
        tmp_path,
        attempt="a" * 64,
        decision_key="evidence",
        reason="receipt",
        request=document,
        supersede=None,
    )
    with writer_lock(tmp_path) as lock:
        journal = capture_journal(tmp_path, lock=lock)
    original = journal.committed_entries[0]
    reviewed["selection"]["journal_head"] = {
        "schema_version": 1,
        "sequence": journal.head.sequence,
        "entry_digest": journal.head.entry_digest,
    }
    reviewed["model"]["tables"]["declarations"] = [
        {"declaration_key": "evidence", "declaration_kind": "link"},
    ]
    expected = write_target(_structural_target(inventory, document))
    reviewed["model"]["review"]["receipts"] = [
        {
            "decision_key": "evidence",
            "expected": expected,
            "candidates": [expected],
            "restore": [],
        }
    ]
    lifecycle = LifecycleLinkRequest("d" * 64, "e" * 64)
    head = (tmp_path / "journal/head.json").read_bytes()
    with pytest.raises(ContractError, match="decision_variant_mismatch"):
        decisions.decide(
            tmp_path,
            attempt="a" * 64,
            decision_key="evidence",
            reason="different assertion",
            request=lifecycle,
            supersede=entry_digest(original),
        )
    assert (tmp_path / "journal/head.json").read_bytes() == head

    target = _structural_target(inventory, lifecycle)
    basis = encode_json(
        {
            "schema_version": 1,
            "attempt_digest": "a" * 64,
            "report_digest": byte_digest(b"synthetic report"),
            "before_key": "evidence",
            "target": write_target(target),
        },
        integer_strings=False,
    )
    with writer_lock(tmp_path) as lock:
        basis_digest = put_retained(tmp_path, basis, lock=lock)
        body = _structural_body(
            inventory,
            lifecycle,
            ReviewBasisRef("a" * 64, basis_digest),
            target,
        )
        payload_digest = put_retained(tmp_path, encode_decision_body(body), lock=lock)
    external = replace(
        original,
        sequence=2,
        previous_entry_digest=entry_digest(original),
        operation=JournalOperation.SUPERSEDE,
        prior_entry_digest=entry_digest(original),
        payload_digest=payload_digest,
    )
    retracted = replace(
        external,
        sequence=3,
        previous_entry_digest=entry_digest(external),
        operation=JournalOperation.RETRACT,
        prior_entry_digest=entry_digest(external),
        payload_digest=None,
    )
    selected = select_journal(
        (original, external, retracted),
        head=JournalHead(3, entry_digest(retracted)),
        retained_heads=(),
    )
    assert selected.active_decisions == ()
    assert original.payload_digest is not None
    original_body = decode_decision_body(
        read_retained(tmp_path, original.payload_digest)
    )
    with pytest.raises(ContractError, match="decision_variant_mismatch"):
        decisions.authenticate_decisions(
            tmp_path,
            journal=selected,
            bases=tuple(sorted((original_body.review.basis_digest, basis_digest))),
            reports=(("a" * 64, byte_digest(b"synthetic report")),),
        )
    with pytest.raises(ReaderError, match="same sealed body variant"):
        independent_body_chain(
            [encode_entry(item) for item in selected.committed_entries],
            {
                digest: read_retained(tmp_path, digest)
                for digest in (original.payload_digest, payload_digest)
            },
        )


def test_transfer_supersession_binds_original_position_boundary(
    tmp_path: Path,
    structural_review: tuple[dict, tuple[GuardTarget, ...]],
) -> None:
    reviewed, inventory = structural_review
    request = TransferRequest(("b" * 64,), (), "NZD")
    recorded = decisions.decide(
        tmp_path,
        attempt="a" * 64,
        decision_key="transfer",
        reason="departure",
        request=request,
        supersede=None,
    )
    with writer_lock(tmp_path) as lock:
        journal = capture_journal(tmp_path, lock=lock)
    reviewed["selection"]["journal_head"] = {
        "schema_version": 1,
        "sequence": journal.head.sequence,
        "entry_digest": journal.head.entry_digest,
    }
    first = inventory[0]
    changed = replace(
        first,
        fields=(GuardField("facts", "string", nullable=False, value="later facts"),),
    )
    reviewed["model"]["review"] = {
        "tail": [
            write_target(changed),
            *[write_target(item) for item in inventory[1:]],
        ],
        "receipts": [
            {
                "decision_key": "transfer",
                "expected": write_target(compound_target((first,))),
                "candidates": [write_target(compound_target((first,)))],
                "restore": [
                    {
                        "entity_id": first.reference.entity.value,
                        "table": "review_legs",
                        "key": ["b" * 64],
                        "candidates": [write_target(first)],
                    }
                ],
            }
        ],
    }
    completed = TransferRequest(("b" * 64,), ("c" * 64,), "NZD")
    decisions.decide(
        tmp_path,
        attempt="a" * 64,
        decision_key="transfer",
        reason="arrival",
        request=completed,
        supersede=str(recorded["entry_digest"]),
    )
    with writer_lock(tmp_path) as lock:
        revised = capture_journal(tmp_path, lock=lock)
    entry = revised.committed_entries[-1]
    assert entry.payload_digest is not None
    body = decode_decision_body(read_retained(tmp_path, entry.payload_digest))
    basis = decode_document(read_retained(tmp_path, body.review.basis_digest), "basis")
    assert basis["before_key"] == "transfer"
    assert basis["target"] == write_target(_structural_target(inventory, completed))


def test_event_correction_after_transfer_seals_its_enrichment_stage_tail(
    tmp_path: Path,
    structural_review: tuple[dict, tuple[GuardTarget, ...]],
    assignment_context: tuple[Prepared, Resolution],
) -> None:
    reviewed, _ = structural_review
    inventory = targets(assignment_context[1])
    event = next(
        item for item in inventory if item.reference.table.value == "review_events"
    )
    leg = next(
        item
        for item in inventory
        if item.reference.table.value == "review_legs"
        and json.loads(
            str(next(field.value for field in item.fields if field.name == "leg"))
        )["kind"]
        == "position"
    )
    reviewed["model"]["review"]["tail"] = [write_target(item) for item in inventory]
    recorded = decisions.decide(
        tmp_path,
        attempt="a" * 64,
        decision_key="move",
        reason="departure",
        request=TransferRequest((str(leg.reference.key[0]),), (), "NZD"),
        supersede=None,
    )
    with writer_lock(tmp_path) as lock:
        journal = capture_journal(tmp_path, lock=lock)
    reviewed["selection"]["journal_head"] = {
        "schema_version": 1,
        "sequence": journal.head.sequence,
        "entry_digest": journal.head.entry_digest,
    }
    legs_field = next(field for field in event.fields if field.name == "legs")
    legs = json.loads(str(legs_field.value))
    legs[0]["transfer"] = "move"
    changed = replace(
        event,
        fields=tuple(
            replace(
                field, value=encode_json(legs, integer_strings=False).decode().strip()
            )
            if field.name == "legs"
            else field
            for field in event.fields
        ),
    )
    expected = write_target(compound_target((leg,)))
    reviewed["model"]["tables"]["declarations"] = [
        {"declaration_key": "move", "declaration_kind": "transfer"},
    ]
    reviewed["model"]["review"] = {
        "tail": [
            write_target(changed if item == event else item) for item in inventory
        ],
        "receipts": [
            {
                "decision_key": "move",
                "entry_digest": recorded["entry_digest"],
                "expected": expected,
                "candidates": [expected],
                "restore": [
                    {
                        "entity_id": event.reference.entity.value,
                        "table": "review_events",
                        "key": list(event.reference.key),
                        "candidates": [write_target(event)],
                    }
                ],
            }
        ],
    }
    assert write_target(event) in independent_boundary(reviewed, None, kind="assign")
    assert write_target(changed) in independent_boundary(reviewed, None)
    correct(
        tmp_path,
        attempt="a" * 64,
        identifier=str(event.reference.key[0]),
        decision_key="payee",
        reason="enrichment before link treatment",
        field="transactions.payee",
        value="Reviewed",
        children=(),
        supersede=None,
    )
    with writer_lock(tmp_path) as lock:
        updated = capture_journal(tmp_path, lock=lock)
    entry = updated.committed_entries[-1]
    assert entry.payload_digest is not None
    body = decode_decision_body(read_retained(tmp_path, entry.payload_digest))
    basis = decode_document(read_retained(tmp_path, body.review.basis_digest), "basis")
    assert basis["before_key"] is None
    assert basis["target"] == write_target(event)
    assert (
        decisions.authenticate_decisions(
            tmp_path,
            journal=updated,
            bases=tuple(
                sorted(
                    decode_decision_body(
                        read_retained(tmp_path, item.payload_digest)
                    ).review.basis_digest
                    for item in updated.committed_entries
                    if item.payload_digest is not None
                )
            ),
            reports=(("a" * 64, byte_digest(b"synthetic report")),),
        )[-1].expected
        == event
    )
