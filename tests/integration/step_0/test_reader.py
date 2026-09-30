import hashlib
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from tests.integration.step_0.corpus import load_case as load_specimen
from tests.integration.step_0.reader import (
    ReaderError,
    _sequence_directions,
    canonical_bytes,
    check_assertions,
    effective_posting_date,
    logical_table_digest,
    read_snapshot,
    replay_rows,
    validate_rows,
)

from jbt.artifacts.parquet import Compression, WriterSettings, write_table
from jbt.artifacts.snapshot import BuildInput, SnapshotInput, assemble_snapshot
from jbt.contracts.catalog import catalog, schema_document
from jbt.contracts.primitives import ContractError
from jbt.contracts.schemas import (
    configuration_schema,
    declaration_schema,
    descriptor_schema,
    extraction_schema,
    manifest_schema,
    registry_schema,
)

INDEX_CASE_IDS = tuple(
    json.loads((Path(__file__).parent / "fixtures/index.json").read_text())["cases"]
)
SOURCE_SEQUENCE_DIRECTIONS = {
    ("synthetic-fixture", "statement-events", "source.sequence"): "ascending"
}

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.step_0.corpus import FixtureCase
    from tests.integration.step_0.reader import ReplayResult, Row, Snapshot


@dataclass(frozen=True)
class Case:
    tables: dict[str, list[Row]]
    expected_answers: Row
    aliases: dict[str, str]
    evidence: Row


def load_case(name: str) -> Case:
    specimen = load_specimen(name)
    return Case(
        specimen.tables, specimen.expected_answers, specimen.aliases, specimen.evidence
    )


def approved_schemas() -> dict[str, str]:
    """Pin the approved contract independently of the snapshot being read."""
    documents = {
        "schema": schema_document(),
        "configuration_schema": configuration_schema(),
        "declaration_schema": declaration_schema(),
        "descriptor_schema": descriptor_schema(),
        "extraction_schema": extraction_schema(),
        "manifest_schema": manifest_schema(),
        "registry_schema": registry_schema(),
    }
    return {
        name + ".json": hashlib.sha256(
            canonical_bytes(document, integer_strings=False)
        ).hexdigest()
        for name, document in documents.items()
    }


@pytest.mark.parametrize(
    "case_id",
    [
        "cash",
        "compound-expensed",
        "compound-capitalized",
        "compound-backfill",
        "pools",
        "multi-origin",
        "multi-origin-alternative",
    ],
)
def test_recorded_changes_reconstruct_hand_calculated_inventory(case_id: str) -> None:
    case = load_case(case_id)
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    for alias, expected in case.expected_answers.get("positions", {}).items():
        assert result.quantity_positions[(entity, case.aliases[alias])] == Fraction(
            expected
        )
    for alias, expected in case.expected_answers.get("inventory", {}).items():
        position = case.aliases[alias]
        states = [
            state for key, state in result.inventory.items() if key[1] == position
        ]
        for component, amount in expected.items():
            assert sum(getattr(state, component) for state in states) == Fraction(
                amount
            )


def test_inventory_changes_are_atomic_within_a_split_step() -> None:
    case = load_case("compound-expensed")
    entity = case.tables["build"][0]["entity_id"]
    # Targets first must not turn a split into two separate movements.
    case.tables["inventory_changes"].sort(
        key=lambda row: row["change_role"], reverse=True
    )
    result = replay_rows(case.tables, entity=entity)
    assert sum(state.units for state in result.inventory.values()) == 15
    assert (
        sum(
            state.principal
            for state in result.inventory.values()
            if state.principal is not None
        )
        == 75
    )
    assert sum(
        state.expensed_fee
        for state in result.inventory.values()
        if state.expensed_fee is not None
    ) == Fraction(3, 2)


def test_equal_local_ids_remain_entity_qualified() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    original = deepcopy(case.tables)
    for name, rows in original.items():
        case.tables[name].extend({**row, "entity_id": "other"} for row in rows)
    original_result = replay_rows(case.tables, entity=entity)
    other_result = replay_rows(case.tables, entity="other")
    position = case.aliases["cash-A"]
    assert original_result.quantity_positions == {(entity, position): Fraction(115)}
    assert other_result.quantity_positions == {("other", position): Fraction(115)}


def test_pending_and_review_flags_do_not_infer_participation() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    for transaction in case.tables["transactions"]:
        transaction["review_state"] = "ignored"
    result = replay_rows(case.tables, entity=entity)
    assert result.quantity_positions[(entity, case.aliases["cash-A"])] == 115
    case.tables["transactions"][0]["event_state"] = "pending"
    with pytest.raises(ReaderError, match="nonparticipating"):
        replay_rows(case.tables, entity=entity)


def test_date_cut_cannot_reorder_recorded_booking() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    case.tables["book_steps"][0]["effective_date"] = "2026-02-01"
    with pytest.raises(ReaderError, match="prefix"):
        replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 15))


def test_in_transit_slice_survives_between_departure_and_arrival() -> None:
    case = load_case("in-transit")
    entity = case.tables["build"][0]["entity_id"]
    between = replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 6))
    for alias, expected in case.expected_answers["between_dates"].items():
        position = case.aliases[alias]
        assert sum(
            state.units
            for key, state in between.inventory.items()
            if key[1] == position
        ) == Fraction(expected)
    transit = [
        state
        for key, state in between.inventory.items()
        if key[1] == case.aliases["transit-Q"]
    ]
    assert sum(state.units for state in transit) == 6
    assert all(state.principal is not None for state in transit)
    assert (
        sum(state.principal for state in transit if state.principal is not None) == 100
    )
    final = replay_rows(case.tables, entity=entity)
    assert (
        sum(
            state.units
            for key, state in final.inventory.items()
            if key[1] == case.aliases["transit-Q"]
        )
        == 0
    )


def test_backward_dependencies_are_rejected_not_resorted() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    steps = case.tables["book_steps"]
    case.tables["book_step_dependencies"] = [
        {
            "entity_id": entity,
            "before_step_id": steps[1]["step_id"],
            "after_step_id": steps[0]["step_id"],
        }
    ]
    with pytest.raises(ReaderError, match="backward"):
        replay_rows(case.tables, entity=entity)


def test_posting_unknown_override_prevents_transaction_inheritance() -> None:
    transaction = {"date_settled": "2026-01-02"}
    posting = {"date_settled": None, "date_settled_mode": "unknown"}
    assert effective_posting_date(posting, transaction, role="settled") is None
    posting["date_settled_mode"] = "inherit"
    assert effective_posting_date(posting, transaction, role="settled") == date(
        2026, 1, 2
    )
    posting.update(date_settled="2026-01-03", date_settled_mode="value")
    assert effective_posting_date(posting, transaction, role="settled") == date(
        2026, 1, 3
    )


