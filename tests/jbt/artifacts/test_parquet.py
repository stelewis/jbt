from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from jbt.artifacts.parquet import (
    Compression,
    WriterSettings,
    arrow_schema,
    write_table,
)
from jbt.contracts.catalog import catalog

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def settings() -> WriterSettings:
    return WriterSettings(
        compression=Compression.ZSTD,
        row_group_size=100,
        dictionary=False,
        statistics=True,
    )


def test_every_empty_table_has_its_declared_physical_schema(
    tmp_path: Path,
    settings: WriterSettings,
) -> None:
    for table in catalog():
        path = tmp_path / f"{table.name}.parquet"
        write_table(path, table, [], settings)
        actual = pq.read_table(path)
        assert actual.num_rows == 0
        assert actual.schema.equals(arrow_schema(table), check_metadata=True)
        for field in actual.schema:
            assert not pa.types.is_floating(field.type)
            assert not pa.types.is_null(field.type)


def test_build_dates_are_physical_dates(
    tmp_path: Path,
    settings: WriterSettings,
) -> None:
    table = next(table for table in catalog() if table.name == "build")
    row = {
        "entity_id": "E",
        "manifest_digest": "a" * 64,
        "producer_version": "0.1.0a1",
        "schema_version": 1,
        "schema_digest": "b" * 64,
        "as_of": "2026-01-31",
        "execution_fingerprint": "c" * 64,
    }
    path = tmp_path / "build.parquet"
    write_table(path, table, [row], settings)
    actual = pq.read_table(path)
    assert actual.schema.field("as_of").type == pa.date32()
    assert str(actual.to_pylist()[0]["as_of"]) == "2026-01-31"
    assert actual.to_pylist()[0]["schema_version"] == 1


def test_descriptive_unicode_is_preserved_in_physical_strings(
    tmp_path: Path,
    settings: WriterSettings,
) -> None:
    table = next(table for table in catalog() if table.name == "counterparties")
    rows = [
        {
            "entity_id": "E",
            "counterparty_id": key,
            "name": name,
            "declaration_id": key,
        }
        for key, name in (("composed", " é\n\x00 "), ("decomposed", " e\u0301\n\x00 "))
    ]
    path = tmp_path / "counterparties.parquet"
    write_table(path, table, rows, settings)
    assert pq.read_table(path).to_pylist() == rows


@pytest.mark.parametrize("size", [0, -1, 1_000_001])
def test_invalid_row_group_size(size: int) -> None:
    with pytest.raises(ValueError, match="row_group_size"):
        WriterSettings(
            compression=Compression.NONE,
            row_group_size=size,
            dictionary=False,
            statistics=False,
        )


def test_boolean_is_not_row_group_size() -> None:
    with pytest.raises(TypeError, match="integer"):
        WriterSettings(
            compression=Compression.NONE,
            row_group_size=True,
            dictionary=False,
            statistics=False,
        )
