from copy import deepcopy

import pytest

from jbt.artifacts import attempt
from jbt.artifacts.attempt import (
    decode_review_report,
    encode_review_report,
    read_target,
    review_boundary,
    write_target,
)
from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.domain.guards import GuardField, GuardReference, GuardTarget
from jbt.domain.ids import EntityId, SemanticKey


def test_safe_failure_report_has_distinct_attempt_and_input_lineage() -> None:
    value = {
        "schema_version": 1,
        "attempt_digest": "a" * 64,
        "selection": {
            "input_digest": "b" * 64,
            "journal_head": None,
            "journal_headers": [],
            "journal_bases": [],
            "uncommitted_sequences": [],
        },
        "model": None,
        "checks": [],
        "failure": {"code": "malformed", "location": "project"},
    }
    payload = encode_review_report(value)
    assert decode_review_report(payload) == value
    with pytest.raises(ContractError, match="review_report_canonical"):
        decode_review_report(payload + b"\n")
    invalid = deepcopy(value)
    invalid["schema_version"] = True
    with pytest.raises(ContractError, match="review_report_schema"):
        encode_review_report(invalid)


def test_report_validation_reuses_only_identical_bytes_without_sharing_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "schema_version": 1,
        "attempt_digest": "c" * 64,
        "selection": {
            "input_digest": "d" * 64,
            "journal_head": None,
            "journal_headers": [],
            "journal_bases": [],
            "uncommitted_sequences": [],
        },
        "model": None,
        "checks": [],
        "failure": {"code": "malformed", "location": "project"},
    }
    payload = encode_review_report(report)
    validations = 0
    original = attempt.validate_document

    def counted(document: object, schema: dict) -> None:
        nonlocal validations
        validations += 1
        original(document, schema)

    monkeypatch.setattr(attempt, "validate_document", counted)
    decoded = decode_review_report(payload)
    selection = decoded["selection"]
    assert isinstance(selection, dict)
    selection["journal_headers"].append("e" * 64)
    for _ in range(20):
        assert decode_review_report(payload) == report
    assert validations == 1
    with pytest.raises(ContractError, match="review_report_schema"):
        decode_review_report(
            payload.replace(b'"schema_version":1', b'"schema_version":2')
        )
    with pytest.raises(ContractError, match="review_report_canonical"):
        decode_review_report(payload + b"\n")


def test_native_target_wire_keeps_typed_values_and_exact_text() -> None:
    target = GuardTarget(
        GuardReference(
            EntityId("invented-owner"), SemanticKey("review_events"), ("a" * 64,)
        ),
        (
            GuardField("accounts", "list", nullable=False, value=("bank",)),
            GuardField(
                "source_identity", "string", nullable=False, value=" e\u0301 \n"
            ),
        ),
        active=True,
    )
    assert read_target(write_target(target)) == target
    payload = encode_json(write_target(target), integer_strings=False)
    assert " e\u0301 " in payload.decode()
    invalid = write_target(target)
    invalid["active"] = 1
    with pytest.raises(ContractError, match="basis_target_schema"):
        read_target(invalid)


@pytest.mark.parametrize(
    ("kind", "value"), [("date", "2026-02-31"), ("integer", "bad")]
)
def test_invalid_basis_field_values_are_named_contract_errors(
    kind: str, value: str
) -> None:
    target = {
        "entity_id": "invented-owner",
        "table": "review_events",
        "key": ["a" * 64],
        "active": True,
        "fields": [{"name": "bad", "kind": kind, "nullable": False, "value": value}],
    }
    with pytest.raises(ContractError, match="basis_field_value"):
        read_target(target)


def test_reverse_boundary_changes_restore_other_targets_and_reject_bad_evidence() -> (
    None
):
    def target(key: str, value: str) -> dict:
        return write_target(
            GuardTarget(
                GuardReference(
                    EntityId("invented-owner"),
                    SemanticKey("review_events"),
                    (key * 64,),
                ),
                (GuardField("dates", "string", nullable=False, value=value),),
                active=True,
            )
        )

    unchanged = target("a", "original")
    before = target("b", "before")
    after = target("b", "after")
    change = {
        "entity_id": before["entity_id"],
        "table": before["table"],
        "key": before["key"],
        "candidates": [before],
    }
    review: dict = {
        "tail": [unchanged, after],
        "receipts": [
            {
                "decision_key": "earlier",
                "expected": unchanged,
                "candidates": [unchanged],
                "restore": [],
            },
            {
                "decision_key": "later",
                "expected": before,
                "candidates": [before],
                "restore": [change],
            },
        ],
    }
    assert review_boundary(review, "earlier") == (
        read_target(unchanged),
        read_target(before),
    )
    assert review_boundary(review, None) == (
        read_target(unchanged),
        read_target(after),
    )
    for candidates in ([after], [unchanged]):
        bad = deepcopy(review)
        bad["receipts"][1]["restore"][0]["candidates"] = candidates
        with pytest.raises(ContractError, match="review_boundary_change"):
            review_boundary(bad, "earlier")
    bad = deepcopy(review)
    bad["receipts"][1]["restore"] = []
    with pytest.raises(ContractError, match="review_boundary_receipt"):
        review_boundary(bad, "earlier")
