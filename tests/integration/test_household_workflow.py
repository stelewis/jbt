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
from jbt.domain.decisions import IdentityMode
from jbt.runtime.decisions import IdentityRequest, TransferRequest, correct, decide
from jbt.runtime.worker import build, verify
from jbt.storage.local import acquire, writer_lock

if TYPE_CHECKING:
    from jbt.artifacts.reader import PinnedSnapshot
    from jbt.runtime.execution import ObservedExecution
    from jbt.runtime.worker import BuildReport

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("installed_ofx_release"),
]


def _project(example: Path) -> dict:
    project = json.loads((example / "project.json").read_bytes())
    declarations = project["declarations"]
    by_key = {item["key"]: item for item in declarations}
    card = by_key["card-bank"]["payload"]
    card.update(
        statement_cadence="monthly_cycle",
        period_rule="monthly_cycle",
        expected_periods=[],
        cycle_end_day=15,
        closing_tolerance_days=0,
    )
    internal = deepcopy(by_key["bank"])
    internal["key"] = "internal"
    internal["payload"].update(
        account_id="internal",
        bindings=[],
        statements_expected=False,
        period_rule="none",
        expected_periods=[],
    )
    transit = deepcopy(by_key["cash"])
    transit["key"] = "transit"
    transit["payload"].update(
        position_id="transit", account_id="internal", purpose="in_transit"
    )
    eur = deepcopy(by_key["usd"])
    eur["key"] = "eur"
    eur["payload"].update(commodity_id="EUR", name="EUR")
    eur["payload"]["symbols"][0].update(symbol_id="eur-symbol", symbol="EUR")
    declarations.extend((internal, transit, eur))
    for payee in ("Card purchase", "Card refund", "Card interest", "Foreign purchase"):
        rule = deepcopy(by_key["rule-expense"])
        rule["key"] = f"rule-{payee.lower().replace(' ', '-')}"
        rule["payload"]["order"] = len(declarations)
        rule["payload"]["matches"][0]["value"] = payee
        declarations.append(rule)
    rendering = deepcopy(project["configuration"]["rendering"][0])
    rendering.update(
        target={"table": "positions", "key": ["transit"]},
        positive_name="Assets:Transfers:Transit",
    )
    project["configuration"]["rendering"].append(rendering)
    project["configuration"]["as_of"] = "2026-02-16"
    for field in ("currate", "cursym"):
        project["registries"]["observation_fields"].append(
            {
                "field_name": f"source.origcurrency.{field}",
                "value_type": "text",
                "units": "none",
                "payload_schema": None,
            }
        )
    return project


def _summary(pinned: PinnedSnapshot) -> dict:
    return json.loads(
        next(
            payload
            for path, payload in pinned.outputs.items()
            if path.endswith("summary.json")
        )
    )


