from copy import deepcopy
from datetime import date

import pytest

from jbt.contracts.catalog import catalog, tabular_schema
from jbt.contracts.publication import (
    _active_refs,
    validate_configuration,
    validate_descriptor,
    validate_extraction,
    validate_manifest,
    validate_registries,
)
from jbt.domain.guards import GuardField, GuardTarget, projection_digest
from jbt.domain.ids import EntityId, RecordId, RecordKind


@pytest.fixture
def empty_manifest() -> tuple[dict, dict]:
    entity = {
        "entity_id": "e",
        "producer_version": "test",
        "schema_version": 1,
        "schema_digest": "0" * 64,
        "as_of": "2026-01-01",
        "execution_fingerprint": "1" * 64,
        "is_dirty": False,
    }
    tables = {table.name: [] for table in catalog()}
    tables["build"] = [{**entity, "manifest_digest": "2" * 64}]
    manifest = {
        "schema_version": 1,
        "entities": [entity],
        "tables": [
            {
                "name": name,
                "path": f"{name}.parquet",
                "byte_digest": "3" * 64,
                "logical_digest": "4" * 64,
                "row_count": 0,
            }
            for name in sorted(tables)
            if name != "build"
        ],
        "schemas": [
            {
                "schema_id": "tabular_schema",
                "schema_version": 1,
                "path": "tabular_schema.json",
                "byte_digest": "5" * 64,
            }
        ],
        "artifacts": [],
        "registries": [],
        "configuration": {"path": "config.json", "byte_digest": "6" * 64},
        "effective_declarations": [],
        "bindings": [],
        "recovery_set": {
            "vault_objects": [],
            "accession_records": [],
            "authored_history": [],
            "dependencies": [],
            "external_assets": [],
            "runtime": [
                {
                    "runtime_id": "python",
                    "version": "3.14",
                    "platform": "synthetic",
                    "path": "recovery/python.tar",
                    "byte_digest": "9" * 64,
                }
            ],
        },
        "inputs": [],
        "writer_settings": {
            "library": "pyarrow",
            "version": "test",
            "compression": "zstd",
            "dictionary": False,
            "statistics": True,
            "row_group_size": 100,
            "data_page_version": "1.0",
        },
        "toolchain": [
            {"component": "python", "version": "test", "content_digest": "7" * 64}
        ],
        "derivations": [],
        "capabilities": [],
        "outputs": [],
        "checks": [],
        "logical_content_digest": "8" * 64,
        "numeric_limits": {
            "coefficient_digits": 96,
            "scale": 38,
            "source_scale": 38,
            "intermediate_digits": 384,
            "intermediate_scale": 152,
        },
    }
    return manifest, tables


def test_complete_manifest_has_no_checksum_cycle(
    empty_manifest: tuple[dict, dict],
) -> None:
    manifest, tables = empty_manifest
    validate_manifest(manifest, tables)
    descriptor = {
        "schema_version": 1,
        "manifest_path": "manifest.json",
        "manifest_digest": "2" * 64,
        "payloads": [
            {"path": row["path"], "byte_digest": row["byte_digest"]}
            for row in [
                *manifest["tables"],
                *manifest["schemas"],
                manifest["configuration"],
            ]
        ]
        + [{"path": "build.parquet", "byte_digest": "9" * 64}],
    }
    validate_descriptor(descriptor, manifest)
    descriptor["payloads"].append({"path": "manifest.json", "byte_digest": "2" * 64})
    with pytest.raises(ValueError, match="descriptor_payload_inventory"):
        validate_descriptor(descriptor, manifest)


def test_manifest_rejects_unknown_fields_and_uninventoried_tables(
    empty_manifest: tuple[dict, dict],
) -> None:
    manifest, tables = empty_manifest
    with pytest.raises(ValueError, match="manifest_schema"):
        validate_manifest({**manifest, "extras": {}}, tables)
    manifest["tables"].pop()
    with pytest.raises(ValueError, match="manifest_table_inventory"):
        validate_manifest(manifest, tables)


