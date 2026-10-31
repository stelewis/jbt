"""Decode captured project intent and effective declaration revisions."""

from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from jsonschema import ValidationError

from jbt.artifacts.integrity import decode_document
from jbt.contracts.declarations import (
    field_registry,
    validate_declaration,
    validate_rule_matches,
)
from jbt.contracts.primitives import ContractError, require
from jbt.contracts.publication import validate_registries
from jbt.contracts.schemas import configuration_schema, validate_document
from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.identity import revision_id
from jbt.domain.ids import EntityId, SemanticKey


def document(value: object) -> dict[str, JsonValue]:
    """Require a JSON object with no unsupported Python values."""
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        code = "object_required"
        raise ContractError(code, "project")
    return normalized


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | bool) or type(value) is int:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    code = "json_value_required"
    raise ContractError(code, "project")


def text(value: object, location: str) -> str:
    """Require nonempty text at an external input boundary."""
    if not isinstance(value, str) or not value:
        code = "text_required"
        raise ContractError(code, location)
    return value


def objects(value: object, location: str) -> tuple[dict[str, JsonValue], ...]:
    """Require an array of closed documents before decoding its variants."""
    if not isinstance(value, list):
        code = "array_required"
        raise ContractError(code, location)
    return tuple(document(item) for item in value)


def strings(value: object, location: str) -> tuple[str, ...]:
    """Require an array of nonempty strings, not an iterable scalar."""
    if not isinstance(value, list):
        code = "array_required"
        raise ContractError(code, location)
    return tuple(text(item, location) for item in value)


def nullable_text(value: object, location: str) -> str | None:
    """Preserve explicit absence when decoding optional textual facts."""
    return None if value is None else text(value, location)


def integer(value: object, location: str) -> int:
    """Reject booleans and coercion for exact counts and precision."""
    if type(value) is not int:
        code = "integer_required"
        raise ContractError(code, location)
    return value


def nullable_integer(value: object, location: str) -> int | None:
    """Keep absent precision distinct from an integer zero."""
    return None if value is None else integer(value, location)


def boolean(value: object, location: str) -> bool:
    """Require a stated boolean rather than interpreting truthiness."""
    if not isinstance(value, bool):
        code = "boolean_required"
        raise ContractError(code, location)
    return value


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Selection names custody; accession receipts own scope and importer."""

    accession_id: str


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

    def applies(self, when: date) -> bool:
        """Apply the declaration's half-open effective interval."""
        return (self.valid_from is None or self.valid_from <= when) and (
            self.valid_to is None or when < self.valid_to
        )


@dataclass(frozen=True, slots=True)
class Project:
    """Captured project intent, independent of the working directory."""

    entity: EntityId
    sources: tuple[SourceSelection, ...]
    documents: tuple[SourceSelection, ...]
    declarations: tuple[Declaration, ...]
    configuration: bytes
    registries: bytes

    def configuration_document(self) -> dict[str, JsonValue]:
        """Return the exact effective configuration as a fresh document."""
        return document(decode_document(self.configuration, "configuration"))


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
                "items": identifier,
                "uniqueItems": True,
            },
            "documents": {
                "type": "array",
                "items": identifier,
                "uniqueItems": True,
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
        code = "project_schema"
        raise ContractError(code, "project.json") from error
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
        if kind == "rule":
            validate_rule_matches(
                body,
                objects(
                    authored_registries["observation_fields"], "observation_fields"
                ),
            )
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
        require(
            start is None or end is None or start < end,
            "effective_policy_interval",
            key,
        )
        revision = revision_id(
            entity,
            SemanticKey(key),
            effective_from=start,
            effective_to=end,
            payload=body,
        )
        declarations.append(Declaration(key, revision.value, kind, encoded, start, end))
    for key in {item.key for item in declarations}:
        revisions = sorted(
            (item for item in declarations if item.key == key),
            key=lambda item: item.valid_from or date.min,
        )
        for previous, current in pairwise(revisions):
            require(
                previous.kind == current.kind
                and previous.valid_to is not None
                and current.valid_from is not None
                and previous.valid_to <= current.valid_from,
                "declaration_revision_overlap",
                key,
            )
        if revisions[0].kind in {
            "account",
            "position",
            "commodity",
            "category",
            "assertion_scope",
        }:
            stable = {
                "account": ("account_id",),
                "position": ("position_id", "account_id", "commodity_id"),
                "commodity": ("commodity_id",),
                "category": ("category_id",),
                "assertion_scope": ("scope_id", "account_id"),
            }[revisions[0].kind]
            require(
                len(
                    {
                        tuple(item.decode()[field] for field in stable)
                        for item in revisions
                    }
                )
                == 1,
                "declaration_identity_changed",
                key,
            )
    sources = tuple(
        SourceSelection(item) for item in strings(raw["sources"], "sources")
    )
    require(
        len({s.accession_id for s in sources}) == len(sources),
        "source_selection_unique",
        "sources",
    )
    configuration = document(raw["configuration"])
    require(
        configuration["declaration_source_order"] == ["project.json"],
        "declaration_source_order",
        "configuration",
    )
    return Project(
        entity,
        sources,
        tuple(SourceSelection(item) for item in strings(raw["documents"], "documents")),
        tuple(declarations),
        encode_json(configuration, integer_strings=False),
        encode_json(registries, integer_strings=False),
    )
