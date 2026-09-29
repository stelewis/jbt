import pytest

from jbt.domain.errors import ArithmeticDomainError, IdentityError
from jbt.domain.ids import EntityId, RecordId, RecordKind
from jbt.domain.money import Knowledge, MonetaryComponent, Money
from jbt.domain.numbers import ExactDecimal


def test_money_conserves_dimension_and_exact_total() -> None:
    usd = RecordId(EntityId("entity"), RecordKind.COMMODITY, "a" * 64)
    left = Money(ExactDecimal.parse("1.20"), usd)
    right = Money(ExactDecimal.parse("0.03"), usd)
    assert left.add(right) == Money(ExactDecimal("123", 2, None), usd)
    other = Money(
        right.amount, RecordId(EntityId("entity"), RecordKind.COMMODITY, "b" * 64)
    )
    with pytest.raises(ArithmeticDomainError, match="commodity_mismatch"):
        left.add(other)
    with pytest.raises(IdentityError, match="reference_kind_or_entity"):
        Money(right.amount, RecordId(EntityId("entity"), RecordKind.ACCOUNT, "a" * 64))


def test_unknown_is_not_zero_or_inapplicable() -> None:
    zero = Money(
        ExactDecimal("0", 0, None),
        RecordId(EntityId("entity"), RecordKind.COMMODITY, "a" * 64),
    )
    assert MonetaryComponent(Knowledge.UNKNOWN, None) != MonetaryComponent(
        Knowledge.INAPPLICABLE, None
    )
    assert MonetaryComponent(Knowledge.UNKNOWN, None) != MonetaryComponent(
        Knowledge.KNOWN, zero
    )
    with pytest.raises(ArithmeticDomainError, match="knowledge_value_combination"):
        MonetaryComponent(Knowledge.KNOWN, None)
    with pytest.raises(ArithmeticDomainError, match="knowledge_value_combination"):
        MonetaryComponent(Knowledge.UNKNOWN, zero)
