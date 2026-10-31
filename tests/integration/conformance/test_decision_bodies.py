import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from tests.integration.conformance.decision_bodies import (
    canonical_wire,
    parse_body,
    read_decision_body,
    reference_address,
)
from tests.integration.conformance.reader import ReaderError

if TYPE_CHECKING:
    from collections.abc import Callable

from jbt.artifacts.decisions import decode_decision_body, encode_decision_body


def sample(kind: str = "assign") -> dict:
    common = {
        "schema_version": 1,
        "kind": kind,
        "entity_id": "synthetic",
        "review": {"attempt_digest": "a" * 64, "basis_digest": "b" * 64},
    }
    if kind == "assign":
        return {
            **common,
            "target": {"entity_id": "synthetic", "event_id": "c" * 64},
            "assignments": [
                {"kind": "event_text", "field": "narration", "value": None},
                {"kind": "event_text", "field": "payee", "value": " e\u0301\n\x00 "},
                {"kind": "event_date", "role": "posted", "value": "2026-01-02"},
                {"kind": "event_review", "state": "ignored"},
                {"kind": "event_tags", "tags": ["", "é"]},
                {"kind": "event_date", "role": "value", "value": None},
            ],
        }
    return {
        **common,
        "parent": {"entity_id": "synthetic", "leg_id": "d" * 64},
        "children": [
            {
                "child_key": "a",
                "amount": {"coefficient": "1", "scale": 0, "source_scale": 4},
            },
            {
                "child_key": "b",
                "amount": {"coefficient": "-25", "scale": 2, "source_scale": None},
            },
        ],
    }


@pytest.mark.parametrize("kind", ["assign", "split"])
def test_independent_native_reader_agrees_with_the_live_codec(kind: str) -> None:
    wire = canonical_wire(sample(kind))
    consumer = read_decision_body(wire)
    assert consumer.kind == kind
    assert consumer.entity_id == "synthetic"
    assert consumer.wire == wire
    assert consumer.digest == hashlib.sha256(wire).hexdigest()
    assert encode_decision_body(decode_decision_body(wire)) == consumer.wire


@pytest.mark.parametrize(
    ("mode", "value"),
    [("inherit", None), ("unknown", None), ("value", "2026-01-02")],
)
def test_independent_leg_date_modes_and_category_absence(
    mode: str, value: str | None
) -> None:
    document = sample()
    document["target"] = {"entity_id": "synthetic", "leg_id": "d" * 64}
    document["assignments"] = [
        {"kind": "leg_category", "category_id": None},
        {"kind": "leg_date", "role": "posted", "date": {"mode": mode, "value": value}},
    ]
    wire = canonical_wire(document)
    assert read_decision_body(wire).wire == encode_decision_body(
        decode_decision_body(wire)
    )
    document["assignments"][1]["date"]["value"] = (
        None if mode == "value" else "2026-01-02"
    )
    with pytest.raises(ReaderError):
        parse_body(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.update(kind="identity"),
        lambda doc: doc.update(kind="unknown"),
        lambda doc: doc.update(guards=[]),
        lambda doc: doc.update(assignments=[{"kind": "cancel_event"}]),
        lambda doc: doc["target"].update(entity_id="other"),
        lambda doc: doc["target"].update(leg_id="d" * 64),
        lambda doc: doc["review"].pop("basis_digest"),
        lambda doc: doc["assignments"].reverse(),
        lambda doc: doc["assignments"].append(deepcopy(doc["assignments"][0])),
        lambda doc: doc["assignments"][2].update(value="2026-02-30"),
        lambda doc: doc["assignments"][4].update(tags=["é", "a"]),
        lambda doc: doc.update(schema_version=True),
    ],
)
def test_independent_reader_rejects_closed_contract_violations(
    change: Callable[[dict], object],
) -> None:
    document = sample()
    change(document)
    wire = canonical_wire(document)
    with pytest.raises(ReaderError):
        read_decision_body(wire)


def test_independent_reader_requires_canonical_bytes_and_unique_json_fields() -> None:
    wire = canonical_wire(sample())
    for payload in (
        wire[:-1],
        b" " + wire,
        wire.replace(b'"schema_version":1', b'"schema_version":1.0'),
        wire.replace(b'"kind":"assign"', b'"kind":"assign","kind":"assign"'),
    ):
        with pytest.raises(ReaderError):
            read_decision_body(payload)


def test_split_precision_target_identity_and_sorted_child_keys_are_independent() -> (
    None
):
    document = sample("split")
    assert reference_address(document["parent"], "synthetic") == (
        "synthetic",
        "review_legs",
        ("d" * 64,),
    )
    seal = parse_body(document).digest
    document["children"][0]["amount"]["source_scale"] = None
    assert parse_body(document).digest != seal
    document["children"].reverse()
    with pytest.raises(ReaderError):
        parse_body(document)
    document = sample("split")
    document["parent"] = {"entity_id": "synthetic", "event_id": "c" * 64}
    with pytest.raises(ReaderError):
        parse_body(document)


@pytest.mark.parametrize(
    "operation",
    [
        {
            "kind": "transfer",
            "outgoing": ["c" * 64],
            "incoming": [],
            "commodity": "NZD",
        },
        {
            "kind": "transfer",
            "outgoing": [],
            "incoming": ["c" * 64, "d" * 64],
            "commodity": "NZD",
        },
        {
            "kind": "link",
            "link_kind": "document",
            "target": {"entity_id": "synthetic", "leg_id": "c" * 64},
            "documents": ["d" * 64],
        },
        {
            "kind": "link",
            "link_kind": "lifecycle",
            "attempt": {"entity_id": "synthetic", "event_id": "c" * 64},
            "fee": {"entity_id": "synthetic", "event_id": "d" * 64},
        },
    ],
)
def test_independent_transfer_and_link_reader_agrees_with_live_codec(
    operation: dict,
) -> None:
    document = {
        "schema_version": 1,
        "entity_id": "synthetic",
        "review": {"attempt_digest": "a" * 64, "basis_digest": "b" * 64},
        **operation,
    }
    wire = canonical_wire(document)
    assert read_decision_body(wire).wire == encode_decision_body(
        decode_decision_body(wire)
    )
    document["inferred"] = True
    with pytest.raises(ReaderError):
        read_decision_body(canonical_wire(document))
