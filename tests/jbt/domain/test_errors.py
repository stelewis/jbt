import pytest

from jbt.domain.errors import (
    ArithmeticDomainError,
    CanonicalError,
    DomainError,
    GuardError,
    IdentityError,
    NumericLimitError,
    NumericSyntaxError,
    TemporalError,
)


@pytest.mark.parametrize(
    "error_type",
    [
        ArithmeticDomainError,
        CanonicalError,
        GuardError,
        IdentityError,
        NumericLimitError,
        NumericSyntaxError,
        TemporalError,
    ],
)
def test_named_errors_retain_structured_safe_context(
    error_type: type[DomainError],
) -> None:
    error = error_type("constraint", "schema.field", ("synthetic-record",))
    assert isinstance(error, ValueError)
    assert error.constraint == "constraint"
    assert error.location == "schema.field"
    assert error.references == ("synthetic-record",)
    assert str(error) == "schema.field: constraint"
    assert "synthetic-record" not in str(error)
