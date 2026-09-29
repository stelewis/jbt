"""Dimensioned monetary components, with unknown distinct from known zero."""

from dataclasses import dataclass
from enum import StrEnum

from jbt.domain.errors import ArithmeticDomainError
from jbt.domain.ids import RecordId, RecordKind
from jbt.domain.numbers import ExactDecimal


@dataclass(frozen=True, slots=True)
class Money:
    """A known amount denominated in one entity-qualified commodity."""

    amount: ExactDecimal
    commodity: RecordId

    def __post_init__(self) -> None:
        """Require an exact amount and a commodity identity."""
        if not isinstance(self.amount, ExactDecimal):
            raise ArithmeticDomainError(
                constraint="exact_amount_required", location="money"
            )
        if not isinstance(self.commodity, RecordId):
            raise ArithmeticDomainError(
                constraint="commodity_required", location="money"
            )
        self.commodity.require(entity=self.commodity.entity, kind=RecordKind.COMMODITY)

    def add(self, other: Money) -> Money:
        """Conserve one dimension, rejecting cross-commodity/entity sums."""
        if self.commodity != other.commodity:
            raise ArithmeticDomainError(
                constraint="commodity_mismatch", location="money"
            )
        amount = self.amount.rational().add(other.amount.rational()).exact_decimal()
        return Money(amount, self.commodity)


class Knowledge(StrEnum):
    """Known, unknown, and inapplicable are different financial assertions."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    INAPPLICABLE = "inapplicable"


@dataclass(frozen=True, slots=True)
class MonetaryComponent:
    """A component whose explicit knowledge state controls its value."""

    state: Knowledge
    value: Money | None

    def __post_init__(self) -> None:
        """Permit a value if and only if the component is known."""
        if not isinstance(self.state, Knowledge):
            raise ArithmeticDomainError(
                constraint="knowledge_state_required", location="component"
            )
        if (self.state == Knowledge.KNOWN) != isinstance(self.value, Money):
            raise ArithmeticDomainError(
                constraint="knowledge_value_combination", location="component"
            )
        if self.state != Knowledge.KNOWN and self.value is not None:
            raise ArithmeticDomainError(
                constraint="absent_value_required", location="component"
            )
