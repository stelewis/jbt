from datetime import date

import pytest

from jbt.domain.ids import SemanticKey
from jbt.domain.numbers import ExactDecimal
from jbt.domain.rules import MatchType, MatchValue, Operator, Predicate, matches


@pytest.mark.parametrize(
    ("kind", "operator", "operands", "observed", "expected"),
    [
        (MatchType.TEXT, Operator.EQUALS, (" e\u0301 ",), " e\u0301 ", True),
        (MatchType.TEXT, Operator.EQUALS, (" e\u0301 ",), " é ", False),
        (MatchType.TEXT, Operator.CONTAINS, ("[.*]",), "literal [.*] text", True),
        (MatchType.TEXT, Operator.CONTAINS, ("[.*]",), "anything", False),
        (MatchType.TEXT, Operator.CONTAINS, ("",), None, False),
        (MatchType.TEXT, Operator.EQUALS, (None,), None, True),
        (MatchType.TEXT, Operator.IN, ("a", "b"), "b", True),
        (MatchType.KEY, Operator.IN, (SemanticKey("USD"),), SemanticKey("USD"), True),
        (MatchType.KEY, Operator.EQUALS, (SemanticKey("USD"),), "USD", False),
        (MatchType.DATE, Operator.IN, (date(2026, 1, 1),), date(2026, 1, 1), True),
        (
            MatchType.DECIMAL,
            Operator.IN,
            (ExactDecimal.parse("-5.00"), ExactDecimal.parse("0")),
            ExactDecimal.parse("-5"),
            True,
        ),
        (MatchType.INTEGER, Operator.EQUALS, (1,), True, False),
        (MatchType.BOOLEAN, Operator.EQUALS, (True,), True, True),
        (MatchType.LIST, Operator.IN, (("a", "b"), ("c",)), ("a", "b"), True),
    ],
)
def test_operator_uses_semantic_field_type(
    kind: MatchType,
    operator: Operator,
    operands: tuple[MatchValue, ...],
    observed: MatchValue,
    *,
    expected: bool,
) -> None:
    predicate = Predicate(SemanticKey("field"), kind, operator, operands)
    assert matches(predicate, observed) is expected


@pytest.mark.parametrize(
    ("kind", "operator", "operands"),
    [
        (MatchType.INTEGER, Operator.EQUALS, (True,)),
        (MatchType.TEXT, Operator.IN, ("text", 1)),
        (MatchType.DECIMAL, Operator.CONTAINS, (ExactDecimal.parse("1"),)),
        (MatchType.KEY, Operator.CONTAINS, (SemanticKey("USD"),)),
        (MatchType.DATE, Operator.EQUALS, ("2026-01-01",)),
        (MatchType.TEXT, Operator.IN, ()),
        (MatchType.TEXT, Operator.EQUALS, ("a", "b")),
        (MatchType.LIST, Operator.EQUALS, (("b", "a"),)),
    ],
)
def test_predicates_reject_undecoded_or_incompatible_operands(
    kind: MatchType, operator: Operator, operands: tuple[MatchValue, ...]
) -> None:
    with pytest.raises(TypeError, match="typed_rule_predicate_required"):
        Predicate(SemanticKey("field"), kind, operator, operands)
