"""Decode project inputs into the supported cash declaration boundary."""

# Exception arguments are stable codes, not message formatting.
# ruff: noqa: EM101

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from jsonschema import ValidationError

from jbt.artifacts.integrity import decode_document
from jbt.contracts.declarations import field_registry, validate_declaration
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.publication import validate_registries
from jbt.contracts.schemas import configuration_schema, validate_document
from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.cash import CategoryRule, MatchField, Predicate
from jbt.domain.identity import revision_id
from jbt.domain.ids import EntityId, SemanticKey


def document(value: object) -> dict[str, JsonValue]:
    """Require a JSON object with no unsupported Python values."""
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise ContractError("object_required", "project")
    return normalized


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | bool) or type(value) is int:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ContractError("json_value_required", "project")


def text(value: object, location: str) -> str:
    """Require nonempty text at an external input boundary."""
    if not isinstance(value, str) or not value:
        raise ContractError("text_required", location)
    return value


def objects(value: object, location: str) -> tuple[dict[str, JsonValue], ...]:
    """Require an array of closed documents before decoding its variants."""
    if not isinstance(value, list):
        raise ContractError("array_required", location)
    return tuple(document(item) for item in value)


class SourceRole(StrEnum):
    """Evidence selection, not an inferred transaction kind."""

    STATEMENT = "statement"
    OPENING = "opening_balance"


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """A selected acquisition and its reviewed source-account binding."""

    accession_id: str
    source_scope_id: str
    source_account_identifier: str
    account_id: str
    role: SourceRole


@dataclass(frozen=True, slots=True)
class Declaration:
    """An immutable authored revision; decoded payloads are boundary values."""

    key: str
    revision: str
    kind: str
    payload: bytes
    valid_from: date | None
    valid_to: date | None

    def decode(self) -> dict[str, JsonValue]:
        """Decode a fresh value without exposing shared mutable input."""
        return document(decode_document(self.payload, "declaration"))


@dataclass(frozen=True, slots=True)
class Project:
    """Captured project intent, independent of the working directory."""

    entity: EntityId
    sources: tuple[SourceSelection, ...]
    declarations: tuple[Declaration, ...]
    configuration: bytes
    registries: bytes

    def one(self, kind: str) -> Declaration:
        """Require one declaration for a singleton supported boundary."""
        selected = tuple(item for item in self.declarations if item.kind == kind)
        require(len(selected) == 1, "singleton_declaration_required", kind)
        return selected[0]

    def configuration_document(self) -> dict[str, JsonValue]:
        """Return the exact effective configuration as a fresh document."""
        return document(decode_document(self.configuration, "configuration"))

    def category_rules(self) -> tuple[CategoryRule, ...]:
        """Decode the existing rule language's supported equality subset."""
        rules: list[tuple[int, CategoryRule]] = []
        categories = {
            text(item.decode()["category_id"], "category")
            for item in self.declarations
            if item.kind == "category"
        }
        for item in self.declarations:
            if item.kind != "rule":
                continue
            payload = item.decode()
            require(not payload["split"], "rule_split_unsupported", item.key)
            assignments = objects(payload["assignments"], item.key)
            require(len(assignments) == 1, "rule_assignment_count", item.key)
            assignment = assignments[0]
            require(
                assignment["field"] == "postings.category_id",
                "rule_assignment_unsupported",
                item.key,
            )
            category = text(assignment["value"], item.key)
            require(category in categories, "rule_category_missing", item.key)
            predicates = []
            for predicate in objects(payload["matches"], item.key):
                require(
                    predicate["operator"] == "equals",
                    "rule_operator_unsupported",
                    item.key,
                )
                field = text(predicate["field"], item.key)
                require(field in MatchField, "rule_field_unsupported", item.key)
                predicates.append(
                    Predicate(MatchField(field), text(predicate["value"], item.key))
                )
            order = payload["order"]
            require(type(order) is int, "rule_order", item.key)
            if not isinstance(order, int):
                raise ContractError("rule_order", item.key)
            rules.append(
                (order, CategoryRule(item.revision, tuple(predicates), category))
            )
        require(
            len({order for order, _ in rules}) == len(rules),
            "rule_order_unique",
            "rules",
        )
        return tuple(rule for _, rule in sorted(rules, key=lambda item: item[0]))


