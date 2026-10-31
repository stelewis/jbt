from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from jbt.contracts.primitives import ContractError
from jbt.contracts.publication import validate_extraction
from jbt.domain.errors import IdentityError
from jbt.domain.identity import AcquiredAnchor, source_record_digest
from jbt.domain.ids import SemanticKey
from jbt.extraction import (
    ExtractionContext,
    ExtractionError,
    decode,
    encode,
    extract,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class OneByteImporter:
    contract_version = 1
    profile = "one-byte"
    profile_version = 1
    maximum_bytes = 1

    def __init__(self, callback: Callable[[bytes, ExtractionContext], dict]) -> None:
        self.callback = callback

    def extract(self, data: bytes, context: ExtractionContext) -> dict:
        return self.callback(data, context)


def test_one_byte_profile_accepts_equality_and_rejects_over_before_adapter() -> None:
    context = ExtractionContext("synthetic", "a" * 64, "one-byte")
    expected = {
        "schema_version": 1,
        "source_scope_id": "synthetic",
        "source_blob_digest": "a" * 64,
        "importer_id": "one-byte",
        "source_scope": {"sections": []},
        "status_mappings": [],
        "records": [],
        "unsupported_content": [],
    }
    seen = []

    def adapter(data: bytes, selected: ExtractionContext) -> dict:
        assert selected == context
        seen.append(data)
        return expected

    plugin = OneByteImporter(adapter)
    assert decode(extract(plugin, b"A", context), context) == expected
    assert seen == [b"A"]
    with pytest.raises(ContractError, match="source_size_limit"):
        extract(plugin, b"AA", context)
    assert seen == [b"A"]


def test_unstructured_adapter_failure_is_safe_and_located() -> None:
    def fail(_data: bytes, _context: ExtractionContext) -> dict:
        message = "synthetic secret source value"
        raise ValueError(message)

    plugin = OneByteImporter(fail)
    context = ExtractionContext("synthetic", "a" * 64, "one-byte")
    with pytest.raises(ExtractionError, match="importer_extraction") as error:
        extract(plugin, b"A", context)
    assert str(error.value) == "importer_extraction: one-byte"


def test_context_requires_retained_digest() -> None:
    with pytest.raises(IdentityError, match="sha256_digest_required"):
        ExtractionContext("main", "sha256:synthetic", "ofx")


def test_wire_round_trip_and_closed_context(
    extraction_documents: tuple[dict, ...],
) -> None:
    value = extraction_documents[0]
    context = ExtractionContext(
        value["source_scope_id"], value["source_blob_digest"], value["importer_id"]
    )
    assert decode(encode(value, context), context) == value
    for field in ("source_scope_id", "source_blob_digest", "importer_id"):
        changed: dict = {
            **value,
            "source_scope": {"sections": []},
            "records": [],
            "unsupported_content": [],
        }
        changed[field] = "b" * 64 if field == "source_blob_digest" else "other"
        with pytest.raises(ContractError, match="extraction_context_mismatch"):
            encode(changed, context)
        other_context = ExtractionContext(
            changed["source_scope_id"],
            changed["source_blob_digest"],
            changed["importer_id"],
        )
        with pytest.raises(ContractError, match="extraction_context_mismatch"):
            decode(encode(changed, other_context), context)
    changed = {**value, "undeclared": True}
    with pytest.raises(ContractError, match="extraction"):
        encode(changed, context)
    with pytest.raises(ContractError, match="extraction"):
        decode(b'{"schema_version":1}', context)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda d: d["records"][0].update(source_key="unpaired"), "source_key_pair"),
        (lambda d: d["records"][0].update(source_section="missing"), "source_section"),
        (
            lambda d: d["records"][0].update(source_account_identifier="other"),
            "section_account_context",
        ),
        (
            lambda d: d["records"][0].update(
                source_correction={
                    "source_key_domain": "synthetic-key",
                    "target_source_key": "old",
                    "action": "replace",
                }
            ),
            "source_correction",
        ),
        (
            lambda d: d["source_scope"]["sections"][0]["evidence_parts"][0].update(
                record_locators=["row:missing"]
            ),
            "evidence_membership",
        ),
        (
            lambda d: d["source_scope"]["sections"][0]["evidence_parts"][1].update(
                meaning="available_credit"
            ),
            "evidence_meaning",
        ),
        (
            lambda d: d["source_scope"]["sections"].append(
                deepcopy(d["source_scope"]["sections"][0])
            ),
            "metadata_unique",
        ),
        (
            lambda d: d["source_scope"]["sections"][0]["evidence_parts"][0][
                "start"
            ].update(timestamp_local="00:00:61"),
            "extraction_source_time",
        ),
        (
            lambda d: d["source_scope"]["sections"][0]["evidence_parts"][1][
                "point"
            ].update(timestamp_offset_seconds=86400),
            "extraction_source_time",
        ),
        (
            lambda d: d["source_scope"]["sections"][0]["evidence_parts"][0][
                "start"
            ].update(timestamp_date="0000-02-30"),
            "extraction_schema",
        ),
        (
            lambda d: d["records"][0]["payload"].update(
                subject_source_record_id="missing"
            ),
            "observation_subject",
        ),
    ],
)
def test_section_key_correction_and_membership_invariants(
    extraction_documents: tuple[dict, ...],
    change: Callable[[dict], None],
    code: str,
) -> None:
    changed = deepcopy(extraction_documents[0])
    change(changed)
    with pytest.raises(ContractError, match=code):
        validate_extraction(changed)