def test_complete_empty_scope_is_not_a_missing_assertion() -> None:
    case = load_case("cash")
    entity = case.tables["build"][0]["entity_id"]
    account = case.tables["positions"][0]["account_id"]
    case.tables["assertion_scopes"] = [
        {
            "entity_id": entity,
            "scope_id": "scope",
            "account_id": account,
            "measurement": "units",
            "scope_kind": "account_net",
            "coverage_basis": "explicit-basis",
            "selection": "account_all",
            "position_ids": [],
        }
    ]
    case.tables["balance_assertions"] = [
        {
            "entity_id": entity,
            "scope_id": "scope",
            "assertion_set_id": "empty",
            "date": "2026-01-31",
            "assertion_kind": "closing",
            "is_complete": True,
        }
    ]
    assert check_assertions(case.tables, entity=entity, date_roles={})[0].status == (
        "not_evaluable"
    )
    result = check_assertions(
        case.tables, entity=entity, date_roles={"explicit-basis": "posted"}
    )
    assert result[0].status == "fail"
    case.tables["balance_assertions"][0]["is_complete"] = False
    result = check_assertions(
        case.tables, entity=entity, date_roles={"explicit-basis": "posted"}
    )
    assert result[0].status == "pass"


def test_reference_resets_replay_without_calculating_another_settlement() -> None:
    case = load_case("reference")
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    assert result.settlements[(entity, case.aliases["USD"])] == Fraction(
        case.expected_answers["settlement_total"]
    )
    assert result.quantity_positions[(entity, case.aliases["margin"])] == Fraction(
        case.expected_answers["collateral"]
    )
    assert result.references == {}
    reset = replay_rows(case.tables, entity=entity, effective_date=date(2026, 1, 3))
    assert reset.references[(entity, case.aliases["future"], case.aliases["FL"])] == 103
    assert reset.settlements[(entity, case.aliases["USD"])] == 30
    close = next(
        row for row in case.tables["reference_changes"] if row["change_kind"] == "close"
    )
    close["reference_before_coefficient"] = "100"
    with pytest.raises(ReaderError, match="stale reference"):
        replay_rows(case.tables, entity=entity)


def test_gross_assertions_cannot_be_replaced_by_an_equal_net() -> None:
    case = load_case("brokerage")
    entity = case.tables["build"][0]["entity_id"]
    settlement_dates = {
        case.aliases["broker-open"]: "2026-01-01",
        case.aliases["broker-fee"]: "2026-01-02",
    }
    for transaction in case.tables["transactions"]:
        transaction["date_settled"] = settlement_dates[transaction["txn_id"]]
    before = check_assertions(
        case.tables, entity=entity, date_roles={"settled-units": "settled"}
    )
    assert [answer.status for answer in before] == ["pass", "pass"]
    for row in case.tables["balances"]:
        if row["position_id"] == case.aliases["cash-A"]:
            row["amount_coefficient"] = "107"
        elif row["position_id"] == case.aliases["debt"]:
            row["amount_coefficient"] = "-50"
    after = check_assertions(
        case.tables, entity=entity, date_roles={"settled-units": "settled"}
    )
    assert sorted(answer.status for answer in after) == ["fail", "pass"]


def test_fee_lineage_tracks_original_fee_without_another_expense() -> None:
    case = load_case("compound-expensed")
    entity = case.tables["build"][0]["entity_id"]
    result = replay_rows(case.tables, entity=entity)
    originals = {
        identity for state in result.inventory.values() for identity in state.fees
    }
    assert len(originals) == 1
    assert sum(
        value for state in result.inventory.values() for value in state.fees.values()
    ) == Fraction(3, 2)


@pytest.mark.parametrize("mutation", ["version", "foreign", "null", "type", "order"])
def test_shipped_schema_is_interpreted_independently(mutation: str) -> None:
    case = load_case("cash")
    schema = schema_document()
    if mutation == "version":
        schema["schema_version"] = 2
    elif mutation == "foreign":
        case.tables["postings"][0]["txn_id"] = "missing"
    elif mutation == "null":
        case.tables["postings"][0]["amount_scale"] = None
    elif mutation == "type":
        case.tables["postings"][0]["posting_index"] = True
    else:
        case.tables["postings"].reverse()
    with pytest.raises((ReaderError, ValueError)):
        validate_rows(case.tables, schema)


@pytest.mark.parametrize(
    "mutation", ["digest", "interval", "payload", "payload_semantics"]
)
def test_independent_declaration_revision_checks_exact_meaning(
    mutation: str,
) -> None:
    case = load_case("cash")
    declaration = next(
        row
        for row in case.tables["declarations"]
        if row["declaration_kind"] == "position"
    )
    if mutation == "digest":
        declaration["revision_digest"] = "0" * 64
    elif mutation == "interval":
        declaration["valid_from"] = (
            "2026-01-01" if declaration["valid_from"] is None else "2026-01-02"
        )
    else:
        payload = json.loads(declaration["payload_json"])
        if mutation == "payload":
            payload["schema_version"] = "1"
        else:
            payload["purpose"] = "collateral"
        declaration["payload_json"] = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    with pytest.raises(
        ReaderError, match=r"declaration revision identity|kind or version"
    ):
        validate_rows(case.tables, schema_document())


@pytest.mark.parametrize(
    ("target_kind", "target_alias", "expected"),
    [
        (
            "inventory_allocation",
            "spinoff-allocation",
            "basis effect requires an exact inventory change target",
        ),
        (
            "inventory_change",
            "spinoff-child",
            "basis effect targets the wrong change amount",
        ),
    ],
)
def test_independent_basis_effect_targets_exact_change(
    target_kind: str, target_alias: str, expected: str
) -> None:
    case = load_case("corporate-variants")
    application = next(
        row
        for row in case.tables["action_applications"]
        if row["effect_id"] == case.aliases["spinoff-parent-basis"]
    )
    application["target_kind"] = target_kind
    application["target_id"] = case.aliases[target_alias]
    specification = next(
        row
        for row in schema_document()["tables"]
        if row["name"] == "action_applications"
    )
    case.tables["action_applications"].sort(
        key=lambda row: tuple(row[key] for key in specification["sort_key"])
    )
    with pytest.raises(ReaderError, match=expected):
        validate_rows(case.tables, schema_document())


