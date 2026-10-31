"""Cross-document validation for retained metadata and active decisions."""

import itertools
from collections import defaultdict
from collections.abc import Mapping
from datetime import date

from jsonschema import ValidationError

from jbt.contracts.catalog import Column, Table, catalog, tabular_schema
from jbt.contracts.declarations import decode_declaration, field_registry
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.schemas import (
    configuration_schema,
    descriptor_schema,
    extraction_schema,
    manifest_schema,
    registry_schema,
    validate_document,
)
from jbt.contracts.validation import validate_tables
from jbt.domain.errors import IdentityError, TemporalError
from jbt.domain.guards import (
    Guard,
    GuardField,
    GuardOutcome,
    GuardReference,
    GuardTarget,
    evaluate_guard,
)
from jbt.domain.identity import AcquiredAnchor, source_record_digest
from jbt.domain.ids import EntityId, SemanticKey
from jbt.domain.time import Precision, SourceTimeObservation

type Tables = Mapping[str, list[dict]]


def _validate(document: object, schema: dict, kind: str) -> None:
    try:
        validate_document(document, schema)
    except ValidationError as error:
        code = f"{kind}_schema"
        raise ContractError(code, kind) from error


def _unique(rows: list[dict], fields: tuple[str, ...], location: str) -> None:
    keys = [tuple(row[field] for field in fields) for row in rows]
    require(len(keys) == len(set(keys)), "metadata_unique", location)


def validate_registries(registries: dict) -> None:
    """Validate finite observation/status/sequence/coverage registries."""
    _validate(registries, registry_schema(), "registries")
    for name, fields in {
        "observation_fields": ("field_name",),
        "status_mappings": (
            "source_scope_id",
            "source_field",
            "source_token",
            "event_key",
        ),
        "source_sequences": ("source_scope_id", "sequence_domain", "field_name"),
        "coverage_bases": ("basis_id",),
        "field_registry": ("field_name",),
    }.items():
        _unique(registries[name], fields, f"registries.{name}")
    for definition in registries["observation_fields"]:
        require(
            (definition["payload_schema"] is not None)
            == (definition["value_type"] == "json_text"),
            "observation_payload_schema",
            "registries.observation_fields",
        )
    for basis in registries["coverage_bases"]:
        require(
            (basis["quote_commodity_id"] is not None)
            == (basis["measurement"] == "value"),
            "coverage_quote",
            "registries.coverage_bases",
        )
    builtins = {r["field_name"]: r for r in field_registry()}
    for definition in registries["field_registry"]:
        require(
            definition["field_name"] in builtins
            and definition == builtins[definition["field_name"]],
            "field_registry_semantics",
            "registries.field_registry",
        )
    observations = {r["field_name"]: r for r in registries["observation_fields"]}
    for mapping in registries["status_mappings"]:
        require(
            mapping["source_field"] in observations
            and observations[mapping["source_field"]]["value_type"] == "text",
            "status_mapping_field",
            "registries.status_mappings",
        )
    for sequence in registries["source_sequences"]:
        require(
            sequence["field_name"] in observations
            and observations[sequence["field_name"]]["value_type"] == "decimal"
            and observations[sequence["field_name"]]["units"] == "ordinal",
            "source_sequence_field",
            "registries.source_sequences",
        )


def _resolve(reference: dict, entity: str, tables: Tables) -> dict:
    specs = {t["name"]: t for t in tabular_schema()["tables"]}
    table = reference["table"]
    require(table in specs, "record_kind", "reference")
    fields = specs[table]["primary_key"][1:]
    require(len(reference["key"]) == len(fields), "record_key_arity", "reference")
    found = [
        row
        for row in tables[table]
        if row["entity_id"] == entity
        and [row[field] for field in fields] == reference["key"]
    ]
    require(len(found) == 1, "active_reference", table)
    return found[0]


