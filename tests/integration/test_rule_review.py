import json
from typing import TYPE_CHECKING

import pytest
from tests.jbt.model.test_quantity import history, source

from jbt.artifacts.attempt import read_target
from jbt.contracts.schemas import model_artifact_schema, validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.decisions import LegReviewRef, ReviewBasisRef, SplitBody, SplitChild
from jbt.domain.ids import RecordId, RecordKind, SemanticKey
from jbt.domain.journal import (
    DecisionKind,
    JournalEntry,
    JournalHead,
    JournalOperation,
    JournalSelection,
    entry_digest,
    select_journal,
)
from jbt.domain.numbers import ExactDecimal
from jbt.model.corrections import Decision
from jbt.runtime import decisions
from jbt.runtime.build import _model_payload
from jbt.runtime.project import document, objects, read_project
from jbt.runtime.resolution import ExtractInput

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from pathlib import Path


def test_typed_bank_rules_project_one_fold_into_report_provenance_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = source(
        bank=(("debit", "20260120", -5),),
        card=(("purchase", "20260120", -8),),
        bank_balance=95,
        card_balance=-8,
    ).replace(b"</NAME>", b"</NAME><MEMO>5</MEMO>")
    value, extracts = history((raw,))
    retained = []
    for encoded in extracts:
        extract = json.loads(encoded)
        for record in extract["records"]:
            if record["kind"] != "observation":
                continue
            payload = record["payload"]
            if payload["field_name"] == "source.memo":
                payload.update(
                    field_name="source.synthetic.amount",
                    value_type="decimal",
                    decimal_value={
                        "coefficient": "5",
                        "scale": 0,
                        "source_scale": 2,
                    },
                    text_value=None,
                )
            elif payload["field_name"] == "source.dtposted":
                payload.update(
                    field_name="source.synthetic.date",
                    value_type="date",
                    date_value="2026-01-20",
                    text_value=None,
                )
        retained.append(encode_json(extract, integer_strings=False))
    value["registries"]["observation_fields"].extend(
        [
            {
                "field_name": "source.synthetic.amount",
                "value_type": "decimal",
                "units": "commodity",
                "payload_schema": None,
            },
            {
                "field_name": "source.synthetic.date",
                "value_type": "date",
                "units": "none",
                "payload_schema": None,
            },
        ]
    )
    value["declarations"] = [
        item for item in value["declarations"] if item["payload"]["kind"] != "rule"
    ]
    predicates = [
        {"field": "transactions.payee", "operator": "contains", "value": "expense"},
        {
            "field": "transactions.description_as_stated",
            "operator": "equals",
            "value": "Invented expense",
        },
        {
            "field": "transactions.event_state",
            "operator": "equals",
            "value": "recognized",
        },
        {
            "field": "transactions.date_posted",
            "operator": "in",
            "value": ["2026-01-20"],
        },
        {"field": "postings.date_posted", "operator": "equals", "value": "2026-01-20"},
        {"field": "postings.account_id", "operator": "equals", "value": "bank"},
        {"field": "postings.commodity_id", "operator": "in", "value": ["USD"]},
        {
            "field": "postings.amount",
            "operator": "equals",
            "value": {"coefficient": "5", "scale": 0, "source_scale": 2},
        },
        {"field": "source.status", "operator": "equals", "value": "STMTTRN"},
        {"field": "source.trntype", "operator": "in", "value": ["DEBIT"]},
        {
            "field": "source.synthetic.amount",
            "operator": "in",
            "value": [{"coefficient": "5", "scale": 0, "source_scale": None}],
        },
        {"field": "source.synthetic.date", "operator": "equals", "value": "2026-01-20"},
    ]
    for key, order, category, payee, tags in (
        ("first", 0, "expense", "Enriched", ["a"]),
        ("second", 1, "income", "Loses", ["a", "b"]),
    ):
        value["declarations"].append(
            {
                "key": key,
                "valid_from": None,
                "valid_to": None,
                "payload": {
                    "schema_version": 1,
                    "kind": "rule",
                    "order": order,
                    "matches": predicates,
                    "assignments": [
                        {"field": "postings.category_id", "value": category},
                        {"field": "transactions.payee", "value": payee},
                        {"field": "transactions.tags", "value": tags},
                    ],
                    "split": [],
                },
            }
        )
    project = read_project(encode_json(value, integer_strings=False))
    inputs = tuple(
        ExtractInput(selection, encoded)
        for selection, encoded in zip(project.sources, retained, strict=True)
    )
    model = json.loads(
        _model_payload(
            project, inputs, (), JournalSelection(JournalHead(0, None), (), (), ()), ()
        )
    )
    validate_document(model, model_artifact_schema())
    matched = [
        evaluation for evaluation in model["review"]["rules"] if evaluation["matched"]
    ]
    assert [evaluation["rule_key"] for evaluation in matched] == ["first", "second"]
    txn_id = matched[0]["txn_id"]
    transaction = next(
        row for row in model["tables"]["transactions"] if row["txn_id"] == txn_id
    )
    assert (transaction["payee"], transaction["tags"]) == ("Enriched", ["a", "b"])
    assert [result["outcome"] for result in matched[1]["assignments"]] == [
        "shadowed",
        "shadowed",
        "applied",
    ]
    revisions = {item.key: item.revision for item in project.declarations}
    tags = [
        row
        for row in model["tables"]["provenance"]
        if row["record_kind"] == "transactions"
        and row["record_id"] == json.dumps([txn_id], separators=(",", ":"))
        and row["field_name"] == "tags"
    ]
    assert {row["evidence_id"] for row in tags} == {
        revisions["first"],
        revisions["second"],
    }
    assert all(row["value_origin"] == "rule" for row in tags)
    monkeypatch.setattr(
        decisions,
        "report",
        lambda *_args: {
            "model": model,
            "selection": {"journal_headers": []},
        },
    )
    shown = decisions.show(tmp_path, attempt="a" * 64, identifier=txn_id)
    assert document(shown["review"])["rules"] == matched
    assert {"source.status", "source.synthetic.amount", "source.synthetic.date"} <= {
        row["field_name"] for row in objects(shown["observations"], "observations")
    }
    assert {
        row["declaration_key"] for row in objects(shown["declarations"], "declarations")
    } >= {
        "first",
        "second",
    }
    parent = matched[0]["posting_id"]
    target = read_target(
        next(
            target
            for target in model["review"]["tail"]
            if target["table"] == "review_legs" and target["key"] == [parent]
        )
    )
    entry = JournalEntry(
        1,
        None,
        project.entity,
        SemanticKey("allocate"),
        DecisionKind.SPLIT,
        JournalOperation.RECORD,
        None,
        "a" * 64,
        "Synthetic allocation",
        "b" * 64,
    )
    body = SplitBody(
        project.entity,
        ReviewBasisRef("a" * 64, "c" * 64),
        LegReviewRef(RecordId(project.entity, RecordKind.LEG, parent)),
        (
            SplitChild(SemanticKey("first"), ExactDecimal.parse("2")),
            SplitChild(SemanticKey("second"), ExactDecimal.parse("3")),
        ),
    )
    model = json.loads(
        _model_payload(
            project,
            inputs,
            (Decision(entry, body, target),),
            select_journal(
                (entry,),
                head=JournalHead(1, entry_digest(entry)),
                retained_heads=(),
            ),
            (),
        )
    )
    validate_document(model, model_artifact_schema())
    children = [
        row
        for row in model["tables"]["postings"]
        if row["txn_id"] == txn_id and row["leg_kind"] == "boundary"
    ]
    assert {row["amount_coefficient"] for row in children} == {"2", "3"}
    child_ids = sorted(row["posting_id"] for row in children)
    for child in children:
        assert child["category_id"] == "expense"
        shown = decisions.show(
            tmp_path, attempt="a" * 64, identifier=child["posting_id"]
        )
        evaluations = objects(document(shown["review"])["rules"], "rules")
        assert [evaluation["rule_key"] for evaluation in evaluations] == [
            "first",
            "second",
        ]
        assert all(
            evaluation["posting_id"] == parent
            and evaluation["target_posting_ids"] == child_ids
            and evaluation["predicates"] == expected["predicates"]
            for evaluation, expected in zip(evaluations, matched, strict=True)
        )