def test_independent_reader_rejects_transferred_reference_repricing() -> None:
    case = load_case("reference-variants")
    validate_rows(case.tables, schema_document())
    tables = deepcopy(case.tables)
    for alias, field in (
        ("variant-transfer-in", "reference_after"),
        ("variant-three-B-close", "reference_before"),
    ):
        change = next(
            row
            for row in tables["reference_changes"]
            if row["reference_change_id"] == case.aliases[alias]
        )
        change[field + "_coefficient"] = "102"
    with pytest.raises(ReaderError, match="transferred reference changed"):
        validate_rows(tables, schema_document())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("domain", "incomparable source sequence scope or domain"),
        ("scope", "incomparable source sequence scope or domain"),
        ("fractional", "invalid source sequence observations"),
    ],
)
def test_independent_reader_rejects_incomparable_source_sequence(
    mutation: str, message: str
) -> None:
    case = load_case("source-sequence")
    validate_rows(
        case.tables,
        schema_document(),
        source_sequence_directions=SOURCE_SEQUENCE_DIRECTIONS,
    )
    tables = deepcopy(case.tables)
    if mutation == "scope":
        row = next(
            row
            for row in tables["source_records"]
            if row["source_record_id"] == case.aliases["source:debit"]
        )
        row["source_scope_id"] = "unrelated-scope"
    else:
        row = next(
            row
            for row in tables["observations"]
            if row["observation_id"]
            == case.aliases[
                "debit-domain" if mutation == "domain" else "debit-sequence"
            ]
        )
        if mutation == "domain":
            row["text_value"] = "unrelated-statement"
        else:
            row["decimal_value_scale"] = 1
    with pytest.raises(ReaderError, match=message):
        validate_rows(
            tables,
            schema_document(),
            source_sequence_directions=SOURCE_SEQUENCE_DIRECTIONS,
        )


def test_source_sequence_requires_one_retained_direction() -> None:
    case = load_case("source-sequence")
    with pytest.raises(ReaderError, match="missing source sequence direction"):
        validate_rows(case.tables, schema_document())
    entry = {
        "source_scope_id": "synthetic-fixture",
        "sequence_domain": "statement-events",
        "field_name": "source.sequence",
        "direction": "ascending",
    }
    with pytest.raises(ReaderError, match="ambiguous source sequence direction"):
        _sequence_directions(
            {"registries": [{"path": "first.json"}, {"path": "second.json"}]},
            {
                "first.json": {"source_sequences": [entry]},
                "second.json": {"source_sequences": [entry]},
            },
        )


def test_descending_source_sequence_registry_preserves_source_ordinals(
    artifact_root: Path,
) -> None:
    case = load_case("source-sequence")
    tables = deepcopy(case.tables)
    for row in tables["book_steps"]:
        if row["txn_id"] == case.aliases["credit"]:
            row["sequence"] = 2
        elif row["txn_id"] == case.aliases["debit"]:
            row["sequence"] = 1
    tables["book_steps"].sort(key=lambda row: (row["entity_id"], row["sequence"]))
    edge = tables["book_step_dependencies"][0]
    before, after = edge["before_step_id"], edge["after_step_id"]
    edge["before_step_id"], edge["after_step_id"] = after, before
    for row in tables["provenance"]:
        if row["record_kind"] == "book_step_dependencies":
            key = json.loads(row["record_id"])
            row["record_id"] = json.dumps(
                [
                    after if value == before else before if value == after else value
                    for value in key
                ],
                separators=(",", ":"),
            )
    digest = _snapshot(
        artifact_root,
        case_id="source-sequence",
        staged_tables=tables,
        sequence_direction="descending",
    )
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert snapshot.tables["book_step_dependencies"][0]["before_step_id"] == after
    assert snapshot.tables["book_step_dependencies"][0]["after_step_id"] == before
    assert {
        row["decimal_value_coefficient"]
        for row in snapshot.tables["observations"]
        if row["field_name"] == "source.sequence"
    } == {"10", "11"}


def test_publication_rejects_registered_direction_disagreeing_with_ordinals(
    artifact_root: Path,
) -> None:
    with pytest.raises(ContractError, match="source_sequence_direction"):
        _snapshot(
            artifact_root,
            case_id="source-sequence",
            sequence_direction="descending",
        )


