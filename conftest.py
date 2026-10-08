"""Repository-wide pytest command-line configuration."""

from pathlib import Path

import pytest  # noqa: TC002 - Pluggy evaluates hook annotations at registration.


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register artifact selection before pytest resolves collection paths."""
    parser.addoption(
        "--release-wheel",
        type=Path,
        default=None,
        help="Use this final release wheel in the installed financial acceptance test.",
    )
