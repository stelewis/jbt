"""Schema-typed JSON row projection guards and explicit reviewed rebinding."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from jbt.domain.canonical import JsonValue, encode_json
from jbt.domain.errors import CanonicalError, GuardError, IdentityError
from jbt.domain.ids import EntityId, SemanticKey, validate_digest, validate_key

type FieldKind = Literal["string", "integer", "boolean", "date", "list"]
type FieldValue = str | int | bool | date | tuple[str, ...] | None
type LogicalKey = tuple[str | int | date, ...]


@dataclass(frozen=True, slots=True)
class GuardReference:
    """An entity-qualified table address, including composite logical keys."""

    entity: EntityId
    table: SemanticKey
    key: LogicalKey

    def __post_init__(self) -> None:
        """Reject untyped identity dimensions and mutable or boolean keys."""
        if (
            not isinstance(self.entity, EntityId)
            or not isinstance(self.table, SemanticKey)
            or type(self.key) is not tuple
            or any(type(part) not in {str, int, date} for part in self.key)
        ):
            raise GuardError(
                constraint="immutable_typed_reference_required",
                location="guard_reference",
            )


@dataclass(frozen=True, slots=True)
class GuardField:
    """One physical row field with its explicit schema type and nullability."""

    name: str
    kind: FieldKind
    nullable: bool
    value: FieldValue

    def __post_init__(self) -> None:
        """Keep nulls typed and prohibit coercion before row canonicalization."""
        try:
            validate_key(self.name, "guard_field.name")
        except IdentityError as error:
            raise GuardError(error.constraint, error.location) from error
        if (
            type(self.nullable) is not bool
            or not isinstance(self.kind, str)
            or self.kind not in {"string", "integer", "boolean", "date", "list"}
        ):
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
        try:
            encode_json(self.json_value(), integer_strings=True)
        except CanonicalError as error:
            raise GuardError(error.constraint, error.location) from error

    def json_value(self) -> JsonValue:
        """Convert only explicit date/list types for canonical physical rows."""
        if isinstance(self.value, date):
            return self.value.isoformat()
        if isinstance(self.value, tuple):
            return list(self.value)
        return self.value


@dataclass(frozen=True, slots=True)
class GuardTarget:
    """An addressable row with immutable explicit physical field definitions."""

    reference: GuardReference
    fields: tuple[GuardField, ...]
    active: bool

    def __post_init__(self) -> None:
        """Require unique schema fields and an explicit active/history state."""
        if not isinstance(self.reference, GuardReference) or not isinstance(
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
        try:
            validate_key(name, "guard.fields")
        except IdentityError as error:
            raise GuardError(error.constraint, error.location) from error
    if tuple(sorted(set(fields), key=lambda name: name.encode("utf-8"))) != fields:
        raise GuardError(constraint="sorted_unique_guard_fields", location="guard")
    if any(name in {"expected_digest", "guard_digest"} for name in fields):
        raise GuardError(constraint="self_referential_guard", location="guard")


@dataclass(frozen=True, slots=True)
class Guard:
    """A reviewed digest of exactly the named fields, including typed nulls."""

    reference: GuardReference
    fields: tuple[str, ...]
    expected_digest: str

    def __post_init__(self) -> None:
        """Require an explicit target, canonical fields, and review digest."""
        if not isinstance(self.reference, GuardReference):
            raise GuardError(constraint="typed_guard_required", location="guard")
        _validate_names(self.fields)
        try:
            validate_digest(self.expected_digest, "guard.expected_digest")
        except IdentityError as error:
            raise GuardError(error.constraint, error.location) from error


def projection_digest(target: GuardTarget, fields: tuple[str, ...]) -> str:
    """Hash the version-one schema header then its selected row JSON array.

    This is row canonicalization, not generated-identity framing: schema
    version and ordered field definitions precede one LF-terminated row;
    integer values become decimal strings, booleans/null keep JSON types.
    The enclosing guard, not the digest payload, names the target.
    """
    if not isinstance(target, GuardTarget):
        raise GuardError(
            constraint="immutable_typed_target_required", location="guard_target"
        )
    _validate_names(fields)
    available = {field.name: field for field in target.fields}
    if not set(fields) <= available.keys():
        raise GuardError(constraint="guard_field_missing", location="guard")
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


class GuardOutcome(StrEnum):
    """Closed applicability outcomes; malformed contracts are not outcomes."""

    APPLIED = "applied"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INACTIVE = "inactive"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class GuardEvaluation:
    """Retain reviewed lineage and readable selected observations."""

    guard: Guard
    outcome: GuardOutcome
    observed_fields: tuple[GuardField, ...]
    observed_digest: str | None

    def __post_init__(self) -> None:
        """Prevent contradictory outcomes or fabricated projection evidence."""
        if (
            not isinstance(self.guard, Guard)
            or not isinstance(self.outcome, GuardOutcome)
            or not isinstance(self.observed_fields, tuple)
            or any(not isinstance(field, GuardField) for field in self.observed_fields)
        ):
            raise GuardError(
                constraint="typed_evaluation_required", location="guard_evaluation"
            )
        names = tuple(field.name for field in self.observed_fields)
        if names != tuple(name for name in self.guard.fields if name in names):
            raise GuardError(
                constraint="selected_observations_required", location="guard_evaluation"
            )
        complete = names == self.guard.fields
        digest = (
            projection_digest(
                GuardTarget(self.reference, self.observed_fields, active=True), names
            )
            if complete
            else None
        )
        valid = self.observed_digest == digest
        match self.outcome:
            case GuardOutcome.MISSING | GuardOutcome.AMBIGUOUS:
                valid = valid and not self.observed_fields
            case GuardOutcome.APPLIED:
                valid = valid and complete and digest == self.guard.expected_digest
            case GuardOutcome.CHANGED:
                valid = valid and digest != self.guard.expected_digest
            case GuardOutcome.INACTIVE:
                pass
        if not valid:
            raise GuardError(
                constraint="consistent_evaluation_required", location="guard_evaluation"
            )

    @property
    def reference(self) -> GuardReference:
        """Identify the exact reviewed address, never a redirected target."""
        return self.guard.reference


def evaluate_guard(guard: Guard, targets: tuple[GuardTarget, ...]) -> GuardEvaluation:
    """Evaluate exact row applicability without redirecting inactive targets."""
    if (
        not isinstance(guard, Guard)
        or not isinstance(targets, tuple)
        or any(not isinstance(target, GuardTarget) for target in targets)
    ):
        raise GuardError(constraint="typed_guard_required", location="guard")
    matches = tuple(target for target in targets if target.reference == guard.reference)
    if not matches:
        return GuardEvaluation(guard, GuardOutcome.MISSING, (), None)
    if len(matches) > 1:
        return GuardEvaluation(guard, GuardOutcome.AMBIGUOUS, (), None)
    target = matches[0]
    available = {field.name: field for field in target.fields}
    observed = tuple(available[name] for name in guard.fields if name in available)
    digest = (
        projection_digest(target, guard.fields)
        if len(observed) == len(guard.fields)
        else None
    )
    if not target.active:
        outcome = GuardOutcome.INACTIVE
    elif digest != guard.expected_digest:
        outcome = GuardOutcome.CHANGED
    else:
        outcome = GuardOutcome.APPLIED
    return GuardEvaluation(guard, outcome, observed, digest)