def test_household_card_review_correction_and_checked_replay(  # noqa: PLR0915 - one household publication/replay history
    tmp_path: Path, release_execution: ObservedExecution
) -> None:
    example = Path(__file__).parents[2] / "examples/checking-card"
    fixtures = Path(__file__).parent / "fixtures_synthetic"
    project = _project(example)
    ledger_output = next(
        output
        for output in project["configuration"]["outputs"]
        if output["sink"] == "beancount"
    )
    project["configuration"]["outputs"].remove(ledger_output)
    root = tmp_path / "household"
    root.mkdir()
    with writer_lock(root) as lock:
        receipts = [
            acquire(
                root,
                source,
                retry_key=source.name,
                acquired_at="2026-02-17T00:00:00Z",
                source_scope_id="bank-feed",
                importer_id="ofx",
                lock=lock,
            )
            for source in (
                example / "prior.ofx",
                fixtures / "household-first.ofx",
                fixtures / "household-second.ofx",
            )
        ]
    project["sources"] = [receipt.accession_id for receipt in receipts]
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
    purchases = [
        row for row in transactions.values() if row["payee"] == "Card purchase"
    ]
    assert len(purchases) == 2
    assert len({row["txn_id"] for row in purchases}) == 2
    assert sorted(
        (transactions[row["txn_id"]]["payee"], row["amount_coefficient"])
        for row in tables["postings"]
        if row["position_id"] == "card-cash"
        and row["step_id"] is not None
        and transactions[row["txn_id"]]["payee"] is not None
    ) == [
        ("Card interest", "-1"),
        ("Card payment", "15"),
        ("Card purchase", "-8"),
        ("Card purchase", "-8"),
        ("Card refund", "2"),
        ("Foreign purchase", "-5"),
        ("Unreviewed purchase", "-9"),
    ]
    initial_summary = _summary(read_pinned(root, initial["descriptor_digest"]))
    assert initial_summary["review"]["pending_events"] == 1
    foreign = next(
        row
        for row in tables["postings"]
        if row["position_id"] == "card-cash"
        and transactions[row["txn_id"]]["payee"] == "Foreign purchase"
    )
    assert foreign["amount_coefficient"] == "-5"
    assert foreign["fx_rate_as_stated_coefficient"] == "125"
    assert foreign["fx_rate_as_stated_scale"] == 2
    assert foreign["original_commodity_id"] == "EUR"
    assert foreign["original_amount_coefficient"] is None
    pending = next(
        row for row in transactions.values() if row["event_state"] == "pending"
    )
    proposal = before["model"]["review"]["proposals"][0]
    decide(
        root,
        attempt=initial["attempt_digest"],
        decision_key="authorization-posted",
        reason="Reviewed pending authorization and its keyed posting",
        request=IdentityRequest(
            IdentityMode(proposal["mode"]),
            proposal["canonical"],
            tuple(proposal["members"]),
        ),
        supersede=None,
    )
    recognized, merged = run()
    merged_summary = _summary(read_pinned(root, recognized["descriptor_digest"]))
    assert merged_summary["review"]["pending_events"] == 0
    assert any(
        row["record_id"] == pending["txn_id"] and row["identity_state"] == "retired"
        for row in merged["model"]["tables"]["economic_identities"]
    )
    assert (
        len(
            [
                row
                for row in merged["model"]["tables"]["postings"]
                if row["position_id"] == "card-cash"
                and row["amount_coefficient"] == "-9"
                and row["step_id"] is not None
            ]
        )
        == 1
    )
    payments = [
        row
        for row in tables["postings"]
        if row["leg_kind"] == "position"
        and transactions[row["txn_id"]]["payee"] == "Card payment"
    ]
    outgoing = next(
        row["posting_id"] for row in payments if row["position_id"] == "cash"
    )
    incoming = next(
        row["posting_id"] for row in payments if row["position_id"] == "card-cash"
    )
    decide(
        root,
        attempt=recognized["attempt_digest"],
        decision_key="card-payment",
        reason="Reviewed checking departure and card arrival across month end",
        request=TransferRequest((outgoing,), (incoming,), "USD"),
        supersede=None,
    )
    linked, after = run()
    published = read_pinned(root, linked["descriptor_digest"])
    backlog = _summary(published)
    assert backlog["review"]["uncategorized_events"] == 1
    assert [
        (row["leg_count"], row["amount"]["coefficient"])
        for row in backlog["uncategorized_work"]
    ] == [(1, "9")]
    target = next(
        row["posting_id"]
        for row in after["model"]["tables"]["postings"]
        if row["leg_kind"] == "boundary" and row["category_id"] is None
    )
    assert any(row["posting_id"] == target for row in published.tables["postings"])
    correct(
        root,
        attempt=linked["attempt_digest"],
        identifier=target,
        decision_key="purchase-category",
        reason="Classify the published tabular purchase ID",
        field="postings.category_id",
        value="expense",
        children=(),
        supersede=None,
    )
    project["configuration"]["outputs"].append(ledger_output)
    (root / "project.json").write_bytes(encode_json(project, integer_strings=False))
    final, corrected = run()
    pinned = read_pinned(root, final["descriptor_digest"])
    assert any(path.endswith(".beancount") for path in pinned.outputs)
    summary = _summary(pinned)
    assert summary["uncategorized_work"] == []
    assert summary["review"]["uncategorized_events"] == 0
    assert [
        (row["month"], row["closing"]["coefficient"])
        for row in summary["transit_balances"]
    ] == [("2026-01", "15"), ("2026-02", "0")]
    assert {
        row["position_id"]: row["after"]["coefficient"] for row in summary["changes"]
    } == {"cash": "85", "card-cash": "-14", "transit": "0"}
    assert all(
        receipt["outcome"] == "applied"
        for receipt in corrected["model"]["review"]["receipts"]
    )
    for kind in (
        "source_reconciliation",
        "model_reconciliation",
        "occurrence_accounting",
        "transfer_conservation",
    ):
        checks = [check for check in corrected["checks"] if check["check_kind"] == kind]
        assert checks
        assert all(check["status"] == "pass" for check in checks)
    coverage = [
        check
        for check in corrected["checks"]
        if check["check_kind"] == "coverage"
        and check["check_id"].startswith("coverage:card:")
    ]
    assert len(coverage) == 2
    assert all(check["status"] == "fail" for check in coverage)
    assert [json.loads(check["expected"]["value"])["period"] for check in coverage] == [
        {"start": "2026-01-01", "end": "2026-01-16"},
        {"start": "2026-01-16", "end": "2026-02-16"},
    ]
    assert all(not row["is_complete"] for row in tables["statement_coverage"])
    independent = read_snapshot(
        root / "generations" / final["descriptor_digest"],
        descriptor_digest=final["descriptor_digest"],
        expected_schema_digests=approved_schemas(),
    )
    replayed = replay_rows(independent.tables, entity="invented-owner")
    assert {
        position: amount
        for (_, position), amount in replayed.quantity_positions.items()
    } == {"cash": 85, "card-cash": -14, "transit": 0}
    replay = verify(
        root, baseline_digest=final["descriptor_digest"], execution=release_execution
    )
    assert replay["equal"]
    assert replay["reused"] == 0