def test_required_failure_cannot_be_published(
    empty_manifest: tuple[dict, dict],
) -> None:
    manifest, tables = empty_manifest
    manifest["outputs"] = [{"sink": "tabular", "required_checks": ["coverage"]}]
    with pytest.raises(ValueError, match="required_check_missing"):
        validate_manifest(manifest, tables)
    manifest["checks"] = [
        {
            "check_id": "coverage",
            "check_kind": "coverage",
            "entity_id": "e",
            "status": "not_evaluable",
            "severity": "error",
            "scope": [],
            "expected": {"kind": "boolean", "value": True},
            "observed": {"kind": "unavailable", "reason": "missing_evidence"},
            "evidence": [],
            "policy_declaration_ids": [],
            "exception_declaration_id": None,
        }
    ]
    with pytest.raises(ValueError, match="required_check_failed"):
        validate_manifest(manifest, tables)
    manifest["outputs"] = []
    validate_manifest(manifest, tables)


def test_historical_target_need_not_resolve_but_active_target_must(
    empty_manifest: tuple[dict, dict],
) -> None:
    manifest, tables = empty_manifest
    manifest["effective_declarations"] = [
        {"entity_id": "e", "declaration_id": "absent"}
    ]
    with pytest.raises(ValueError, match="active_reference"):
        validate_manifest(manifest, tables)


def test_active_guard_matches_domain_projection_bytes_and_detects_drift(
    empty_manifest: tuple[dict, dict],
) -> None:
    _, tables = empty_manifest
    tables["accounts"] = [
        {
            "entity_id": "e",
            "account_id": "a",
            "statement_cadence": "monthly",
            "opened_date": None,
        }
    ]
    target = GuardTarget(
        RecordId(EntityId("e"), RecordKind.ACCOUNT, "a" * 64),
        (
            GuardField("account_id", "string", nullable=False, value="a"),
            GuardField("opened_date", "date", nullable=True, value=None),
            GuardField("statement_cadence", "string", nullable=True, value="monthly"),
        ),
        active=True,
    )
    guard = {
        "target": {"table": "accounts", "key": ["a"]},
        "fields": ["account_id", "opened_date", "statement_cadence"],
        "expected_digest": projection_digest(
            target, ("account_id", "opened_date", "statement_cadence")
        ),
    }
    columns = next(
        spec for spec in tabular_schema()["tables"] if spec["name"] == "accounts"
    )["columns"]
    assert any(column.get("enum") for column in columns)
    _active_refs(guard, "e", tables)
    reversed_guard = {**guard, "fields": list(reversed(guard["fields"]))}
    with pytest.raises(ValueError, match="guard_field_order"):
        _active_refs(reversed_guard, "e", tables)
    self_guard = {**guard, "fields": ["guard_digest"]}
    with pytest.raises(ValueError, match="guard_field_reference"):
        _active_refs(self_guard, "e", tables)
    tables["accounts"][0]["account_id"] = "changed"
    with pytest.raises(ValueError, match="active_reference"):
        _active_refs(guard, "e", tables)
    tables["accounts"][0]["account_id"] = "a"
    tables["accounts"][0]["opened_date"] = "2026-01-01"
    with pytest.raises(ValueError, match="guard_projection_changed"):
        _active_refs(guard, "e", tables)
    dated_target = GuardTarget(
        target.identifier,
        (
            GuardField("account_id", "string", nullable=False, value="a"),
            GuardField("opened_date", "date", nullable=True, value=date(2026, 1, 1)),
            GuardField("statement_cadence", "string", nullable=True, value="monthly"),
        ),
        active=True,
    )
    dated_guard = {
        **guard,
        "expected_digest": projection_digest(dated_target, tuple(guard["fields"])),
    }
    _active_refs(dated_guard, "e", tables)


def test_registry_status_tokens_are_closed_by_evidenced_mapping() -> None:
    registries = {
        "schema_version": 1,
        "observation_fields": [
            {
                "field_name": "source.status",
                "value_type": "text",
                "units": "none",
                "payload_schema": None,
            },
            {
                "field_name": "source.sequence",
                "value_type": "decimal",
                "units": "ordinal",
                "payload_schema": None,
            },
        ],
        "status_mappings": [
            {
                "source_scope_id": "scope",
                "source_field": "source.status",
                "source_token": " PENDING ",
                "event_key": "event",
                "candidate_state": "pending",
            }
        ],
        "source_sequences": [],
        "coverage_bases": [],
        "field_registry": [],
    }
    validate_registries(registries)
    assert registries["status_mappings"][0]["source_token"] == " PENDING "  # noqa: S105
    registries["status_mappings"][0]["candidate_state"] = "unknown-default"
    with pytest.raises(ValueError, match="registries_schema"):
        validate_registries(registries)


