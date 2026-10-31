from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from tests.integration.conformance.attempt_reader import read_attempt
from tests.integration.conformance.corpus import approved_schemas
from tests.integration.conformance.reader import read_snapshot

from jbt.artifacts.integrity import byte_digest
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.importers.ofx import extract, to_extraction
from jbt.runtime import build as build_module
from jbt.runtime.decisions import RevisionRequest, correct, decide
from jbt.runtime.worker import BuildAttemptError, build, verify
from jbt.storage.local import acquire, writer_lock

if TYPE_CHECKING:
    from jbt.extraction import ExtractionContext
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.worker import BuildReport

pytestmark = pytest.mark.integration
_COMPLETE = b"\n<!-- synthetic complete statement -->"


class SyntheticStatementImporter:
    """The fixture states completeness; the OFX profile does not."""

    contract_version = 1
    profile = "synthetic-statement"
    profile_version = 1
    maximum_bytes = 1_048_576

    def extract(self, data: bytes, context: ExtractionContext) -> dict:
        document = to_extraction(extract(data.removesuffix(_COMPLETE)), context)
        if data.endswith(_COMPLETE):
            for section in document["source_scope"]["sections"]:
                section["source_class"] = "statement"
                for part in section["evidence_parts"]:
                    part["completeness"] = "complete"
        return document


def test_authoritative_reissue_requires_review_and_publishes_atomically(  # noqa: PLR0915 - one durable acceptance history
    tmp_path: Path,
    project_document: dict,
    release_execution: ObservedExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = SimpleNamespace(
        metadata=SimpleNamespace(
            get_all=lambda key: ["jbt"] if key == "Name" else ["0.1.0a1"]
        ),
        read_text=lambda _name: None,
    )
    entry = SimpleNamespace(
        name="ofx", dist=distribution, load=SyntheticStatementImporter
    )
    monkeypatch.setattr(metadata, "entry_points", lambda **_kwargs: [entry])
    fixture = Path(__file__).parents[1] / "jbt/importers/fixtures_synthetic"
    main = (fixture / "main.ofx").read_bytes()
    sources = {
        "prior": (fixture / "prior.ofx").read_bytes(),
        "rolling": main,
        "statement": main + _COMPLETE,
        "revision": main.replace(b"-5.00", b"-6.00").replace(b"115.00", b"114.00")
        + _COMPLETE,
    }
    root = tmp_path / "record"
    root.mkdir()
    accessions = {}
    with writer_lock(root) as lock:
        for key, data in sources.items():
            path = tmp_path / f"{key}.ofx"
            path.write_bytes(data)
            receipt = acquire(
                root,
                path,
                retry_key=key,
                acquired_at="2026-02-02T00:00:00Z",
                source_scope_id="bank-feed",
                importer_id="ofx",
                lock=lock,
            )
            accessions[key] = receipt.accession_id
    project = deepcopy(project_document)
    project["sources"] = [accessions[key] for key in ("prior", "rolling", "statement")]
    project["configuration"]["as_of"] = "2026-02-02"
    for output in project["configuration"]["outputs"]:
        output["required_checks"].append("coverage")
    project["configuration"]["source_authority"] = [
        {
            "account_id": "bank",
            "source_scope_id": "bank-feed",
            "fact_kind": kind,
            "coverage_basis_id": basis,
            "period_start": "2026-01-01",
            "period_end": "2026-02-02",
            "mode": "complete_replacement",
        }
        for kind, basis in (("movement", "posted"), ("balance", "posted"))
    ]

    def run() -> BuildReport:
        (root / "project.json").write_bytes(encode_json(project, integer_strings=False))
        return build(
            root,
            project="project.json",
            execution=release_execution,
            comparison_baseline=None,
        )

    first = run()
    before = read_attempt(root, first["attempt_digest"])
    posting = next(
        row
        for row in before["model"]["tables"]["postings"]
        if row["leg_kind"] == "boundary" and row["amount_coefficient"] == "5"
    )
    assert posting["category_id"] == "expense"
    original = correct(
        root,
        attempt=first["attempt_digest"],
        identifier=posting["posting_id"],
        decision_key="category",
        reason="Reviewed the signed debit of five",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    baseline = run()
    current = (root / "current.json").read_bytes()
    project["sources"].append(accessions["revision"])
    with pytest.raises(BuildAttemptError) as disagreement:
        run()
    failed = read_attempt(root, disagreement.value.attempt)
    receipt = failed["model"]["review"]["receipts"][0]
    assert receipt["outcome"] == "ambiguous"
    assert len(receipt["candidates"]) == 3
    assert (root / "current.json").read_bytes() == current
    statements = {
        json.loads(
            next(
                field["value"] for field in target["fields"] if field["name"] == "facts"
            )
        )["blob_digest"]: target["key"][0]
        for target in failed["model"]["review"]["tail"]
        if target["table"] == "review_statements"
    }
    old = statements[byte_digest(sources["statement"])]
    new = statements[byte_digest(sources["revision"])]
    attempt = disagreement.value.attempt
    for kind, basis, end in (
        ("movement", "posted", date(2026, 2, 1)),
        ("balance", "posted", date(2026, 2, 2)),
    ):
        decide(
            root,
            attempt=attempt,
            decision_key=f"revision-{kind}",
            reason="Reviewed the reissue within this exact financial basis",
            request=RevisionRequest(
                (old,),
                (new,),
                "bank",
                kind,
                basis,
                date(2026, 1, 1),
                end,
            ),
            supersede=None,
        )
        with pytest.raises(BuildAttemptError) as changed:
            run()
        attempt = changed.value.attempt
    reviewed = read_attempt(root, attempt)
    assert [row["outcome"] for row in reviewed["model"]["review"]["receipts"]] == [
        "applied",
        "applied",
        "changed",
    ]
    assert (root / "current.json").read_bytes() == current
    assert isinstance(original["entry_digest"], str)
    correct(
        root,
        attempt=attempt,
        identifier=posting["posting_id"],
        decision_key="category",
        reason="Reviewed the revised signed debit of six",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=original["entry_digest"],
    )

    def interrupted(*_args: object) -> bytes:
        code = "interrupted_sink"
        raise ContractError(code, "beancount")

    with monkeypatch.context() as patch:
        patch.setattr(build_module, "ledger", interrupted)
        with pytest.raises(BuildAttemptError, match="interrupted_sink"):
            run()
    assert (root / "current.json").read_bytes() == current
    completed = run()
    after = read_attempt(root, completed["attempt_digest"])
    tables = after["model"]["tables"]
    assert [
        (row["posting_id"], row["amount_coefficient"], row["category_id"])
        for row in tables["postings"]
        if row["leg_kind"] == "boundary" and row["amount_coefficient"] == "6"
    ] == [(posting["posting_id"], "6", "expense")]
    assert all(
        check["status"] == "pass"
        for check in after["checks"]
        if check["check_kind"]
        in {"source_reconciliation", "model_reconciliation", "coverage"}
    )
    snapshot = read_snapshot(
        root / "generations" / completed["descriptor_digest"],
        descriptor_digest=completed["descriptor_digest"],
        expected_schema_digests=approved_schemas(),
    )
    assert [
        (row["period_start"], row["period_end"])
        for row in snapshot.tables["statement_coverage"]
        if row["is_complete"] and not row["is_eclipsed"]
    ] == [("2026-01-01", "2026-02-01")]
    assert baseline["descriptor_digest"] != completed["descriptor_digest"]
    assert verify(
        root,
        baseline_digest=completed["descriptor_digest"],
        execution=release_execution,
    )["equal"]
