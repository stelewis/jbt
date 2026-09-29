"""Schema-typed JSON row projection guards and explicit reviewed rebinding."""

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Literal

from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.errors import GuardError
from jbt.domain.ids import RecordId, validate_digest, validate_key

type FieldKind = Literal["string", "integer", "boolean", "date", "list"]
type FieldValue = str | int | bool | date | tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class GuardField:
    """One physical row field with its explicit schema type and nullability."""

    name: str
    kind: FieldKind
    nullable: bool
    value: FieldValue

    def __post_init__(self) -> None:
        """Keep nulls typed and prohibit coercion before row canonicalization."""
        validate_key(self.name, "guard_field.name")
        if type(self.nullable) is not bool or self.kind not in {
            "string",
            "integer",
            "boolean",
            "date",
            "list",
        }:
            raise GuardError(constraint="field_schema_required", location="guard_field")
        if self.value is None:
            if not self.nullable:
                raise GuardError(
                    constraint="nonnull_field_required", location="guard_field"
                )
            return
        self._validate_value()

    def _validate_value(self) -> None:
        valid = False
        match self.kind:
            case "string":
                valid = isinstance(self.value, str)
            case "integer":
                valid = type(self.value) is int
            case "boolean":
                valid = type(self.value) is bool
            case "date":
                valid = type(self.value) is date
            case "list":
                valid = isinstance(self.value, tuple) and all(
                    isinstance(item, str) for item in self.value
                )
        if not valid:
            raise GuardError(
                constraint="field_value_type_mismatch", location="guard_field"
            )
        encode_json(self.json_value(), integer_strings=True)

    def json_value(self) -> JsonValue:
        """Convert only explicit date/list types for canonical physical rows."""
        if isinstance(self.value, date):
            return self.value.isoformat()
        if isinstance(self.value, tuple):
            return list(self.value)
        return self.value


@dataclass(frozen=True, slots=True)
class GuardTarget:
    """An addressable record with immutable explicit physical field definitions."""

    identifier: RecordId
    fields: tuple[GuardField, ...]
    active: bool

    def __post_init__(self) -> None:
        """Require unique schema fields and an explicit active/history state."""
        if not isinstance(self.identifier, RecordId) or not isinstance(
            self.fields, tuple
        ):
            raise GuardError(
                constraint="immutable_typed_target_required", location="guard_target"
            )
        if any(not isinstance(field, GuardField) for field in self.fields):
            raise GuardError(
                constraint="field_schema_required", location="guard_target"
            )
        if (
            len({field.name for field in self.fields}) != len(self.fields)
            or type(self.active) is not bool
        ):
            raise GuardError(
                constraint="unique_fields_and_explicit_state", location="guard_target"
            )


def _validate_names(fields: tuple[str, ...]) -> None:
    if not isinstance(fields, tuple) or not fields:
        raise GuardError(constraint="sorted_unique_guard_fields", location="guard")
    for name in fields:
        validate_key(name, "guard.fields")
    if tuple(sorted(set(fields), key=lambda name: name.encode("utf-8"))) != fields:
        raise GuardError(constraint="sorted_unique_guard_fields", location="guard")


@dataclass(frozen=True, slots=True)
class Guard:
    """A reviewed digest of exactly the named fields, including typed nulls."""

    target: RecordId
    fields: tuple[str, ...]
    expected_digest: str

    def __post_init__(self) -> None:
        """Require an explicit target, canonical fields, and review digest."""
        if not isinstance(self.target, RecordId):
            raise GuardError(constraint="typed_guard_required", location="guard")
        _validate_names(self.fields)
        validate_digest(self.expected_digest, "guard.expected_digest")


def projection_digest(target: GuardTarget, fields: tuple[str, ...]) -> str:
    """Hash the version-one schema header then its selected row JSON array.

    This is row canonicalization, not generated-identity framing: schema
    version and ordered field definitions precede one LF-terminated row;
    integer values become decimal strings, booleans/null keep JSON types.
    The enclosing guard, not the digest payload, names the target.
    """
    _validate_names(fields)
    available = {field.name: field for field in target.fields}
    if not set(fields) <= available.keys():
        raise GuardError(constraint="guard_field_missing", location="guard")
    if any(name in {"expected_digest", "guard_digest"} for name in fields):
        raise GuardError(constraint="self_referential_guard", location="guard")
    selected = tuple(available[name] for name in fields)
    columns = [
        {"name": field.name, "kind": field.kind, "nullable": field.nullable}
        for field in selected
    ]
    digest = sha256(
        encode_json({"schema_version": 1, "columns": columns}, integer_strings=True)
    )
    digest.update(
        encode_json([field.json_value() for field in selected], integer_strings=True)
    )
    return digest.hexdigest()


def validate_guard(guard: Guard, targets: tuple[GuardTarget, ...]) -> GuardTarget:
    """Resolve one active, unchanged target; never follow retired references."""
    matches = tuple(target for target in targets if target.identifier == guard.target)
    if len(matches) != 1:
        raise GuardError(
            constraint="unique_guard_target_required",
            location="guard",
            references=(guard.target.value,),
        )
    target = matches[0]
    if not target.active:
        raise GuardError(
            constraint="active_guard_target_required",
            location="guard",
            references=(guard.target.value,),
        )
    if projection_digest(target, guard.fields) != guard.expected_digest:
        raise GuardError(
            constraint="guard_projection_changed",
            location="guard",
            references=(guard.target.value,),
        )
    return target


def validate_rebinding(
    previous: Guard, replacement: Guard, targets: tuple[GuardTarget, ...]
) -> GuardTarget:
    """Check historical reviewed meaning and a separately reviewed new target."""
    historical = tuple(
        target for target in targets if target.identifier == previous.target
    )
    if len(historical) != 1:
        raise GuardError(
            constraint="unique_historical_target_required", location="guard_rebinding"
        )
    if projection_digest(historical[0], previous.fields) != previous.expected_digest:
        raise GuardError(
            constraint="historical_guard_changed", location="guard_rebinding"
        )
    if previous.target.entity != replacement.target.entity:
        raise GuardError(
            constraint="cross_entity_rebinding", location="guard_rebinding"
        )
    if previous.target.kind != replacement.target.kind:
        raise GuardError(constraint="cross_kind_rebinding", location="guard_rebinding")
    return validate_guard(replacement, targets)
