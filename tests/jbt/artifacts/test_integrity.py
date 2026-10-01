from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import (
    ArtifactIntegrityError,
    IntegrityCode,
    byte_digest,
    decode_document,
    read_payload,
    require_digest,
    require_filename,
    require_inventory,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_known_sha256_vector() -> None:
    assert byte_digest(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":1.2}',
        b"[]",
        b"\xff",
        b"{",
        b'{"a":9223372036854775808}',
        b'{"a":-9223372036854775809}',
        b'{"a":' + b"9" * 5000 + b"}",
    ],
)
def test_invalid_json_document(payload: bytes) -> None:
    with pytest.raises(ArtifactIntegrityError) as error:
        decode_document(payload, "manifest.json")
    assert error.value.code is IntegrityCode.JSON


def test_json_preserves_types_without_coercion() -> None:
    assert decode_document(
        b'{"integer":1,"boolean":true,"coefficient":"100","missing":null}',
        "tabular_schema.json",
    ) == {"integer": 1, "boolean": True, "coefficient": "100", "missing": None}


@pytest.mark.parametrize("value", ["", "F" * 64, "a" * 63, "a" * 65])
def test_invalid_digest(value: str) -> None:
    with pytest.raises(ArtifactIntegrityError) as error:
        require_digest(value)
    assert error.value.code is IntegrityCode.DIGEST


@pytest.mark.parametrize(
    "filename",
    ["1.json", "UPPER.json", "\u00e9.json", "nested/a.json", "a.json\n"],
)
def test_filename_profile_is_identical_for_readers_and_writers(filename: str) -> None:
    with pytest.raises(ArtifactIntegrityError) as error:
        require_filename(filename)
    assert error.value.code is IntegrityCode.PATH


def test_pinned_read_and_inventory(tmp_path: Path) -> None:
    payload = b'{"schema_version":"1"}\n'
    (tmp_path / "manifest.json").write_bytes(payload)
    assert (
        read_payload(
            tmp_path,
            "manifest.json",
            byte_digest(payload),
            maximum_bytes=len(payload),
        )
        == payload
    )
    require_inventory(tmp_path, frozenset({"manifest.json"}))
    (tmp_path / "unexpected.json").write_text("{}")
    with pytest.raises(ArtifactIntegrityError) as error:
        require_inventory(tmp_path, frozenset({"manifest.json"}))
    assert error.value.code is IntegrityCode.INVENTORY


@pytest.mark.parametrize(
    "filename",
    ["../manifest.json", "/manifest.json", "nested/manifest.json", "file://a.json"],
)
def test_path_escape_is_rejected(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ArtifactIntegrityError) as error:
        read_payload(tmp_path, filename, "0" * 64, maximum_bytes=10)
    assert error.value.code is IntegrityCode.PATH
    assert filename not in str(error.value)


def test_digest_mismatch(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError) as error:
        read_payload(tmp_path, "manifest.json", "0" * 64, maximum_bytes=10)
    assert error.value.code is IntegrityCode.DIGEST


def test_size_and_missing_fail_distinctly(tmp_path: Path) -> None:
    with pytest.raises(ArtifactIntegrityError) as error:
        read_payload(tmp_path, "manifest.json", "0" * 64, maximum_bytes=10)
    assert error.value.code is IntegrityCode.MISSING
    (tmp_path / "manifest.json").write_bytes(b"12345")
    with pytest.raises(ArtifactIntegrityError) as error:
        read_payload(tmp_path, "manifest.json", byte_digest(b"12345"), maximum_bytes=4)
    assert error.value.code is IntegrityCode.SIZE


def test_symlink_even_with_correct_digest_fails(tmp_path: Path) -> None:
    (tmp_path / "target.json").write_bytes(b"{}")
    (tmp_path / "manifest.json").symlink_to(tmp_path / "target.json")
    with pytest.raises(ArtifactIntegrityError) as error:
        read_payload(tmp_path, "manifest.json", byte_digest(b"{}"), maximum_bytes=10)
    assert error.value.code is IntegrityCode.SYMLINK


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_read_limit(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="maximum_bytes"):
        read_payload(tmp_path, "manifest.json", "0" * 64, maximum_bytes=limit)
