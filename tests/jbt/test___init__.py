"""Package installation smoke test."""

from importlib.metadata import version

import jbt


def test_installed_package_is_importable() -> None:
    """The installed distribution exposes the jbt package."""
    assert jbt.__name__ == "jbt"
    assert version("jbt")