def test_observation_subject_cannot_cross_sections(
    extraction_documents: tuple[dict, ...],
) -> None:
    changed = deepcopy(extraction_documents[0])
    section = changed["source_scope"]["sections"][0]
    changed["source_scope"]["sections"].append(
        {
            **deepcopy(section),
            "section_id": "another-section",
            "evidence_parts": [],
        }
    )
    assertion = next(r for r in changed["records"] if r["kind"] == "assertion")
    observation = next(r for r in changed["records"] if r["kind"] == "observation")
    observation["source_section"] = "another-section"
    observation["source_record_id"] = source_record_digest(
        AcquiredAnchor(
            changed["source_blob_digest"],
            SemanticKey(changed["source_scope_id"]),
            SemanticKey(observation["source_section"]),
            SemanticKey(observation["record_locator"]),
        )
    )
    observation["payload"]["subject_source_record_id"] = assertion["source_record_id"]
    with pytest.raises(ContractError, match="observation_subject"):
        validate_extraction(changed)


def test_source_occurrence_identity_cannot_be_forged_with_matching_references(
    extraction_documents: tuple[dict, ...],
) -> None:
    changed = deepcopy(extraction_documents[0])
    target = next(row for row in changed["records"] if row["kind"] == "assertion")
    original_id = target["source_record_id"]
    observation = next(
        row for row in changed["records"] if row["kind"] == "observation"
    )
    observation["payload"]["subject_source_record_id"] = original_id
    target["source_record_id"] = "f" * 64
    for row in changed["records"]:
        if (
            row["kind"] == "observation"
            and row["payload"]["subject_source_record_id"] == original_id
        ):
            row["payload"]["subject_source_record_id"] = target["source_record_id"]
    with pytest.raises(ContractError, match="source_occurrence_identity"):
        validate_extraction(changed)


def test_generic_price_action_and_document_section_has_no_ofx_requirements() -> None:
    context = ExtractionContext("reference-feed", "b" * 64, "synthetic")

    def source_id(locator: str) -> str:
        return source_record_digest(
            AcquiredAnchor(
                context.source_blob_digest,
                SemanticKey(context.source_scope_id),
                SemanticKey("reference"),
                SemanticKey(locator),
            )
        )

    time = {
        "timestamp_date": "0000-02-29",
        "timestamp_local": None,
        "timestamp_offset_seconds": None,
        "timestamp_zone": None,
        "timestamp_precision": "date",
        "timestamp_fraction_digits": None,
    }
    number = {"coefficient": "2", "scale": 0, "source_scale": 0}
    record = {
        "source_section": "reference",
        "external_record_id": None,
        "source_account_identifier": None,
        "source_key_domain": None,
        "source_key": None,
        "source_correction": None,
    }
    value = {
        "schema_version": 1,
        "source_scope_id": context.source_scope_id,
        "source_blob_digest": context.source_blob_digest,
        "importer_id": context.importer_id,
        "status_mappings": [],
        "source_scope": {
            "sections": [
                {
                    "section_id": "reference",
                    "source_class": "document",
                    "institution": None,
                    "source_account_identifier": None,
                    "stated_currency": None,
                    "account_context": [],
                    "revisions": [],
                    "evidence_parts": [
                        {
                            "part_key": "reference-facts",
                            "kind": "observation_set",
                            "record_locators": [
                                "row:quote",
                                "row:split",
                                "row:document",
                            ],
                        }
                    ],
                }
            ]
        },
        "records": [
            {
                **record,
                "kind": "price",
                "source_record_id": source_id("row:quote"),
                "record_locator": "row:quote",
                "event_key": "quote",
                "payload": {
                    **time,
                    "commodity_token": "SHARE",
                    "quote_commodity_token": "USD",
                    "date": "0000-02-29",
                    "rate": number,
                    "basis_as_stated": None,
                    "market": None,
                },
            },
            {
                **record,
                "kind": "corporate_action",
                "source_record_id": source_id("row:split"),
                "record_locator": "row:split",
                "event_key": "split",
                "payload": {
                    **time,
                    "commodity_token": "SHARE",
                    "effective_date": "0000-02-29",
                    "announcement_date": None,
                    "record_date": None,
                    "payment_date": None,
                    "status_token": None,
                    "action_kind": "split",
                    "effects": [
                        {
                            "effect_key": "split-units",
                            "effect_kind": "units",
                            "input_commodity_id": "SHARE",
                            "resulting_commodity_id": "SHARE",
                            "ratio_numerator": number,
                            "ratio_denominator": {
                                "coefficient": "1",
                                "scale": 0,
                                "source_scale": 0,
                            },
                            "cash_per_unit": None,
                            "cash_total": None,
                            "cash_commodity_id": None,
                            "basis_per_unit": None,
                            "basis_total": None,
                            "basis_commodity_id": None,
                            "symbol_id": None,
                            "scope_declaration_id": None,
                        }
                    ],
                },
            },
            {
                **record,
                "kind": "observation",
                "source_record_id": source_id("row:document"),
                "record_locator": "row:document",
                "event_key": "document",
                "payload": {
                    "subject_source_record_id": None,
                    "field_name": "source.description",
                    "observation_kind": "description",
                    "value_type": "text",
                    "decimal_value": None,
                    "text_value": "  Reference notice e\u0301  ",
                    "date_value": None,
                    "commodity_token": None,
                },
            },
        ],
        "unsupported_content": [],
    }
    assert decode(encode(value, context), context) == value
