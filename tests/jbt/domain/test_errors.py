from jbt.domain.errors import DomainError


def test_domain_error_retains_structured_safe_context() -> None:
    error = DomainError("constraint", "schema.field", ("synthetic-record",))
    assert isinstance(error, ValueError)
    assert error.constraint == "constraint"
    assert error.location == "schema.field"
    assert error.references == ("synthetic-record",)
    assert str(error) == "schema.field: constraint"
    assert "synthetic-record" not in str(error)
