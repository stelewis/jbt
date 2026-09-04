from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import pytest

from jbt.core import CanonicalizationError, canonical_bytes, sha256_digest
from jbt.core.serde import CanonicalValue


def test_canonical_bytes_normalizes_keys_text_decimals_and_utc() -> None:
    value = {
        "z": Decimal("84.2000"),
        "e\N{COMBINING ACUTE ACCENT}": "Cafe\N{COMBINING ACUTE ACCENT}",
        "at": datetime(2026, 9, 4, 12, 30, tzinfo=timezone(timedelta(hours=2))),
    }

    assert canonical_bytes(value) == (
        b'{"at":"2026-09-04T10:30:00Z","z":84.2,"\xc3\xa9":"Caf\xc3\xa9"}\n'
    )


@pytest.mark.parametrize("value", [0.0, float("nan"), float("inf")])
def test_canonical_bytes_rejects_all_floats(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="floating-point"):
        canonical_bytes(cast(CanonicalValue, value))


def test_canonical_bytes_rejects_naive_datetime() -> None:
    with pytest.raises(CanonicalizationError, match="UTC offset"):
        canonical_bytes(datetime(2026, 9, 4))


def test_canonical_bytes_rejects_normalized_key_collision() -> None:
    with pytest.raises(CanonicalizationError, match="collide"):
        canonical_bytes(
            {
                "\N{LATIN SMALL LETTER E WITH ACUTE}": 1,
                "e\N{COMBINING ACUTE ACCENT}": 2,
            }
        )


def test_sha256_digest_is_qualified_and_stable() -> None:
    assert sha256_digest(b"jbt\n") == (
        "sha256:71f06d26f2dd9511a87b5664f8447ea7c8a8e0a87a765d144e6c2138fa47eedd"
    )