def project_schema() -> dict:
    """Publish the execution input wrapper, without duplicating declarations."""
    identifier = {"type": "string", "minLength": 1}
    nullable_date = {"type": ["string", "null"], "format": "date"}

    def closed(properties: dict) -> dict:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    return closed(
        {
            "schema_version": {"const": 1},
            "entity_id": identifier,
            "sources": {
                "type": "array",
                "minItems": 2,
                "items": closed(
                    {
                        "accession_id": identifier,
                        "source_scope_id": identifier,
                        "source_account_identifier": identifier,
                        "account_id": identifier,
                        "role": {"enum": [role.value for role in SourceRole]},
                    }
                ),
            },
            "declarations": {
                "type": "array",
                "minItems": 1,
                "items": closed(
                    {
                        "key": identifier,
                        "valid_from": nullable_date,
                        "valid_to": nullable_date,
                        "payload": {"type": "object"},
                    }
                ),
            },
            "configuration": {"type": "object"},
            "registries": {"type": "object"},
        }
    )


def read_project(payload: bytes) -> Project:
    """Reject unsupported input before constructing deterministic operations."""
    raw = document(decode_document(payload, "project.json"))
    try:
        validate_document(raw, project_schema())
        validate_document(raw["configuration"], configuration_schema())
    except ValidationError as error:
        raise ContractError("project_schema", "project.json") from error
    entity = EntityId(text(raw["entity_id"], "entity_id"))
    authored_registries = document(raw["registries"])
    require(
        set(authored_registries)
        == {
            "schema_version",
            "observation_fields",
            "status_mappings",
            "source_sequences",
            "coverage_bases",
        },
        "project_registry_fields",
        "project.json",
    )
    registries = {**authored_registries, "field_registry": list(field_registry())}
    validate_registries(registries)
    declarations = []
    supported = {
        "account",
        "category",
        "commodity",
        "counterparty",
        "position",
        "assertion_scope",
        "authored_fact",
        "rule",
    }
    for item in objects(raw["declarations"], "declarations"):
        key = text(item["key"], "declaration.key")
        body = document(item["payload"])
        encoded = encode_json(body, integer_strings=False)
        validate_declaration(body)
        kind = text(body["kind"], key)
        require(kind in supported, "declaration_unsupported", key)
        start = (
            date.fromisoformat(text(item["valid_from"], key))
            if item["valid_from"] is not None
            else None
        )
        end = (
            date.fromisoformat(text(item["valid_to"], key))
            if item["valid_to"] is not None
            else None
        )
        require(end is None, "effective_policy_end_unsupported", key)
        revision = revision_id(
            entity,
            SemanticKey(key),
            effective_from=start,
            effective_to=end,
            payload=body,
        )
        declarations.append(Declaration(key, revision.value, kind, encoded, start, end))
    require(
        len({d.key for d in declarations}) == len(declarations),
        "declaration_key_unique",
        "declarations",
    )
    sources = tuple(
        SourceSelection(
            text(item["accession_id"], "source"),
            text(item["source_scope_id"], "source"),
            text(item["source_account_identifier"], "source"),
            text(item["account_id"], "source"),
            SourceRole(text(item["role"], "source")),
        )
        for item in objects(raw["sources"], "sources")
    )
    require(
        len({s.accession_id for s in sources}) == len(sources),
        "source_selection_unique",
        "sources",
    )
    require(
        sum(s.role == SourceRole.OPENING for s in sources) == 1,
        "opening_evidence_required",
        "sources",
    )
    configuration = document(raw["configuration"])
    require(
        configuration["source_authority"] == [],
        "source_authority_unsupported",
        "configuration",
    )
    require(
        configuration["declaration_source_order"] == ["project.json"],
        "declaration_source_order",
        "configuration",
    )
    result = Project(
        entity,
        sources,
        tuple(declarations),
        encode_json(configuration, integer_strings=False),
        encode_json(registries, integer_strings=False),
    )
    for kind in (
        "account",
        "position",
        "commodity",
        "assertion_scope",
        "authored_fact",
    ):
        result.one(kind)
    result.category_rules()
    return result
