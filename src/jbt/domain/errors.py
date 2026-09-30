"""Structured invariant failures without financial payload disclosure."""

from dataclasses import dataclass


@dataclass(eq=False)
class DomainError(ValueError):
    """Identify a failed constraint without retaining source payloads."""

    constraint: str
    location: str
    references: tuple[str, ...] = ()

    def __str__(self) -> str:
        """Return only the constraint and its schema location."""
        return f"{self.location}: {self.constraint}"


class NumericSyntaxError(DomainError):
    """A numeric input is malformed or noncanonical."""


class NumericLimitError(DomainError):
    """An input or operation exceeds the admitted resource envelope."""


class ArithmeticDomainError(DomainError):
    """An arithmetic operation has no admitted mathematical meaning."""


class IdentityError(DomainError):
    """An identity or retained membership is invalid."""


class GuardError(DomainError):
    """A reviewed target is missing, retired, ambiguous, or changed."""


class TemporalError(DomainError):
    """A temporal observation or required resolution is invalid."""


class CanonicalError(DomainError):
    """A value cannot be encoded by the canonical value schema."""