@pytest.fixture
def artifact_root() -> Iterator[Path]:
    root = Path.cwd() / (".reader-fixture-" + uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _snapshot(  # noqa: PLR0913
    root: Path,
    *,
    empty: bool = False,
    case_id: str = "cash",
    staged_tables: dict[str, list[Row]] | None = None,
    active_correction: str | None = None,
    sequence_direction: str = "ascending",
) -> str:
    case = load_specimen(case_id)
    tables = staged_tables if staged_tables is not None else case.tables
    row = tables["build"][0]
    configuration = canonical_bytes(
        {
            "schema_version": 1,
            "as_of": "2026-12-31",
            "source_authority": [],
            "rendering": [],
            "outputs": [{"sink": "tabular", "required_checks": []}],
            "declaration_source_order": [],
        },
        integer_strings=False,
    )
    sequence_registry = (
        {
            "schema_version": 1,
            "observation_fields": [
                {
                    "field_name": "source.sequence",
                    "value_type": "decimal",
                    "units": "ordinal",
                    "payload_schema": None,
                },
                {
                    "field_name": "source.sequence_domain",
                    "value_type": "text",
                    "units": "none",
                    "payload_schema": None,
                },
            ],
            "status_mappings": [],
            "source_sequences": [
                {
                    "source_scope_id": "synthetic-fixture",
                    "sequence_domain": "statement-events",
                    "field_name": "source.sequence",
                    "direction": sequence_direction,
                }
            ],
            "coverage_bases": [],
            "field_registry": [],
        }
        if case_id == "source-sequence"
        else None
    )
    registry_bytes = (
        canonical_bytes(sequence_registry, integer_strings=False)
        if sequence_registry is not None
        else None
    )
    metadata = {
        "artifacts": [],
        "registries": (
            [
                {
                    "registry_id": "synthetic-sequence",
                    "schema_id": "registry_schema",
                    "path": "registry.json",
                    "byte_digest": hashlib.sha256(registry_bytes).hexdigest(),
                    "content_digest": hashlib.sha256(
                        canonical_bytes(sequence_registry)
                    ).hexdigest(),
                }
            ]
            if registry_bytes is not None
            else []
        ),
        "effective_declarations": [
            {"entity_id": row["entity_id"], "declaration_id": row["declaration_id"]}
            for row in tables["declarations"]
            if not empty and row["declaration_kind"] not in {"correction", "identity"}
        ]
        + (
            [
                {
                    "entity_id": row["entity_id"],
                    "declaration_id": case.aliases["declaration:" + active_correction],
                }
            ]
            if active_correction is not None
            else []
        ),
        "bindings": [
            {
                "entity_id": row["entity_id"],
                "source_scope_id": "synthetic-fixture",
                "importer_id": "synthetic-fixture",
                "account_mappings": [],
            }
        ]
        if not empty
        else [],
        "recovery_set": {
            name: []
            for name in (
                "vault_objects",
                "accession_records",
                "authored_history",
                "dependencies",
                "external_assets",
                "runtime",
            )
        },
        "configuration": {
            "path": "configuration.json",
            "byte_digest": hashlib.sha256(configuration).hexdigest(),
        },
        "toolchain": [
            {"component": "fixture", "version": "1", "content_digest": "c" * 64}
        ],
        "derivations": [
            {
                "derivation_id": identity,
                "algorithm": "fixture-supplied-determinations",
                "version": "1",
                "declaration_ids": [],
                "units": "declared-components",
                "content_digest": hashlib.sha256(
                    cast("str", identity).encode()
                ).hexdigest(),
            }
            for identity in sorted(
                {
                    row["derivation_id"]
                    for row in tables["provenance"]
                    if row["derivation_id"] is not None and not empty
                },
                key=str,
            )
        ],
        "capabilities": [],
        "checks": [],
        "outputs": [{"sink": "tabular", "required_checks": []}],
        "inputs": [],
    }
    metadata["recovery_set"]["runtime"] = [
        {
            "runtime_id": "synthetic-python",
            "version": "3.14",
            "platform": "synthetic",
            "path": "retained/python",
            "byte_digest": "d" * 64,
        }
    ]
    metadata["recovery_set"]["vault_objects"] = [
        {
            "object_id": digest,
            "path": "retained/" + digest,
            "digest_algorithm": "sha256",
            "digest": digest,
            "size_bytes": len(text.encode()),
        }
        for digest, text in sorted(
            cast("dict[str, str]", case.evidence["blobs"]).items()
        )
        if not empty
    ]
    result = assemble_snapshot(
        root,
        SnapshotInput(
            tables={
                name: [] if empty else rows
                for name, rows in tables.items()
                if name != "build"
            },
            builds=[
                BuildInput(
                    entity_id=cast("str", row["entity_id"]),
                    producer_version="independent-reader-test",
                    as_of=cast("str", row["as_of"]),
                    input_fingerprint="a" * 64,
                    is_dirty=False,
                )
            ],
            metadata=metadata,
            resources={
                "configuration.json": configuration,
                **(
                    {"registry.json": registry_bytes}
                    if registry_bytes is not None
                    else {}
                ),
            },
        ),
        WriterSettings(
            compression=Compression.ZSTD,
            row_group_size=1024,
            dictionary=False,
            statistics=True,
        ),
    )
    return result.descriptor_digest


def test_typed_empty_snapshot_is_independently_readable(artifact_root: Path) -> None:
    digest = _snapshot(artifact_root, empty=True)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert all(not rows for name, rows in snapshot.tables.items() if name != "build")
    assert len(snapshot.tables["build"]) == 1


@pytest.mark.parametrize("case_id", INDEX_CASE_IDS)
def test_every_indexed_snapshot_replays_hand_calculated_corpus(
    artifact_root: Path, case_id: str
) -> None:
    digest = _snapshot(artifact_root, case_id=case_id)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    case = load_case(case_id)
    entity = snapshot.tables["build"][0]["entity_id"]
    result = replay_rows(snapshot.tables, entity=entity)
    _assert_snapshot_oracles(case, snapshot.tables, result, entity=entity)
    if case_id == "reference":
        assert result.references == {}
        assert result.settlements[(entity, case.aliases["USD"])] == Fraction(
            case.expected_answers["settlement_total"]
        )


def _staged_correction_rows(
    case: FixtureCase, alias: str, amount: str
) -> dict[str, list[Row]]:
    tables = deepcopy(case.tables)
    replacements = {
        case.aliases["credit-cash"]: amount,
        case.aliases["credit-income"]: "-" + amount,
    }
    for table in ("postings", "posting_weights"):
        for row in tables[table]:
            if replacement := replacements.get(row["posting_id"]):
                row["amount_coefficient"] = replacement
                row["amount_scale"] = 0
                row["amount_source_scale"] = None
    if alias != "retract-credit":
        for row in tables["provenance"]:
            _stage_correction_evidence(
                row, case, replacements, case.aliases["declaration:" + alias]
            )
    return tables


def _stage_correction_evidence(
    row: Row, case: FixtureCase, replacements: dict[str, str], active: str
) -> None:
    if row["field_name"] != "amount":
        return
    posting = json.loads(row["record_id"])[0]
    if posting not in replacements:
        return
    if row["record_kind"] == "postings":
        row["value_origin"] = (
            "correction" if posting == case.aliases["credit-cash"] else "booking"
        )
        row["evidence_kind"] = "declaration"
        row["evidence_id"] = active
        if posting == case.aliases["credit-income"]:
            row["derivation_id"] = "booked-counterpart-from-correction-v1"
    elif row["record_kind"] == "posting_weights":
        row["evidence_kind"] = "declaration"
        row["evidence_id"] = active
        row["derivation_id"] = "booked-weight-from-correction-v1"


def test_reviewed_correction_stages_are_distinct_pinned_snapshots(
    artifact_root: Path,
) -> None:
    case = load_specimen("correction-history")
    original_event = case.aliases["credit"]
    original_leg = case.aliases["credit-cash"]
    financial_digests = []
    for alias, amount, expected in zip(
        ("correct-credit", "replace-credit", "retract-credit"),
        ("25", "22", "20"),
        case.expected_answers["correction_cash"],
        strict=True,
    ):
        location = artifact_root / alias
        location.mkdir()
        descriptor = _snapshot(
            location,
            case_id=case.case_id,
            staged_tables=_staged_correction_rows(case, alias, amount),
            active_correction=alias,
        )
        snapshot = read_snapshot(
            location,
            descriptor_digest=descriptor,
            expected_schema_digests=approved_schemas(),
        )
        assert {
            row["declaration_id"]
            for row in snapshot.manifest["effective_declarations"]
            if row["declaration_id"]
            in {
                case.aliases["declaration:" + name]
                for name in ("correct-credit", "replace-credit", "retract-credit")
            }
        } == {case.aliases["declaration:" + alias]}
        assert {row["txn_id"] for row in snapshot.tables["transactions"]} == {
            row["txn_id"] for row in case.tables["transactions"]
        }
        assert any(
            row["posting_id"] == original_leg and row["txn_id"] == original_event
            for row in snapshot.tables["postings"]
        )
        cash = sum(
            (
                Fraction(int(row["amount_coefficient"]), 10 ** row["amount_scale"])
                for row in snapshot.tables["postings"]
                if row["position_id"] == case.aliases["cash-A"]
            ),
            Fraction(),
        )
        assert cash == Fraction(expected)
        financial_digests.append(snapshot.financial_digest)
    assert len(set(financial_digests)) == 3


def _assert_snapshot_oracles(
    case: Case, tables: dict[str, list[Row]], result: ReplayResult, *, entity: str
) -> None:
    verified = _assert_position_oracles(case, result, entity=entity)
    verified += _assert_inventory_oracles(case, result)
    verified += _assert_special_oracles(case, tables, result, entity=entity)
    verified += _assert_time_loan_oracles(case, tables)
    assert verified, "no independent oracle applied to indexed fixture"


def _assert_position_oracles(case: Case, result: ReplayResult, *, entity: str) -> int:
    verified = 0
    for family in ("positions", "quantities"):
        for alias, expected in case.expected_answers.get(family, {}).items():
            position = case.aliases[alias]
            states = [
                state for key, state in result.inventory.items() if key[1] == position
            ]
            actual = (
                sum(state.units for state in states)
                if states
                else result.quantity_positions.get((entity, position), Fraction())
            )
            assert actual == Fraction(expected), (family, alias)
            verified += 1
    return verified


def _assert_inventory_oracles(case: Case, result: ReplayResult) -> int:
    verified = 0
    for family in ("inventory", "pools"):
        for alias, expected in case.expected_answers.get(family, {}).items():
            states = [
                state
                for key, state in result.inventory.items()
                if key[1 if family == "inventory" else 3] == case.aliases[alias]
            ]
            assert states, (family, alias)
            for component, amount in expected.items():
                values = [getattr(state, component) for state in states]
                if amount is None:
                    assert all(value is None for value in values), alias
                else:
                    assert all(value is not None for value in values), alias
                    assert sum(value for value in values if value is not None) == (
                        Fraction(amount)
                    ), (alias, component)
                verified += 1
    return verified


def _assert_special_oracles(
    case: Case, tables: dict[str, list[Row]], result: ReplayResult, *, entity: str
) -> int:
    verified = 0
    if "outcomes_before_resolution" in case.expected_answers:
        before = replay_rows(tables, entity=entity, effective_date=date(2026, 4, 1))
        for label, replay in (
            ("outcomes_before_resolution", before),
            ("outcomes_after_resolution", result),
        ):
            for commodity, expected in case.expected_answers[label].items():
                position = case.aliases["outcome-" + commodity.removeprefix("O")]
                assert replay.quantity_positions[(entity, position)] == Fraction(
                    expected
                )
                verified += 1
    if "settlement_total" in case.expected_answers:
        assert sum(result.settlements.values()) == Fraction(
            case.expected_answers["settlement_total"]
        )
        verified += 1
    if "same_day_units" in case.expected_answers:
        assert sum(
            state.units
            for key, state in result.inventory.items()
            if key[1] == case.aliases["AQ"]
        ) == Fraction(case.expected_answers["same_day_units"])
        verified += 1
    if "same_day_split_then_buy" in case.expected_answers:
        same_day = replay_rows(tables, entity=entity, effective_date=date(2026, 1, 30))
        assert sum(
            state.units
            for key, state in same_day.inventory.items()
            if key[1] == case.aliases["AQ"]
        ) == Fraction(case.expected_answers["same_day_split_then_buy"])
        verified += 1
    if "empty_count" in case.expected_answers:
        counts = {row["assertion_set_id"]: 0 for row in tables["balance_assertions"]}
        for row in tables["balances"]:
            counts[row["assertion_set_id"]] += 1
        assert sorted(counts.values()) == sorted(
            (
                case.expected_answers["empty_count"],
                case.expected_answers["selected_count"],
            )
        )
        verified += 1
    if "split_survivors" in case.expected_answers:
        assert (
            sum(
                row["identity_state"] == "active"
                for row in tables["economic_identities"]
            )
            == case.expected_answers["split_survivors"]
        )
        verified += 1
    return (
        verified
        + _assert_trade_chain(case, tables)
        + _assert_three_lot(case, tables)
        + _assert_corporate_variants(case, tables)
        + _assert_original_fees(case, tables)
        + _assert_source_sequence(case, tables)
    )


def _assert_trade_chain(case: Case, tables: dict[str, list[Row]]) -> int:
    if "chain_members" not in case.expected_answers:
        return 0
    members = [row for row in tables["links"] if row["link_kind"] == "trade_chain"]
    assert len(members) == int(case.expected_answers["chain_members"])
    assert len({row["member_id"] for row in members}) == len(members)
    assert sum(row["member_role"] == "roll" for row in members) == int(
        case.expected_answers["roll_members"]
    )
    allocations = {row["allocation_id"] for row in tables["disposals"]}
    released = sum(
        -Fraction(
            int(row["principal_delta_coefficient"]),
            10 ** row["principal_delta_scale"],
        )
        for row in tables["inventory_changes"]
        if row["allocation_id"] in allocations and row["change_role"] == "source"
    )
    assert released == Fraction(case.expected_answers["realized_principal"])
    return 1


def _assert_three_lot(case: Case, tables: dict[str, list[Row]]) -> int:
    if "selected_origins" not in case.expected_answers:
        return 0
    slices = [
        row
        for row in tables["inventory_changes"]
        if row["posting_id"] == case.aliases["sale-q"]
    ]
    assert len(slices) == case.expected_answers["selected_origins"]
    assert len({row["allocation_id"] for row in slices}) == len(slices)
    assert {
        row["lot_id"]: (
            Fraction(
                int(row["units_delta_coefficient"]), 10 ** row["units_delta_scale"]
            ),
            Fraction(
                int(row["principal_delta_coefficient"]),
                10 ** row["principal_delta_scale"],
            ),
        )
        for row in slices
    } == {
        case.aliases[alias]: values
        for alias, values in {
            "L1": (Fraction(-1), Fraction(-10)),
            "L2": (Fraction(-1), Fraction(-20)),
            "L3": (Fraction(-2), Fraction(-60)),
        }.items()
    }
    allocation_ids = {row["allocation_id"] for row in slices}
    disposals = [
        row for row in tables["disposals"] if row["allocation_id"] in allocation_ids
    ]
    assert len(disposals) == len(slices)
    proceeds = sum(
        (
            Fraction(
                int(row["proceeds_total_coefficient"]),
                10 ** row["proceeds_total_scale"],
            )
            for row in disposals
        ),
        Fraction(),
    )
    released = -sum(
        (
            Fraction(
                int(row["principal_delta_coefficient"]),
                10 ** row["principal_delta_scale"],
            )
            for row in slices
        ),
        Fraction(),
    )
    assert released == Fraction(case.expected_answers["released_principal"])
    assert proceeds == Fraction(case.expected_answers["proceeds"])
    assert proceeds - released == Fraction(case.expected_answers["gain"])
    return 1


def _assert_corporate_variants(case: Case, tables: dict[str, list[Row]]) -> int:
    if "pre_action_gain" not in case.expected_answers:
        return 0

    def amount(row: Row, field: str) -> Fraction:
        return Fraction(int(row[field + "_coefficient"]), 10 ** row[field + "_scale"])

    changes = {row["change_id"]: row for row in tables["inventory_changes"]}
    effects = {row["effect_id"]: row for row in tables["corporate_action_effects"]}
    applications = {row["effect_id"]: row for row in tables["action_applications"]}
    reverse = effects[case.aliases["reverse-units"]]
    assert amount(reverse, "ratio_numerator") == 1
    assert amount(reverse, "ratio_denominator") == 5
    assert amount(changes[case.aliases["reverse-target"]], "units_delta") == Fraction(
        "1.6"
    )
    for effect, target, principal in (
        ("spinoff-parent-basis", "spinoff-parent", 60),
        ("spinoff-child-basis", "spinoff-child", 20),
        ("capital-basis-effect", "capital-target", 40),
    ):
        application = applications[case.aliases[effect]]
        assert application["target_kind"] == "inventory_change"
        assert application["target_id"] == case.aliases[target]
        assert amount(changes[application["target_id"]], "principal_delta") == principal
    disposal = tables["disposals"][0]
    sale = next(
        row
        for row in tables["inventory_changes"]
        if row["allocation_id"] == disposal["allocation_id"]
    )
    assert amount(disposal, "proceeds_total") + amount(
        sale, "principal_delta"
    ) == Fraction(case.expected_answers["pre_action_gain"])
    assert -amount(changes[case.aliases["capital-source"]], "principal_delta") - amount(
        changes[case.aliases["capital-target"]], "principal_delta"
    ) == Fraction(case.expected_answers["return_of_capital"])
    lots = {row["lot_id"]: row for row in tables["lots"]}
    assert (
        lots[case.aliases["child"]]["acquisition_date"]
        == (case.expected_answers["original_acquisition_date"])
    )
    assert lots[case.aliases["reinvested"]]["acquisition_date"] == "2026-03-11"
    reinvestment = changes[case.aliases["reinvest-target"]]
    assert amount(reinvestment, "units_delta") == Fraction("0.5")
    assert amount(reinvestment, "principal_delta") == 10
    denomination = effects[case.aliases["redenomination-units"]]
    assert denomination["input_commodity_id"] == case.aliases["OLD"]
    assert denomination["resulting_commodity_id"] == case.aliases["NEW"]
    assert amount(denomination, "ratio_numerator") == 1
    assert amount(denomination, "ratio_denominator") == 2
    assert case.aliases["OLD"] != case.aliases["NEW"]
    assert {row["commodity_id"] for row in tables["commodity_symbols"]} == {
        case.aliases["Q"]
    }
    assert {row["symbol"] for row in tables["commodity_symbols"]} == {
        "Q",
        "Q-NEW",
        "Q-é",
    }
    return 1


def _assert_original_fees(case: Case, tables: dict[str, list[Row]]) -> int:
    if "original_fees" not in case.expected_answers:
        return 0
    for currency, expected in case.expected_answers["original_fees"].items():
        source = case.aliases["variant-" + currency.lower() + "-fee"]
        allocations = [
            row for row in tables["fee_allocations"] if row["fee_posting_id"] == source
        ]
        assert len(allocations) == 2
        assert {row["target_id"] for row in allocations} == {
            case.aliases["variant-one"],
            case.aliases["variant-three"],
        }
        assert all(
            row["commodity_id"] == case.aliases[currency]
            and row["treatment"] == "attribution_only"
            and row["book_amount_coefficient"] is None
            for row in allocations
        )
        attributed = -sum(
            (
                Fraction(int(row["amount_coefficient"]), 10 ** row["amount_scale"])
                for row in allocations
            ),
            Fraction(),
        )
        assert attributed == Fraction(expected)
        changes = [
            row
            for row in tables["inventory_fee_changes"]
            if row["fee_allocation_id"]
            in {allocation["fee_allocation_id"] for allocation in allocations}
        ]
        assert len(changes) == len(allocations)
        assert all(row["book_amount_delta_coefficient"] is None for row in changes)
    return 1


def _assert_source_sequence(case: Case, tables: dict[str, list[Row]]) -> int:
    if "credit-sequence" not in case.aliases:
        return 0
    observations = {row["observation_id"]: row for row in tables["observations"]}
    sources = {row["source_record_id"]: row for row in tables["source_records"]}
    for alias, expected in (("credit", 10), ("debit", 11)):
        sequence = observations[case.aliases[alias + "-sequence"]]
        domain = observations[case.aliases[alias + "-domain"]]
        assert sequence["decimal_value_scale"] == 0
        assert int(sequence["decimal_value_coefficient"]) == expected
        assert domain["text_value"] == "statement-events"
        assert (
            sources[sequence["source_record_id"]]["source_scope_id"]
            == (sources[domain["source_record_id"]]["source_scope_id"])
            == "synthetic-fixture"
        )
    assert any(
        row["constraint_kind"] == "source_sequence"
        and row["before_step_id"] == case.aliases["step:credit"]
        and row["after_step_id"] == case.aliases["step:debit"]
        for row in tables["book_step_dependencies"]
    )
    return 1


def _assert_time_loan_oracles(case: Case, tables: dict[str, list[Row]]) -> int:
    if "fold_utc" not in case.expected_answers:
        return 0
    choice = case.evidence["consumer"]["fold_resolution"]
    assert choice == {
        "tzdb_version": "2026a",
        "candidate_offsets": [-240, -300],
        "decision": "later",
        "offset_minutes": -300,
        "shift_minutes": 0,
    }
    for alias, expected in (
        ("fold-deposit", "fold_utc"),
        ("late-local-credit", "late_local_utc"),
    ):
        observed = next(
            row
            for row in tables["event_times"]
            if row["record_id"] == case.aliases[alias] and row["date_role"] == "posted"
        )
        offset = observed["timestamp_offset_minutes"]
        if alias == "fold-deposit":
            assert offset == choice["offset_minutes"]
        instant = datetime.fromisoformat(
            observed["timestamp_date"] + "T" + observed["timestamp_local"]
        ).replace(tzinfo=timezone(timedelta(minutes=offset)))
        assert (
            instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            == (case.expected_answers[expected])
        )
    account = next(
        row for row in tables["accounts"] if row["account_id"] == case.aliases["P"]
    )
    declaration = next(
        row for row in tables["declarations"] if row["declaration_key"] == "P"
    )
    periods = json.loads(declaration["payload_json"])["expected_periods"]
    statements = {row["statement_id"]: row for row in tables["statements"]}
    assert (
        statements[case.aliases["irregular-first"]]["period_end"]
        == (case.expected_answers["first_period_end"])
    )
    assert periods[-1] == {
        key: case.expected_answers["empty_period"][key]
        for key in ("start", "end", "due")
    }
    empty_statement = statements[case.aliases["irregular-empty"]]
    assert (
        empty_statement["period_start"],
        empty_statement["period_end"],
    ) == (
        case.expected_answers["empty_period"]["start"],
        case.expected_answers["empty_period"]["end"],
    )
    transactions = {row["txn_id"]: row for row in tables["transactions"]}
    assert sum(
        1
        for row in tables["postings"]
        if row["account_id"] == case.aliases["P"]
        and empty_statement["period_start"]
        <= transactions[row["txn_id"]]["date_posted"]
        <= empty_statement["period_end"]
    ) == int(case.expected_answers["empty_period"]["movements"])
    assert account["closed_date"] == case.expected_answers["closed_date"]
    assert tables["build"][0]["as_of"] == case.expected_answers["as_of"]
    postings = {row["posting_id"]: row for row in tables["postings"]}
    gross = postings[case.aliases["mortgage-cash"]]
    assert -Fraction(
        int(gross["amount_coefficient"]), 10 ** gross["amount_scale"]
    ) == Fraction(case.expected_answers["known_gross_payment"])
    for alias, expected in (
        ("mortgage-principal", "known_principal_repaid"),
        ("mortgage-interest", "known_interest_expense"),
    ):
        row = postings[case.aliases[alias]]
        assert Fraction(
            int(row["amount_coefficient"]), 10 ** row["amount_scale"]
        ) == Fraction(case.expected_answers[expected])
    pending_rows = [
        row
        for row in tables["postings"]
        if row["txn_id"] == case.aliases["unallocated-loan-observation"]
    ]
    assert len(pending_rows) == 1
    pending = pending_rows[0]
    assert pending["step_id"] is None
    assert Fraction(
        int(pending["amount_coefficient"]), 10 ** pending["amount_scale"]
    ) == -Fraction(case.expected_answers["unallocated_gross"])
    assert case.expected_answers["unallocated_principal"] is None
    assert case.expected_answers["unallocated_interest"] is None
    return 1


def test_read_exact_pinned_snapshot_with_producer_imports_denied(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root)
    script = """
import importlib.abc
import json
import sys
class DenyProducer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jbt" or fullname.startswith("jbt."):
            raise ImportError("producer imports prohibited")
sys.meta_path.insert(0, DenyProducer())
from pathlib import Path
from tests.integration.step_0.reader import read_snapshot, replay_rows
snapshot = read_snapshot(
    Path(sys.argv[1]), descriptor_digest=sys.argv[2],
    expected_schema_digests=json.loads(sys.argv[3]),
)
entity = snapshot.tables["build"][0]["entity_id"]
result = replay_rows(snapshot.tables, entity=entity)
assert list(result.quantity_positions.values()) == [115]
assert not any(name == "jbt" or name.startswith("jbt.") for name in sys.modules)
print(snapshot.financial_digest)
"""
    result = subprocess.run(  # noqa: S603 — fixed local script, no shell.
        [
            sys.executable,
            "-c",
            script,
            str(artifact_root),
            digest,
            json.dumps(approved_schemas()),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    assert result.stdout.strip() == snapshot.financial_digest


@pytest.mark.parametrize(
    "mutation", ["bytes", "extra", "missing", "symlink", "version"]
)
def test_pinned_snapshot_rejects_corruption(artifact_root: Path, mutation: str) -> None:
    digest = _snapshot(artifact_root)
    if mutation == "bytes":
        (artifact_root / "accounts.parquet").write_bytes(b"changed")
    elif mutation == "extra":
        (artifact_root / "unexpected.json").write_text("{}")
    elif mutation == "missing":
        (artifact_root / "accounts.parquet").unlink()
    elif mutation == "symlink":
        (artifact_root / "accounts.parquet").unlink()
        (artifact_root / "accounts.parquet").symlink_to(
            artifact_root / "postings.parquet"
        )
    else:
        descriptor = json.loads((artifact_root / "descriptor.json").read_bytes())
        descriptor["schema_version"] = "2"
        content = canonical_bytes(descriptor)
        (artifact_root / "descriptor.json").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
    with pytest.raises(ReaderError):
        read_snapshot(
            artifact_root,
            descriptor_digest=digest,
            expected_schema_digests=approved_schemas(),
        )


def test_version_one_schema_cannot_redefine_approved_table_semantics(
    artifact_root: Path,
) -> None:
    _snapshot(artifact_root)
    schema_path = artifact_root / "schema.json"
    schema = json.loads(schema_path.read_bytes())
    schema["tables"][0]["columns"][0]["nullable"] = True
    schema_path.write_bytes(canonical_bytes(schema, integer_strings=False))
    descriptor_path = artifact_root / "descriptor.json"
    descriptor = json.loads(descriptor_path.read_bytes())
    schema_entry = next(
        entry for entry in descriptor["payloads"] if entry["path"] == "schema.json"
    )
    schema_entry["byte_digest"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    descriptor_bytes = canonical_bytes(descriptor)
    descriptor_path.write_bytes(descriptor_bytes)
    with pytest.raises(ReaderError, match="unapproved schema semantics"):
        read_snapshot(
            artifact_root,
            descriptor_digest=hashlib.sha256(descriptor_bytes).hexdigest(),
            expected_schema_digests=approved_schemas(),
        )


def test_rechecks_semantics_after_mechanical_checksums_are_recomputed(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    postings = snapshot.tables["postings"]
    postings.append(deepcopy(postings[-1]))
    settings = WriterSettings(
        compression=Compression.ZSTD,
        row_group_size=1024,
        dictionary=False,
        statistics=True,
    )
    specifications = {table.name: table for table in catalog()}
    write_table(
        artifact_root / "postings.parquet",
        specifications["postings"],
        postings,
        settings,
    )
    manifest = snapshot.manifest
    entry = next(row for row in manifest["tables"] if row["name"] == "postings")
    columns = next(
        table["columns"]
        for table in snapshot.schema["tables"]
        if table["name"] == "postings"
    )
    entry["byte_digest"] = hashlib.sha256(
        (artifact_root / "postings.parquet").read_bytes()
    ).hexdigest()
    entry["logical_digest"] = logical_table_digest(columns, postings, version=1)
    entry["row_count"] = str(len(postings))
    manifest_bytes = canonical_bytes(manifest)
    (artifact_root / "manifest.json").write_bytes(manifest_bytes)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    for row in snapshot.tables["build"]:
        row["manifest_digest"] = manifest_digest
    write_table(
        artifact_root / "build.parquet",
        specifications["build"],
        snapshot.tables["build"],
        settings,
    )
    descriptor = json.loads((artifact_root / "descriptor.json").read_bytes())
    descriptor["manifest_digest"] = manifest_digest
    for payload in descriptor["payloads"]:
        payload["byte_digest"] = hashlib.sha256(
            (artifact_root / payload["path"]).read_bytes()
        ).hexdigest()
    descriptor_bytes = canonical_bytes(descriptor)
    (artifact_root / "descriptor.json").write_bytes(descriptor_bytes)
    with pytest.raises(ReaderError, match="duplicate key: postings"):
        read_snapshot(
            artifact_root,
            descriptor_digest=hashlib.sha256(descriptor_bytes).hexdigest(),
            expected_schema_digests=approved_schemas(),
        )


def _rehash_changed_snapshot(
    root: Path,
    snapshot: Snapshot,
    *,
    table: str | None = None,
    resource: str | None = None,
) -> str:
    settings = WriterSettings(
        compression=Compression.ZSTD,
        row_group_size=1024,
        dictionary=False,
        statistics=True,
    )
    specifications = {table.name: table for table in catalog()}
    manifest = snapshot.manifest
    if table is not None:
        write_table(
            root / (table + ".parquet"),
            specifications[table],
            snapshot.tables[table],
            settings,
        )
        entry = next(row for row in manifest["tables"] if row["name"] == table)
        columns = next(
            item["columns"]
            for item in snapshot.schema["tables"]
            if item["name"] == table
        )
        entry["byte_digest"] = hashlib.sha256(
            (root / (table + ".parquet")).read_bytes()
        ).hexdigest()
        entry["logical_digest"] = logical_table_digest(
            columns, snapshot.tables[table], version=1
        )
        entry["row_count"] = str(len(snapshot.tables[table]))
    if resource is not None:
        entry = next(
            row
            for row in [manifest["configuration"], *manifest["registries"]]
            if row["path"] == resource
        )
        entry["byte_digest"] = hashlib.sha256(
            (root / resource).read_bytes()
        ).hexdigest()
    resources = {
        entry["path"]: json.loads((root / entry["path"]).read_bytes())
        for entry in [manifest["configuration"], *manifest["registries"]]
    }
    financial = {
        "entities": [
            [row["entity_id"], row["as_of"]] for row in snapshot.tables["build"]
        ],
        "schemas": {
            name.removesuffix(".json"): json.loads((root / name).read_bytes())
            for name in sorted(approved_schemas())
        },
        "tables": [
            {"name": row["name"], "logical_digest": row["logical_digest"]}
            for row in manifest["tables"]
        ],
        "resources": resources,
        **{
            name: manifest[name]
            for name in ("effective_declarations", "derivations", "checks")
        },
    }
    manifest["logical_content_digest"] = hashlib.sha256(
        canonical_bytes(financial)
    ).hexdigest()
    manifest_bytes = canonical_bytes(manifest)
    (root / "manifest.json").write_bytes(manifest_bytes)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    for row in snapshot.tables["build"]:
        row["manifest_digest"] = manifest_digest
    write_table(
        root / "build.parquet",
        specifications["build"],
        snapshot.tables["build"],
        settings,
    )
    descriptor = json.loads((root / "descriptor.json").read_bytes())
    descriptor["manifest_digest"] = manifest_digest
    for payload in descriptor["payloads"]:
        payload["byte_digest"] = hashlib.sha256(
            (root / payload["path"]).read_bytes()
        ).hexdigest()
    descriptor_bytes = canonical_bytes(descriptor)
    (root / "descriptor.json").write_bytes(descriptor_bytes)
    return hashlib.sha256(descriptor_bytes).hexdigest()


def test_reversed_source_ordinals_fail_after_snapshot_is_mechanically_rehashed(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root, case_id="source-sequence")
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    case = load_case("source-sequence")
    observations = snapshot.tables["observations"]
    for alias, replacement in (("credit-sequence", "11"), ("debit-sequence", "10")):
        row = next(
            row for row in observations if row["observation_id"] == case.aliases[alias]
        )
        row["decimal_value_coefficient"] = replacement
    mutated_digest = _rehash_changed_snapshot(
        artifact_root, snapshot, table="observations"
    )
    with pytest.raises(
        ReaderError, match="source sequence contradicts registered direction"
    ):
        read_snapshot(
            artifact_root,
            descriptor_digest=mutated_digest,
            expected_schema_digests=approved_schemas(),
        )


def test_independent_snapshot_rejects_overlapping_source_authority_after_rehash(
    artifact_root: Path,
) -> None:
    digest = _snapshot(artifact_root)
    snapshot = read_snapshot(
        artifact_root,
        descriptor_digest=digest,
        expected_schema_digests=approved_schemas(),
    )
    case = load_case("cash")
    configuration_path = artifact_root / "configuration.json"
    configuration = json.loads(configuration_path.read_bytes())

    def authority(source: str, start: str, end: str) -> Row:
        identity = case.aliases["source:" + source]
        return {
            "entity_id": snapshot.tables["build"][0]["entity_id"],
            "scope": {"table": "accounts", "key": [case.aliases["A"]]},
            "period_start": start,
            "period_end": end,
            "fact_kind": "movement",
            "coverage_basis_id": "settled-units",
            "authoritative_sources": [{"table": "source_records", "key": [identity]}],
            "mode": "confirming",
            "evidence": [{"kind": "source_record", "id": identity}],
        }

    configuration["source_authority"] = [
        authority("credit", "2026-01-01", "2026-01-15"),
        authority("debit", "2026-01-15", "2026-01-31"),
    ]
    configuration_path.write_bytes(
        canonical_bytes(configuration, integer_strings=False)
    )
    mutated_digest = _rehash_changed_snapshot(
        artifact_root, snapshot, resource="configuration.json"
    )
    with pytest.raises(ReaderError, match="overlapping source authority"):
        read_snapshot(
            artifact_root,
            descriptor_digest=mutated_digest,
            expected_schema_digests=approved_schemas(),
        )


def test_canonical_logical_digest_preserves_types_scales_order_and_duplicates() -> None:
    columns = [
        {"name": "amount_coefficient", "kind": "string", "nullable": False},
        {"name": "amount_scale", "kind": "integer", "nullable": False},
        {"name": "amount_source_scale", "kind": "integer", "nullable": True},
    ]
    rows = [{"amount_coefficient": "842", "amount_scale": 1, "amount_source_scale": 2}]
    baseline = logical_table_digest(columns, rows, version=1)
    changed = deepcopy(rows)
    changed[0]["amount_source_scale"] = 1
    assert logical_table_digest(columns, changed, version=1) != baseline
    assert logical_table_digest(columns, rows * 2, version=1) != baseline
    assert logical_table_digest(list(reversed(columns)), rows, version=1) != baseline
    assert (
        canonical_bytes({"integer": 3, "boolean": True})
        == b'{"boolean":true,"integer":"3"}\n'
    )
    with pytest.raises(ReaderError, match="floating-point"):
        canonical_bytes({"amount": 0.1})
