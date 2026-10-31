import pytest


@pytest.fixture(autouse=True)
def _release_importer(installed_ofx_release: None) -> None:
    """Runtime tests use the explicitly synthetic installed entry point."""