def test_json_observation_registry_requires_closed_schema_id() -> None:
    registries = {
        "schema_version": 1,
        "observation_fields": [
            {
                "field_name": "broker.match",
                "value_type": "json_text",
                "units": "none",
                "payload_schema": None,
            }
        ],
        "status_mappings": [],
        "source_sequences": [],
        "coverage_bases": [],
        "field_registry": [],
    }
    with pytest.raises(ValueError, match="observation_payload_schema"):
        validate_registries(registries)
    registries["observation_fields"][0]["payload_schema"] = "broker-match-v1"
    validate_registries(registries)


def test_configuration_is_closed_and_commits_horizon() -> None:
    config = {
        "schema_version": 1,
        "as_of": "2026-01-01",
        "source_authority": [],
        "rendering": [],
        "outputs": [],
        "declaration_source_order": [],
    }
    validate_configuration(config, {})
    with pytest.raises(ValueError, match="configuration_schema"):
        validate_configuration({**config, "today": True}, {})


def test_source_authority_conflicts_only_within_overlapping_fact_scope() -> None:
    tables = {
        "accounts": [
            {"entity_id": "e", "account_id": "A"},
            {"entity_id": "e", "account_id": "B"},
        ],
        "source_records": [
            {"entity_id": "e", "source_record_id": "first"},
            {"entity_id": "e", "source_record_id": "second"},
        ],
    }

    def authority(account: str, source: str, start: str, end: str) -> dict:
        return {
            "entity_id": "e",
            "scope": {"table": "accounts", "key": [account]},
            "period_start": start,
            "period_end": end,
            "fact_kind": "movement",
            "coverage_basis_id": "settled-units",
            "authoritative_sources": [{"table": "source_records", "key": [source]}],
            "mode": "confirming",
            "evidence": [{"kind": "source_record", "id": source}],
        }

    first = authority("A", "first", "2026-01-01", "2026-01-15")
    different_scope = authority("B", "second", "2026-01-15", "2026-01-31")
    config = {
        "schema_version": 1,
        "as_of": "2026-12-31",
        "source_authority": [first, different_scope],
        "rendering": [],
        "outputs": [],
        "declaration_source_order": [],
    }
    validate_configuration(config, tables)

    conflicting = authority("A", "second", "2026-01-15", "2026-01-31")
    with pytest.raises(ValueError, match="authority_conflict"):
        validate_configuration(
            {**config, "source_authority": [first, conflicting]}, tables
        )
    with pytest.raises(ValueError, match="authority_conflict"):
        validate_configuration(
            {**config, "source_authority": [first, {**first}]}, tables
        )

    contiguous = authority("A", "second", "2026-01-16", "2026-01-31")
    validate_configuration({**config, "source_authority": [first, contiguous]}, tables)


def test_extraction_unknowns_are_not_fabricated_zeroes() -> None:
    extract: dict = {
        "schema_version": 1,
        "source_scope_id": "scope",
        "source_blob_digest": "blob",
        "importer_id": "synthetic",
        "source_scope": {
            "source_class": "document",
            "institution": None,
            "period_start": None,
            "period_end": None,
            "completeness": "unstated",
            "source_accounts": [],
            "revisions": [],
        },
        "records": [
            {
                "kind": "observation",
                "source_record_id": "source",
                "source_section": "account",
                "record_locator": "row:1",
                "external_record_id": None,
                "event_key": "event",
                "source_account_identifier": None,
                "payload": {
                    "field_name": "source.status",
                    "observation_kind": "source_status",
                    "value_type": "text",
                    "decimal_value": None,
                    "text_value": "unmapped-token",
                    "date_value": None,
                    "commodity_token": None,
                },
            }
        ],
        "unsupported_content": [],
    }
    validate_extraction(extract)
    invalid = deepcopy(extract)
    invalid["records"][0]["payload"]["decimal_value"] = {
        "coefficient": "0",
        "scale": 0,
        "source_scale": None,
    }
    with pytest.raises(ValueError, match="observation_value"):
        validate_extraction(invalid)
