from decimal import Decimal

import pytest

from jbt.core import ArtifactEnvelope

CONFIG_DIGEST = "sha256:" + ("a" * 64)
INPUT_DIGEST = "sha256:" + ("b" * 64)


def test_envelope_serialization_and_digest_are_deterministic() -> None:
    envelope = ArtifactEnvelope(
        kind="extract",
        schema_version=1,
        producer="jbt 0.1.0a1",
        config_digest=CONFIG_DIGEST,
        inputs=(INPUT_DIGEST,),
        payload={"amount": Decimal("10.00"), "description": "Example"},
    )

    assert envelope.to_bytes() == (
        b'{"config_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"inputs":["sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],'
        b'"kind":"extract","payload":{"amount":10,"description":"Example"},'
        b'"producer":"jbt 0.1.0a1","schema_version":1}\n'
    )
    assert envelope.digest() == (
        "sha256:2b307e32ee8881a4efbc12ad3f9980fcd9adb3caf7242acdc7d28f4d213b74c4"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 0}, "schema_version"),
        ({"config_digest": "sha256:not-a-digest"}, "config_digest"),
        ({"inputs": (INPUT_DIGEST, CONFIG_DIGEST)}, "sorted"),
        ({"inputs": (INPUT_DIGEST, INPUT_DIGEST)}, "duplicates"),
    ],
)
def test_envelope_rejects_invalid_metadata(
    changes: dict[str, object], message: str
) -> None:
    values = {
        "kind": "extract",
        "schema_version": 1,
        "producer": "jbt 0.1.0a1",
        "config_digest": CONFIG_DIGEST,
        "inputs": (INPUT_DIGEST,),
        "payload": {},
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ArtifactEnvelope(**values)  # type: ignore[arg-type]