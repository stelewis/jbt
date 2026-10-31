import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.integration.conformance.attempt_reader import read_attempt
from tests.integration.conformance.corpus import approved_schemas
from tests.integration.conformance.reader import read_snapshot, replay_rows

from jbt.artifacts.reader import read_pinned
from jbt.domain.canonical import encode_json
from jbt.runtime.decisions import (
    DocumentLinkRequest,
    LifecycleLinkRequest,
    TransferRequest,
    correct,
    decide,
)
from jbt.runtime.worker import build, verify
from jbt.storage.local import acquire, writer_lock

if TYPE_CHECKING:
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.worker import BuildReport

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("installed_ofx_release"),
]


def _transit(project: dict) -> None:
    account = deepcopy(project["declarations"][0])
    account["key"] = "internal"
    account["payload"].update(
        account_id="internal",
        bindings=[],
        statements_expected=False,
        period_rule="none",
        expected_periods=[],
    )
    position = deepcopy(
        next(item for item in project["declarations"] if item["key"] == "cash")
    )
    position["key"] = "transit"
    position["payload"].update(
        position_id="transit", account_id="internal", purpose="in_transit"
    )
    project["declarations"].extend((account, position))
    rendering = deepcopy(project["configuration"]["rendering"][0])
    rendering.update(
        target={"table": "positions", "key": ["transit"]},
        positive_name="Assets:Transfers:Transit",
    )
    project["configuration"]["rendering"].append(rendering)
    failed = deepcopy(
        next(item for item in project["declarations"] if item["key"] == "initial-cash")
    )
    failed["key"] = "failed-attempt"
    payload = failed["payload"]
    payload.update(event_key="failed", evidence=[])
    record = payload["record"]
    record.update(
        event_kind="transaction",
        event_state="cancelled",
        date_authorized="2026-01-30",
        date_posted=None,
        narration="Invented failed attempt",
    )
    record["legs"][0]["amount"]["coefficient"] = "-100"
    record["legs"][1]["amount"]["coefficient"] = "100"
    record["legs"][1]["category_id"] = None
    project["declarations"].append(failed)


