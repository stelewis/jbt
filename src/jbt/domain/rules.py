"""Typed predicates and field policies for exact rule evaluation."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import ExactDecimal


class Operator(StrEnum):
    """Closed, non-executable matching operations."""

    EQUALS = "equals"
    IN = "in"
    CONTAINS = "contains"


class RuleMode(StrEnum):
    """How competing assignments contribute to a field."""

    FIRST = "first"
    UNION = "union"
    PROHIBITED = "prohibited"


class RuleField(StrEnum):
    """The fields standing rules may enrich."""

    PAYEE = "transactions.payee"
    NARRATION = "transactions.narration"
    TAGS = "transactions.tags"
    REVIEW = "transactions.review_state"
    CATEGORY = "postings.category_id"

    @property
    def mode(self) -> RuleMode:
        """Tags accumulate; scalar assignments retain the first match."""
        return RuleMode.UNION if self == RuleField.TAGS else RuleMode.FIRST


class MatchType(StrEnum):
    """Semantic types, distinct from their JSON encodings."""

    TEXT = "string"
    KEY = "identifier"
    DATE = "date"
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    LIST = "list"


type MatchValue = (
    str | SemanticKey | date | ExactDecimal | int | bool | tuple[str, ...] | None
)


@dataclass(frozen=True, slots=True)
class Predicate:
    """A typed operand; membership wraps the field type, never changes it."""

    field: SemanticKey
    kind: MatchType
    operator: Operator
    operands: tuple[MatchValue, ...]

    def __post_init__(self) -> None:
        """Reject undecoded operands and unsupported operator/type pairs."""
        if not isinstance(self.kind, MatchType):
            message = "typed_rule_predicate_required"
            raise TypeError(message)
        expected = {
            MatchType.TEXT: str,
            MatchType.KEY: SemanticKey,
            MatchType.DATE: date,
            MatchType.DECIMAL: ExactDecimal,
            MatchType.INTEGER: int,
            MatchType.BOOLEAN: bool,
            MatchType.LIST: tuple,
        }[self.kind]
        if (
            not isinstance(self.field, SemanticKey)
            or not isinstance(self.kind, MatchType)
            or not isinstance(self.operator, Operator)
            or type(self.operands) is not tuple
            or not self.operands
            or (self.operator != Operator.IN and len(self.operands) != 1)
            or (
                self.operator == Operator.CONTAINS
                and (self.kind != MatchType.TEXT or self.operands[0] is None)
            )
            or any(
                value is not None
                and (
                    type(value) is not expected
                    or (
                        self.kind == MatchType.LIST
                        and isinstance(value, tuple)
                        and (
                            not all(type(item) is str for item in value)
                            or value != tuple(sorted(set(value)))
                        )
                    )
                )
                for value in self.operands
            )
        ):
            message = "typed_rule_predicate_required"
            raise TypeError(message)


def _equal(left: MatchValue, right: MatchValue) -> bool:
    if isinstance(left, ExactDecimal) and isinstance(right, ExactDecimal):
        return left.rational() == right.rational()
    return type(left) is type(right) and left == right


def matches(predicate: Predicate, observed: MatchValue) -> bool:
    """Compare exact text, civil dates and numeric values without coercion."""
    if predicate.operator == Operator.CONTAINS:
        operand = predicate.operands[0]
        return (
            isinstance(observed, str)
            and isinstance(operand, str)
            and operand in observed
        )
    return any(_equal(observed, operand) for operand in predicate.operands)
