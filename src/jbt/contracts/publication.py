"""Cross-document validation for retained metadata and active decisions."""

from collections import defaultdict
from collections.abc import Mapping
from hashlib import sha256

from jsonschema import ValidationError

from jbt.contracts.catalog import tabular_schema
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
from jbt.contracts.validation import validate_link_members, validate_tables
from jbt.domain.canonical import encode_json

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
        _resolve(entry["scope"], entry["entity_id"], tables)
        action = entry["fact_kind"] == "action"
        require(
            entry["scope"]["table"] == ("commodities" if action else "accounts"),
            "authority_scope",
            "configuration.source_authority",
        )
        require(
            (entry["coverage_basis_id"] is None) == action,
            "authority_basis",
            "configuration.source_authority",
        )
        require(
            entry["period_start"] <= entry["period_end"],
            "authority_period",
            "configuration.source_authority",
        )
        key = (
            entry["entity_id"],
            entry["scope"]["table"],
            tuple(entry["scope"]["key"]),
            entry["fact_kind"],
            entry["coverage_basis_id"],
        )
        for previous in authority[key]:
            overlap = (
                entry["period_start"] <= previous["period_end"]
                and previous["period_start"] <= entry["period_end"]
            )
            require(
                not overlap,
                "authority_conflict",
                "configuration.source_authority",
            )
        authority[key].append(entry)
        for source in entry["authoritative_sources"]:
            require(
                source["table"] in {"statements", "source_records"},
                "authority_source",
                "configuration.source_authority",
            )
            _resolve(source, entry["entity_id"], tables)
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


def validate_extraction(extract: dict) -> None:
    """Validate source records without promoting status tokens to event state."""
    _validate(extract, extraction_schema(), "extraction")
    _unique(
        extract["records"],
        ("source_record_id", "event_key", "kind"),
        "extraction.records",
    )
    scope = extract["source_scope"]
    if scope["source_class"] == "statement":
        require(
            scope["period_start"] is not None and scope["period_end"] is not None,
            "statement_period",
            "extraction.source_scope",
        )
    if scope["period_start"] is not None and scope["period_end"] is not None:
        require(
            scope["period_start"] <= scope["period_end"],
            "statement_period",
            "extraction.source_scope",
        )
    for record in extract["records"]:
        payload = record["payload"]
        if record["kind"] == "transaction":
            _unique(payload["legs"], ("posting_key",), "extraction.legs")
            _unique(payload["times"], ("date_role",), "extraction.times")
        if record["kind"] == "observation":
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


def _active_guard(guard: dict, entity: str, tables: Tables) -> None:
    target = guard["target"]
    row = _resolve(target, entity, tables)
    specs = {table["name"]: table for table in tabular_schema()["tables"]}
    columns = {column["name"]: column for column in specs[target["table"]]["columns"]}
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
    selected = [
        {name: columns[field][name] for name in ("name", "kind", "nullable")}
        for field in fields
    ]
    values = [row[field] for field in fields]
    digest = sha256(
        encode_json(
            {"schema_version": 1, "columns": selected},
            integer_strings=True,
        )
    )
    digest.update(encode_json(values, integer_strings=True))
    require(
        digest.hexdigest() == guard["expected_digest"],
        "guard_projection_changed",
        "declarations",
    )


def _correction_journal(tables: Tables) -> dict:
    journal = defaultdict(list)
    corrections = {}
    for row in tables["declarations"]:
        payload = decode_declaration(row["payload_json"])
        if payload["kind"] == "correction":
            journal[row["entity_id"]].append(payload["journal_sequence"])
            corrections[(row["entity_id"], row["declaration_id"])] = payload
    for sequence in journal.values():
        require(
            sorted(sequence) == list(range(len(sequence))),
            "journal_sequence",
            "declarations",
        )
    for (entity, _), payload in corrections.items():
        prior_id = payload["prior_declaration_id"]
        if prior_id is None:
            continue
        prior = corrections.get((entity, prior_id))
        if prior is None:
            code = "correction_prior_reference"
            raise ContractError(code, "declarations")
        require(
            prior["journal_sequence"] < payload["journal_sequence"],
            "correction_prior_order",
            "declarations",
        )
        if payload["operation"] == "supersede":
            require(
                prior["target"] == payload["target"]
                and prior["operation"] != "retract",
                "correction_replacement_target",
                "declarations",
            )
    return corrections


def _identity_membership(
    row: dict, payload: dict, tables: Tables, memberships: set
) -> None:
    for member in payload["members"]:
        _resolve(
            {"table": "source_records", "key": [member["source_record_id"]]},
            row["entity_id"],
            tables,
        )
        key = (row["entity_id"], member["source_record_id"], member["event_key"])
        require(key not in memberships, "identity_membership_disjoint", "declarations")
        memberships.add(key)
    for retired_id in payload["retired_ids"]:
        identity = _resolve(
            {
                "table": "economic_identities",
                "key": [payload["record_kind"], retired_id],
            },
            row["entity_id"],
            tables,
        )
        require(
            identity["identity_state"] == "retired"
            and identity["decision_declaration_id"] == row["declaration_id"],
            "identity_retirement_decision",
            "declarations",
        )
        if identity["anchor_kind"] == "source_record":
            require(
                {
                    "source_record_id": identity["anchor_id"],
                    "event_key": identity["event_key"],
                }
                in payload["members"],
                "identity_retired_membership",
                "declarations",
            )
    require(
        len(payload["successor_ids"]) <= 1
        and (not payload["retired_ids"] or bool(payload["successor_ids"])),
        "identity_survivor_unambiguous",
        "declarations",
    )
    for survivor_id in payload["successor_ids"]:
        identity = _resolve(
            {
                "table": "economic_identities",
                "key": [payload["record_kind"], survivor_id],
            },
            row["entity_id"],
            tables,
        )
        require(
            identity["identity_state"] == "active"
            and identity["event_key"] == payload["event_key"]
            and identity["anchor_kind"]
            == (
                "source_record"
                if payload["anchor"]["kind"] == "source_record"
                else "declaration"
            )
            and identity["anchor_id"] == payload["anchor"]["id"],
            "identity_successor_active",
            "declarations",
        )


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
    corrections = _correction_journal(tables)
    keys = set()
    memberships = set()
    active_corrections = set()
    for selected in effective:
        row = _resolve(
            {"table": "declarations", "key": [selected["declaration_id"]]},
            selected["entity_id"],
            tables,
        )
        key = (row["entity_id"], row["declaration_key"])
        require(key not in keys, "active_revision_unique", "declarations")
        keys.add(key)
        payload = decode_declaration(row["payload_json"])
        _active_refs(payload, row["entity_id"], tables)
        if payload["kind"] == "correction":
            active_corrections.add((row["entity_id"], row["declaration_id"]))
        if payload["kind"] == "identity":
            _identity_membership(row, payload, tables, memberships)
        if payload["kind"] == "link":
            validate_link_members(
                payload["link_kind"],
                payload["members"],
                payload["terms_declaration_id"],
            )
        if payload["kind"] == "ordering":
            _ordering_dependency(row, payload, tables)
        if payload["kind"] in {"booking", "position", "pool"}:
            _policy_references(payload, row["entity_id"], tables)
    for entity, correction_id in active_corrections:
        prior_id = corrections[(entity, correction_id)]["prior_declaration_id"]
        require(
            prior_id is None or (entity, prior_id) not in active_corrections,
            "active_correction_chain",
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
