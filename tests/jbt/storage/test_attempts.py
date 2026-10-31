import json
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.integration.conformance.attempt_reader import read_attempt
from tests.integration.conformance.reader import ReaderError
from tests.jbt.runtime.test_decisions import _build, _posting, report
from tests.jbt.runtime.test_decisions import (
    correction_root as correction_root,  # noqa: PLC0414 - fixture registration
)

from jbt.artifacts.attempt import encode_review_report
from jbt.artifacts.integrity import byte_digest
from jbt.artifacts.journal import decode_entry
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.domain.ids import SemanticKey
from jbt.domain.journal import (
    JournalHead,
    JournalOperation,
    encode_entry,
    encode_head,
    entry_digest,
)
from jbt.runtime.decisions import correct
from jbt.runtime.inputs import InputCaptureError, retained_journal_heads
from jbt.runtime.worker import verify
from jbt.storage.attempts import read_report, retain_attempt, retain_report, selections
from jbt.storage.journal import append_journal, read_journal
from jbt.storage.local import StorageError, acquire, put_retained, writer_lock

if TYPE_CHECKING:
    from jbt.runtime.execution import ObservedExecution

pytestmark = pytest.mark.usefixtures("installed_ofx_release")


def test_exact_pinned_replay_ignores_later_live_journal_and_root_paths(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    posting = _posting(report(correction_root, first["attempt_digest"]))
    correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason="review",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    baseline = _build(correction_root, release_execution)
    correct(
        correction_root,
        attempt=baseline["attempt_digest"],
        identifier=posting["txn_id"],
        decision_key="description",
        reason="later description",
        field="transactions.narration",
        value="later",
        children=(),
        supersede=None,
    )
    (correction_root / "project.json").unlink()
    if (correction_root / "cache").exists():
        shutil.rmtree(correction_root / "cache")
    (correction_root / "journal/head.json").write_bytes(b"damaged later metadata\n")
    (correction_root / "journal/entries/0000000000000000002.json").write_bytes(
        b"damaged later entry\n"
    )
    (correction_root / "attempts/index.json").write_bytes(b"damaged later index\n")
    (correction_root / "attempts" / f"{first['attempt_digest']}.report").write_bytes(
        b"damaged later reviewed-report reference\n"
    )
    moved = correction_root.parent / "relocated"
    correction_root.rename(moved)
    result = verify(
        moved,
        baseline_digest=baseline["descriptor_digest"],
        execution=release_execution,
    )
    assert result["equal"]
    assert result["reused"] == 0
    assert read_attempt(moved, baseline["attempt_digest"])["failure"] is None


def test_capture_and_independent_reader_reject_forged_reviewed_report(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    original = report(correction_root, first["attempt_digest"])
    posting = _posting(original)
    correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason="review",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    baseline = _build(correction_root, release_execution)
    source = Path(__file__).parents[1] / "importers/fixtures_synthetic/main.ofx"
    revised = correction_root.parent / f"{correction_root.name}-forged-review.ofx"
    revised.write_bytes(
        source.read_bytes().replace(b"-5.00", b"-6.00").replace(b"115.00", b"114.00")
    )
    with writer_lock(correction_root) as lock:
        acquired = acquire(
            correction_root,
            revised,
            retry_key="revision",
            acquired_at="2026-02-03T00:00:00Z",
            source_scope_id="bank-feed",
            importer_id="ofx",
            lock=lock,
        )
        project = json.loads((correction_root / "project.json").read_bytes())
        project["sources"][-1] = acquired.accession_id
        (correction_root / "project.json").write_bytes(
            encode_json(project, integer_strings=False)
        )
        history = read_journal(correction_root, head=None, retained_heads=())
        entry = history.committed_entries[0]
        assert entry.payload_digest is not None
        body = json.loads(
            (correction_root / "inputs" / entry.payload_digest).read_bytes()
        )
        basis = json.loads(
            (correction_root / "inputs" / body["review"]["basis_digest"]).read_bytes()
        )
        forged_report = deepcopy(original)
        target = next(
            item
            for item in forged_report["model"]["review"]["tail"]
            if item["key"] == [posting["posting_id"]]
        )
        leg_field = next(field for field in target["fields"] if field["name"] == "leg")
        leg = json.loads(leg_field["value"])
        leg["amount"]["coefficient"] = "6"
        leg_field["value"] = encode_json(leg, integer_strings=False).decode().strip()
        basis["target"] = target
        basis["report_digest"] = put_retained(
            correction_root, encode_review_report(forged_report), lock=lock
        )
        assert basis["report_digest"] != byte_digest(
            read_report(correction_root, first["attempt_digest"])
        )
        body["review"]["basis_digest"] = put_retained(
            correction_root, encode_json(basis, integer_strings=False), lock=lock
        )
        forged_entry = replace(
            entry,
            sequence=2,
            previous_entry_digest=entry_digest(entry),
            operation=JournalOperation.SUPERSEDE,
            prior_entry_digest=entry_digest(entry),
            payload_digest=put_retained(
                correction_root, encode_json(body, integer_strings=False), lock=lock
            ),
        )
        head = append_journal(
            correction_root,
            forged_entry,
            expected_head=history.head,
            retained_heads=retained_journal_heads(correction_root, lock=lock),
            lock=lock,
        )
        baseline_report = report(correction_root, baseline["attempt_digest"])
        witness = json.loads(
            (correction_root / "inputs" / baseline["attempt_digest"]).read_bytes()
        )
        selected = json.loads(
            (correction_root / "inputs" / witness["input_digest"]).read_bytes()
        )
        selected["journal"]["head"] = json.loads(encode_head(head))
        selected["journal"]["headers"].append(
            put_retained(correction_root, encode_entry(forged_entry), lock=lock)
        )
        selected["journal"]["bases"] = sorted(
            [*selected["journal"]["bases"], body["review"]["basis_digest"]]
        )
        digest = put_retained(
            correction_root, encode_json(selected, integer_strings=False), lock=lock
        )
        forged_attempt = retain_attempt(correction_root, digest, lock=lock)
        baseline_report["attempt_digest"] = forged_attempt
        baseline_report["selection"]["input_digest"] = digest
        retain_report(
            correction_root,
            forged_attempt,
            encode_review_report(baseline_report),
            lock=lock,
        )
    current = (correction_root / "current.json").read_bytes()
    with pytest.raises(InputCaptureError, match="decision_report_binding") as failure:
        _build(correction_root, release_execution)
    assert report(correction_root, failure.value.attempt)["model"] is None
    assert (correction_root / "current.json").read_bytes() == current
    with pytest.raises(ReaderError, match="reviewed report role binding"):
        read_attempt(correction_root, forged_attempt)


def test_capture_rejects_journal_damage_with_retained_located_error(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    posting = _posting(report(correction_root, first["attempt_digest"]))
    correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason="review",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    _build(correction_root, release_execution)
    entry = correction_root / "journal/entries/0000000000000000001.json"
    head = correction_root / "journal/head.json"
    original_entry, original_head = entry.read_bytes(), head.read_bytes()
    current = (correction_root / "current.json").read_bytes()
    for damage in ("edited", "truncated", "rollback"):
        entry.write_bytes(original_entry)
        head.write_bytes(original_head)
        if damage == "edited":
            entry.write_bytes(
                encode_entry(replace(decode_entry(original_entry), reason="edited"))
            )
        else:
            entry.unlink()
            if damage == "rollback":
                head.write_bytes(encode_head(JournalHead(0, None)))
        with pytest.raises(InputCaptureError) as failure:
            _build(correction_root, release_execution)
        retained = read_attempt(correction_root, failure.value.attempt)
        assert retained["model"] is None, damage
        assert retained["failure"] is not None, damage
        assert (correction_root / "current.json").read_bytes() == current, damage


def test_uncommitted_suffix_is_explicit_and_never_applied(
    correction_root: Path,
    release_execution: ObservedExecution,
) -> None:
    first = _build(correction_root, release_execution)
    posting = _posting(report(correction_root, first["attempt_digest"]))
    recorded = correct(
        correction_root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason="review",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    header = correction_root / "journal/entries/0000000000000000001.json"
    committed = decode_entry(header.read_bytes())
    orphan = replace(
        committed,
        sequence=2,
        previous_entry_digest=str(recorded["entry_digest"]),
        key=SemanticKey("uncommitted"),
        reason="uncertain append",
    )
    (correction_root / "journal/entries/0000000000000000002.json").write_bytes(
        encode_entry(orphan)
    )
    published = _build(correction_root, release_execution)
    retained = read_attempt(correction_root, published["attempt_digest"])
    assert retained["selection"]["uncommitted_sequences"] == [2]
    assert len(retained["model"]["review"]["receipts"]) == 1
    assert _posting(retained)["category_id"] == "expense"


def test_attempt_witnesses_are_distinct_and_reports_never_replaced(
    tmp_path: Path,
) -> None:
    payload = encode_json({"journal": None}, integer_strings=False)
    with writer_lock(tmp_path) as lock:
        selected = put_retained(tmp_path, payload, lock=lock)
        first = retain_attempt(tmp_path, selected, lock=lock)
        second = retain_attempt(tmp_path, selected, lock=lock)
        assert first != second
        assert selections(tmp_path, lock=lock) == (payload, payload)
        seal = retain_report(tmp_path, first, b"first report", lock=lock)
        assert retain_report(tmp_path, first, b"first report", lock=lock) == seal
        with pytest.raises(StorageError):
            retain_report(tmp_path, first, b"changed report", lock=lock)
        retain_report(tmp_path, second, b"second report", lock=lock)
    assert read_report(tmp_path, first) == b"first report"
    assert read_report(tmp_path, second) == b"second report"


@pytest.mark.parametrize("damage", ["marker", "index", "witness", "orphan"])
def test_attempt_inventory_damage_fails_closed(tmp_path: Path, damage: str) -> None:
    with writer_lock(tmp_path) as lock:
        selected = put_retained(
            tmp_path, encode_json({"journal": None}, integer_strings=False), lock=lock
        )
        attempt = retain_attempt(tmp_path, selected, lock=lock)
    if damage == "marker":
        (tmp_path / "attempts.version").unlink()
    elif damage == "index":
        (tmp_path / "attempts/index.json").unlink()
    elif damage == "witness":
        (tmp_path / "attempts" / f"{attempt}.selection").write_bytes(b"edited witness")
    else:
        (tmp_path / "attempts" / f"{'f' * 64}.selection").write_bytes(
            b"unlisted witness"
        )
    with writer_lock(tmp_path) as lock, pytest.raises((ContractError, StorageError)):
        selections(tmp_path, lock=lock)
