"""Cross-domain corpus selection; financial answers remain authored JSON."""

from tests.integration.step_0.corpus import FixtureCase, load_case

FAMILIES = ("options", "claims", "obligations", "perpetual", "onchain")


def family_cases() -> tuple[FixtureCase, ...]:
    return tuple(load_case("breadth/" + family) for family in FAMILIES)