def validate_configuration(configuration: dict, tables: Tables) -> None:
    """Check source precedence and rendering targets without selecting facts."""
    _validate(configuration, configuration_schema(), "configuration")
    authority = defaultdict(list)
    for entry in configuration["source_authority"]:
        require(
            sum(row["account_id"] == entry["account_id"] for row in tables["accounts"])
            == 1,
            "authority_scope",
            "configuration.source_authority",
        )
        require(
            entry["period_start"] < entry["period_end"],
            "authority_period",
            "configuration.source_authority",
        )
        key = (
            entry["account_id"],
            entry["fact_kind"],
            entry["coverage_basis_id"],
        )
        for previous in authority[key]:
            overlap = (
                entry["period_start"] < previous["period_end"]
                and previous["period_start"] < entry["period_end"]
            )
            require(
                not overlap,
                "authority_conflict",
                "configuration.source_authority",
            )
        authority[key].append(entry)
    names = set()
    targets = set()
    for mapping in configuration["rendering"]:
        require(
            mapping["target"]["table"] in {"positions", "categories"},
            "render_target",
            "configuration.rendering",
        )
        _resolve(mapping["target"], mapping["entity_id"], tables)
        target = (
            mapping["entity_id"],
            mapping["sink"],
            mapping["target"]["table"],
            tuple(mapping["target"]["key"]),
        )
        require(
            target not in targets, "render_target_unique", "configuration.rendering"
        )
        targets.add(target)
        for name in (mapping["positive_name"], mapping["negative_name"]):
            if name is None:
                continue
            key = (mapping["entity_id"], mapping["sink"], name)
            require(
                key not in names and "\n" not in name and "\r" not in name,
                "render_name_collision",
                "configuration.rendering",
            )
            names.add(key)
            if mapping["sink"] == "beancount":
                parts = name.split(":")
                require(
                    parts[0]
                    in {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
                    and len(parts) > 1
                    and all(part and part[0].isupper() for part in parts),
                    "beancount_name",
                    "configuration.rendering",
                )


def _validate_source_time(value: dict) -> None:
    try:
        SourceTimeObservation(
            value["timestamp_date"],
            value["timestamp_local"],
            value["timestamp_offset_seconds"],
            value["timestamp_zone"],
            Precision(value["timestamp_precision"]),
            value["timestamp_fraction_digits"],
        )
    except IdentityError, TemporalError, ValueError:
        code = "extraction_source_time"
        raise ContractError(code, "extraction.time") from None


def validate_extraction(extract: dict) -> None:  # noqa: C901
    """Validate source records without promoting status tokens to event state."""
    _validate(extract, extraction_schema(), "extraction")
    _unique(
        extract["records"],
        ("source_record_id", "event_key", "kind"),
        "extraction.records",
    )
    sections = extract["source_scope"]["sections"]
    _unique(
        extract["status_mappings"],
        ("source_field", "source_token", "event_key"),
        "extraction.status_mappings",
    )
    _unique(sections, ("section_id",), "extraction.sections")
    section_by_id = {section["section_id"]: section for section in sections}
    memberships = {
        (record["source_section"], record["record_locator"])
        for record in extract["records"]
    }
    subjects = {
        (record["source_section"], record["source_record_id"])
        for record in extract["records"]
        if record["kind"] != "observation"
    }
    for section in sections:
        _unique(
            section["account_context"], ("field_name",), "extraction.account_context"
        )
        _unique(section["evidence_parts"], ("part_key",), "extraction.evidence_parts")
        for part in section["evidence_parts"]:
            _unique(
                [{"locator": locator} for locator in part["record_locators"]],
                ("locator",),
                "extraction.membership",
            )
            require(
                all(
                    (section["section_id"], locator) in memberships
                    for locator in part["record_locators"]
                ),
                "evidence_membership",
                "extraction.sections",
            )
            if part["kind"] == "movement_interval":
                _validate_source_time(part["start"])
                _validate_source_time(part["end"])
                require(
                    part["start"]["timestamp_date"] <= part["end"]["timestamp_date"],
                    "statement_period",
                    "extraction.sections",
                )
            elif part["kind"] != "observation_set":
                _validate_source_time(part["point"])
                require(
                    part["meaning"]
                    in {
                        "balance_point": {"settled_position"},
                        "available_balance": {"available_funds", "available_credit"},
                        "pending_snapshot": {"pending"},
                    }[part["kind"]],
                    "evidence_meaning",
                    "extraction.sections",
                )
    keyed = [
        record
        for record in extract["records"]
        if record["kind"] == "transaction" and record["source_key"] is not None
    ]
    _unique(
        keyed,
        ("source_section", "source_key_domain", "source_key"),
        "extraction.source_keys",
    )
    for record in extract["records"]:
        require(
            record["source_section"] in section_by_id,
            "source_section",
            "extraction.records",
        )
        try:
            expected_id = source_record_digest(
                AcquiredAnchor(
                    extract["source_blob_digest"],
                    SemanticKey(extract["source_scope_id"]),
                    SemanticKey(record["source_section"]),
                    SemanticKey(record["record_locator"]),
                )
            )
        except IdentityError as error:
            code = "source_occurrence_identity"
            raise ContractError(code, "extraction.records") from error
        require(
            record["source_record_id"] == expected_id,
            "source_occurrence_identity",
            "extraction.records",
        )
        require(
            record["source_account_identifier"]
            == section_by_id[record["source_section"]]["source_account_identifier"],
            "section_account_context",
            "extraction.records",
        )
        require(
            (record["source_key_domain"] is None) == (record["source_key"] is None),
            "source_key_pair",
            "extraction.records",
        )
        correction = record["source_correction"]
        if correction is not None:
            require(
                record["kind"] == "transaction"
                and record["source_key"] is not None
                and correction["source_key_domain"] == record["source_key_domain"],
                "source_correction",
                "extraction.records",
            )
        payload = record["payload"]
        if "timestamp_date" in payload:
            _validate_source_time(payload)
        if record["kind"] == "transaction":
            for time in payload["times"]:
                _validate_source_time(time)
            _unique(payload["legs"], ("posting_key",), "extraction.legs")
            _unique(payload["times"], ("date_role",), "extraction.times")
        if record["kind"] == "observation":
            subject = payload["subject_source_record_id"]
            require(
                subject is None or (record["source_section"], subject) in subjects,
                "observation_subject",
                "extraction.records",
            )
            present = tuple(
                payload[field] is not None
                for field in ("decimal_value", "text_value", "date_value")
            )
            require(
                present
                == {
                    "decimal": (True, False, False),
                    "text": (False, True, False),
                    "date": (False, False, True),
                }[payload["value_type"]],
                "observation_value",
                "extraction.records",
            )


def _active_refs(value: object, entity: str, tables: Tables) -> None:
    if isinstance(value, dict):
        if set(value) == {"target", "fields", "expected_digest"}:
            _active_guard(value, entity, tables)
            return
        if set(value) == {"table", "key"}:
            _resolve(value, entity, tables)
            return
        if set(value) == {"txn_id", "step_key"}:
            matches = [
                r
                for r in tables["book_steps"]
                if r["entity_id"] == entity
                and r["txn_id"] == value["txn_id"]
                and r["step_key"] == value["step_key"]
            ]
            require(len(matches) == 1, "active_step_reference", "declarations")
        if set(value) == {"kind", "id"} and value["kind"] in {
            "source_record",
            "declaration",
        }:
            table = {"source_record": "source_records", "declaration": "declarations"}[
                value["kind"]
            ]
            _resolve({"table": table, "key": [value["id"]]}, entity, tables)
        for child in value.values():
            _active_refs(child, entity, tables)
    elif isinstance(value, list):
        for child in value:
            _active_refs(child, entity, tables)


def _guard_field(column: Column, value: object) -> GuardField:
    if value is None:
        require(column.nullable, "guard_field_null", column.name)
        parsed = None
    elif column.kind == "date" and isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            code = "guard_field_date"
            raise ContractError(code, column.name) from error
        require(parsed.isoformat() == value, "guard_field_date", column.name)
    elif column.kind == "list" and isinstance(value, list):
        require(
            all(isinstance(item, str) for item in value),
            "guard_field_value",
            column.name,
        )
        parsed = tuple(value)
    elif (
        (column.kind == "string" and isinstance(value, str))
        or (column.kind == "integer" and type(value) is int)
        or (column.kind == "boolean" and type(value) is bool)
    ):
        parsed = value
    else:
        code = "guard_field_value"
        raise ContractError(code, column.name)
    return GuardField(column.name, column.kind, column.nullable, parsed)


def _guard_key(value: object, column: Column) -> str | int | date:
    parsed = _guard_field(column, value).value
    if type(parsed) is bool or not isinstance(parsed, (str, int, date)):
        code = "record_key_type"
        raise ContractError(code, column.name)
    return parsed


def _guard_reference(entity: str, table: Table, key: list) -> GuardReference:
    columns = {column.name: column for column in table.columns}
    names = table.primary_key[1:]
    require(len(key) == len(names), "record_key_arity", "reference")
    return GuardReference(
        EntityId(entity),
        SemanticKey(table.name),
        tuple(
            _guard_key(value, columns[name])
            for name, value in zip(names, key, strict=True)
        ),
    )


def _guard_target(
    reference: GuardReference, table: Table, row: dict, fields: tuple[str, ...]
) -> GuardTarget:
    columns = {column.name: column for column in table.columns}
    return GuardTarget(
        reference,
        tuple(_guard_field(columns[name], row[name]) for name in fields if name in row),
        active=True,
    )


def _active_guard(guard: dict, entity: str, tables: Tables) -> None:
    target = guard["target"]
    specs = {table.name: table for table in catalog()}
    require(target["table"] in specs, "record_kind", "reference")
    table = specs[target["table"]]
    columns = {column.name: column for column in table.columns}
    fields = guard["fields"]
    require(
        fields == sorted(set(fields), key=lambda field: field.encode("utf-8")),
        "guard_field_order",
        "declarations",
    )
    require(
        all(field in columns for field in fields)
        and not {"expected_digest", "guard_digest"}.intersection(fields),
        "guard_field_reference",
        "declarations",
    )
    reference = _guard_reference(entity, table, target["key"])
    reviewed = Guard(reference, tuple(fields), guard["expected_digest"])
    targets = []
    for row in tables[table.name]:
        if row["entity_id"] != entity:
            continue
        names = table.primary_key[1:]
        require(
            all(name in row for name in names), "record_key_field_missing", table.name
        )
        row_reference = _guard_reference(entity, table, [row[name] for name in names])
        if row_reference == reference:
            targets.append(_guard_target(row_reference, table, row, reviewed.fields))
    evaluation = evaluate_guard(reviewed, tuple(targets))
    codes = {
        GuardOutcome.MISSING: "guard_target_missing",
        GuardOutcome.AMBIGUOUS: "guard_target_ambiguous",
        GuardOutcome.INACTIVE: "guard_target_inactive",
        GuardOutcome.CHANGED: "guard_projection_changed",
    }
    if evaluation.outcome != GuardOutcome.APPLIED:
        raise ContractError(codes[evaluation.outcome], "declarations")


def _ordering_dependency(row: dict, payload: dict, tables: Tables) -> None:
    steps = {
        (step["txn_id"], step["step_key"]): step["step_id"]
        for step in tables["book_steps"]
        if step["entity_id"] == row["entity_id"]
    }
    before = steps[(payload["before"]["txn_id"], payload["before"]["step_key"])]
    after = steps[(payload["after"]["txn_id"], payload["after"]["step_key"])]
    require(
        any(
            edge["entity_id"] == row["entity_id"]
            and edge["before_step_id"] == before
            and edge["after_step_id"] == after
            and edge["constraint_kind"] == "decision"
            and edge["declaration_id"] == row["declaration_id"]
            for edge in tables["book_step_dependencies"]
        ),
        "ordering_dependency",
        "declarations",
    )


def _active_declarations(manifest: dict, tables: Tables) -> None:
    effective = manifest["effective_declarations"]
    _unique(
        effective, ("entity_id", "declaration_id"), "manifest.effective_declarations"
    )
    revisions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for selected in effective:
        row = _resolve(
            {"table": "declarations", "key": [selected["declaration_id"]]},
            selected["entity_id"],
            tables,
        )
        key = (row["entity_id"], row["declaration_key"])
        revisions[key].append(row)
        payload = decode_declaration(row["payload_json"])
        _active_refs(payload, row["entity_id"], tables)
        if payload["kind"] == "ordering":
            _ordering_dependency(row, payload, tables)
        if payload["kind"] in {"booking", "position", "pool"}:
            _policy_references(payload, row["entity_id"], tables)
    for rows in revisions.values():
        ordered = sorted(rows, key=lambda row: row["valid_from"] or "")
        for previous, current in itertools.pairwise(ordered):
            require(
                previous["valid_to"] is not None
                and current["valid_from"] is not None
                and previous["valid_to"] <= current["valid_from"],
                "active_revision_overlap",
                "declarations",
            )


def _policy_references(payload: dict, entity: str, tables: Tables) -> None:
    if payload["kind"] == "position" and payload["booking_declaration_id"] is not None:
        booking = _resolve(
            {"table": "declarations", "key": [payload["booking_declaration_id"]]},
            entity,
            tables,
        )
        policy = decode_declaration(booking["payload_json"])
        require(
            policy["kind"] == "booking"
            and payload["position_id"] in policy["position_ids"],
            "position_booking_policy",
            "declarations",
        )
        require(
            (payload["measurement_kind"] == "quantity") == (policy["method"] == "NONE"),
            "booking_measurement",
            "declarations",
        )
    if payload["kind"] == "booking":
        for position_id in payload["position_ids"]:
            position = _resolve(
                {"table": "positions", "key": [position_id]}, entity, tables
            )
            require(
                (position["measurement_kind"] == "quantity")
                == (payload["method"] == "NONE"),
                "booking_measurement",
                "declarations",
            )
            require(
                (position["inventory_method"] == "pooled")
                == (payload["method"] == "AVERAGE"),
                "booking_pool",
                "declarations",
            )
        fee = _resolve(
            {
                "table": "declarations",
                "key": [payload["fee_attribution_declaration_id"]],
            },
            entity,
            tables,
        )
        require(
            fee["declaration_kind"] == "fee_attribution",
            "booking_fee_policy",
            "declarations",
        )
    if payload["kind"] == "pool":
        booking = _resolve(
            {"table": "declarations", "key": [payload["booking_declaration_id"]]},
            entity,
            tables,
        )
        policy = decode_declaration(booking["payload_json"])
        require(
            policy["kind"] == "booking" and policy["method"] == "AVERAGE",
            "pool_booking_policy",
            "declarations",
        )
        fee = _resolve(
            {
                "table": "declarations",
                "key": [policy["fee_attribution_declaration_id"]],
            },
            entity,
            tables,
        )
        require(
            decode_declaration(fee["payload_json"])["acquisition_treatment"]
            == payload["fee_treatment"],
            "pool_fee_policy",
            "declarations",
        )


def _custody(manifest: dict, tables: Tables) -> None:
    recovery = manifest["recovery_set"]
    _unique(recovery["vault_objects"], ("object_id",), "manifest.vault_objects")
    _unique(
        recovery["accession_records"], ("accession_id",), "manifest.accession_records"
    )
    objects = {r["object_id"] for r in recovery["vault_objects"]}
    bindings = {(r["entity_id"], r["source_scope_id"]) for r in manifest["bindings"]}
    _unique(manifest["bindings"], ("entity_id", "source_scope_id"), "manifest.bindings")
    for accession in recovery["accession_records"]:
        require(
            accession["object_id"] in objects,
            "accession_object",
            "manifest.accession_records",
        )
    for source in tables["source_records"]:
        require(
            source["source_blob_digest"] in objects
            and (source["entity_id"], source["source_scope_id"]) in bindings,
            "source_custody_reference",
            "source_records",
        )
    for symbol in tables["commodity_symbols"]:
        require(
            symbol["source_scope_id"] is None
            or (symbol["entity_id"], symbol["source_scope_id"]) in bindings,
            "symbol_scope_reference",
            "commodity_symbols",
        )
    for binding in manifest["bindings"]:
        _unique(
            binding["account_mappings"],
            ("source_section", "source_account_identifier", "source_position_key"),
            "manifest.bindings.account_mappings",
        )
        for mapping in binding["account_mappings"]:
            _resolve(
                {"table": "accounts", "key": [mapping["account_id"]]},
                binding["entity_id"],
                tables,
            )
            if mapping["position_id"] is not None:
                position = _resolve(
                    {"table": "positions", "key": [mapping["position_id"]]},
                    binding["entity_id"],
                    tables,
                )
                require(
                    position["account_id"] == mapping["account_id"],
                    "binding_position_account",
                    "manifest.bindings",
                )
    for link in tables["links"]:
        if link["member_kind"] == "blob":
            require(link["member_id"] in objects, "link_blob_reference", "links")


def validate_manifest(manifest: dict, tables: Tables) -> None:
    """Validate complete metadata references, required checks, and active inputs."""
    _validate(manifest, manifest_schema(), "manifest")
    validate_tables(tables)
    _unique(manifest["entities"], ("entity_id",), "manifest.entities")
    entities = {row["entity_id"] for row in manifest["entities"]}
    require(
        entities == {row["entity_id"] for row in tables["build"]},
        "manifest_entities",
        "manifest",
    )
    for row in tables["build"]:
        expected = next(
            r for r in manifest["entities"] if r["entity_id"] == row["entity_id"]
        )
        require(
            {k: v for k, v in row.items() if k != "manifest_digest"} == expected,
            "build_manifest_metadata",
            "build",
        )
    _unique(manifest["tables"], ("name",), "manifest.tables")
    require(
        {row["name"] for row in manifest["tables"]} == set(tables) - {"build"},
        "manifest_table_inventory",
        "manifest.tables",
    )
    for table in manifest["tables"]:
        require(
            table["path"] == f"{table['name']}.parquet"
            and table["row_count"] == len(tables[table["name"]]),
            "manifest_table_metadata",
            "manifest.tables",
        )
    payload_files = [
        *manifest["tables"],
        *manifest["artifacts"],
        *manifest["schemas"],
        *manifest["registries"],
        manifest["configuration"],
        manifest["derivation_definitions"],
    ]
    _unique(payload_files, ("path",), "manifest.payloads")
    require(
        all(row["path"] != "build.parquet" for row in payload_files),
        "manifest_checksum_cycle",
        "manifest.payloads",
    )
    _unique(manifest["schemas"], ("schema_id",), "manifest.schemas")
    _unique(manifest["registries"], ("registry_id",), "manifest.registries")
    schema_ids = {r["schema_id"] for r in manifest["schemas"]}
    for registry in manifest["registries"]:
        require(
            registry["schema_id"] in schema_ids,
            "registry_schema_reference",
            "manifest.registries",
        )
    _unique(manifest["checks"], ("entity_id", "check_id"), "manifest.checks")
    required = {
        check for output in manifest["outputs"] for check in output["required_checks"]
    }
    checks = {row["check_id"] for row in manifest["checks"]}
    require(required <= checks, "required_check_missing", "manifest.checks")
    for finding in manifest["checks"]:
        require(finding["entity_id"] in entities, "check_entity", "manifest.checks")
        _active_refs(finding["scope"], finding["entity_id"], tables)
        _active_refs(finding["evidence"], finding["entity_id"], tables)
        if finding["check_id"] in required:
            require(
                finding["status"] == "pass", "required_check_failed", "manifest.checks"
            )
        if finding["check_kind"] in {
            "schema",
            "identity",
            "arithmetic",
            "referential_integrity",
        }:
            require(
                finding["status"] == "pass"
                and finding["exception_declaration_id"] is None,
                "nonwaivable_check",
                "manifest.checks",
            )
    _active_declarations(manifest, tables)
    _custody(manifest, tables)


def validate_descriptor(descriptor: dict, manifest: dict) -> None:
    """Verify inventories; byte hashing and filesystem access belong to artifacts."""
    _validate(descriptor, descriptor_schema(), "descriptor")
    _unique(descriptor["payloads"], ("path",), "descriptor.payloads")
    paths = {row["path"] for row in descriptor["payloads"]}
    required = {row["path"] for row in manifest["tables"]}
    required.update(row["path"] for row in manifest["artifacts"])
    required.update(row["path"] for row in manifest["schemas"])
    required.update(row["path"] for row in manifest["registries"])
    required.update(
        {
            "build.parquet",
            manifest["configuration"]["path"],
            manifest["derivation_definitions"]["path"],
        }
    )
    require(paths == required, "descriptor_payload_inventory", "descriptor")
    require(
        descriptor["manifest_path"] not in paths,
        "manifest_checksum_cycle",
        "descriptor",
    )
    digests = {row["path"]: row["byte_digest"] for row in descriptor["payloads"]}
    for row in [
        *manifest["tables"],
        *manifest["artifacts"],
        *manifest["schemas"],
        *manifest["registries"],
        manifest["configuration"],
    ]:
        require(
            digests[row["path"]] == row["byte_digest"],
            "descriptor_payload_digest",
            "descriptor",
        )