def test_card_transfer_supersession_documents_and_fee_links_replay(  # noqa: PLR0915 - one retained transfer/link history
    tmp_path: Path, release_execution: ObservedExecution
) -> None:
    """Prove retained guards, transit booking, sinks, and checked replay together."""
    example = Path(__file__).parents[2] / "examples/checking-card"
    project = json.loads((example / "project.json").read_bytes())
    _transit(project)
    root = tmp_path / "record"
    root.mkdir()
    main = (example / "main.ofx").read_bytes()
    main = main.replace(
        b"</BANKTRANLIST><LEDGERBAL><BALAMT>108.00",
        b"<STMTTRN><TRNTYPE>XFER</TRNTYPE><DTPOSTED>20260131</DTPOSTED>"
        b"<TRNAMT>-15.00</TRNAMT><FITID>payment</FITID>"
        b"<NAME>Invented expense</NAME></STMTTRN>"
        b"</BANKTRANLIST><LEDGERBAL><BALAMT>93.00",
    )
    card_start = main.index(b"<CREDITCARDMSGSRSV1>")
    main = main[:card_start] + main[card_start:].replace(
        b"20260115", b"20260202"
    ).replace(b"20260201000000", b"20260301000000")
    (tmp_path / "main.ofx").write_bytes(main)
    (tmp_path / "receipt.txt").write_bytes(b"Invented purchase receipt: 8 USD\n")
    with writer_lock(root) as lock:
        sources = [
            acquire(
                root,
                source,
                retry_key=key,
                acquired_at="2026-03-02T00:00:00Z",
                source_scope_id="bank-feed",
                importer_id="ofx",
                lock=lock,
            )
            for key, source in (
                ("prior", example / "prior.ofx"),
                ("main", tmp_path / "main.ofx"),
            )
        ]
        receipt = acquire(
            root,
            tmp_path / "receipt.txt",
            retry_key="receipt",
            acquired_at="2026-03-02T00:00:00Z",
            source_scope_id="documents",
            importer_id=None,
            lock=lock,
        )
    project["sources"] = [source.accession_id for source in sources]
    project["documents"] = [receipt.accession_id]
    project["configuration"]["as_of"] = "2026-03-01"
    (root / "project.json").write_bytes(encode_json(project, integer_strings=False))

    def run() -> tuple[BuildReport, dict]:
        result = build(
            root,
            project="project.json",
            execution=release_execution,
            comparison_baseline=None,
        )
        return result, read_attempt(root, result["attempt_digest"])

    initial, before = run()
    tables = before["model"]["tables"]
    transactions = {row["txn_id"]: row for row in tables["transactions"]}
    outgoing = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "cash" and row["amount_coefficient"] == "-15"
    )
    incoming = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "card-cash" and row["amount_coefficient"] == "15"
    )
    boundary_id = next(
        row["posting_id"]
        for row in tables["postings"]
        if row["txn_id"] == outgoing["txn_id"] and row["leg_kind"] == "boundary"
    )
    decide(
        root,
        attempt=initial["attempt_digest"],
        decision_key="card-payment",
        reason="Reviewed synthetic departure; arrival not yet linked",
        request=TransferRequest((outgoing["posting_id"],), (), "USD"),
        supersede=None,
    )
    departed, in_transit = run()
    one_sided = in_transit["model"]["tables"]
    assert not [row for row in one_sided["links"] if row["link_kind"] == "transfer"]
    transit = next(
        row for row in one_sided["postings"] if row["position_id"] == "transit"
    )
    assert transit["posting_id"] == boundary_id
    assert transit["amount_coefficient"] == "15"
    assert transit["date_posted_mode"] == "inherit"
    decide(
        root,
        attempt=departed["attempt_digest"],
        decision_key="card-payment",
        reason="Reviewed arrival on the card after month end",
        request=TransferRequest(
            (outgoing["posting_id"],), (incoming["posting_id"],), "USD"
        ),
        supersede="card-payment",
    )
    completed, after = run()
    transfer = [
        row
        for row in after["model"]["tables"]["links"]
        if row["link_kind"] == "transfer"
    ]
    assert {(row["member_role"], row["member_id"]) for row in transfer} == {
        ("outgoing", outgoing["posting_id"]),
        ("incoming", incoming["posting_id"]),
    }
    assert any(
        check["check_kind"] == "transfer_conservation" and check["status"] == "pass"
        for check in after["checks"]
    )
    purchase = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "card-cash" and row["amount_coefficient"] == "-8"
    )
    decide(
        root,
        attempt=completed["attempt_digest"],
        decision_key="purchase-receipt",
        reason="Reviewed synthetic receipt",
        request=DocumentLinkRequest(purchase["txn_id"], (receipt.object_digest,)),
        supersede=None,
    )
    documented, _ = run()
    failed = next(
        row for row in transactions.values() if row["event_state"] == "cancelled"
    )
    fee = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "cash" and row["amount_coefficient"] == "-7"
    )
    decide(
        root,
        attempt=documented["attempt_digest"],
        decision_key="failed-attempt-fee",
        reason="Human-reviewed failure with a separately charged fee",
        request=LifecycleLinkRequest(failed["txn_id"], fee["txn_id"]),
        supersede=None,
    )
    linked_result, linked = run()
    assert {row["link_kind"] for row in linked["model"]["tables"]["links"]} == {
        "transfer",
        "document",
        "lifecycle",
    }
    correct(
        root,
        attempt=linked_result["attempt_digest"],
        identifier=outgoing["txn_id"],
        decision_key="payment-narration",
        reason="Reviewed description after the transfer was linked",
        field="transactions.narration",
        value="Synthetic card payment",
        children=(),
        supersede=None,
    )
    final, corrected = run()
    assert all(
        receipt["outcome"] == "applied"
        for receipt in corrected["model"]["review"]["receipts"]
    )
    pinned = read_pinned(root, final["descriptor_digest"])
    assert pinned.descriptor_digest == final["descriptor_digest"]
    ledger = next(
        payload.decode()
        for path, payload in pinned.outputs.items()
        if path.endswith(".beancount")
    )
    assert "Assets:Transfers:Transit" in ledger
    assert receipt.object_digest in ledger
    assert "receipt.txt" not in ledger
    report = json.loads(
        next(
            payload
            for path, payload in pinned.outputs.items()
            if path.endswith("summary.json")
        )
    )
    assert [
        (row["month"], row["closing"]["coefficient"])
        for row in report["transit_balances"]
    ] == [("2026-01", "15"), ("2026-02", "0"), ("2026-03", "0")]
    positions = {
        row["position_id"]: row["after"]["coefficient"] for row in report["changes"]
    }
    assert positions["cash"] == "93"
    assert positions["card-cash"] == "6"
    manifest = pinned.manifest
    assert receipt.object_digest in {
        item["object_id"] for item in manifest["recovery_set"]["vault_objects"]
    }
    independent = read_snapshot(
        root / "generations" / final["descriptor_digest"],
        descriptor_digest=final["descriptor_digest"],
        expected_schema_digests=approved_schemas(),
    )
    replayed = replay_rows(independent.tables, entity="invented-owner")
    assert {
        position: amount
        for (_, position), amount in replayed.quantity_positions.items()
    } == {"cash": 93, "card-cash": 6, "transit": 0}
    replay = verify(
        root, baseline_digest=final["descriptor_digest"], execution=release_execution
    )
    assert replay["equal"]
