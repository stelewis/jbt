"""Explicit Parquet physical types, including empty relations."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from jbt.contracts.catalog import Table

MAX_ROW_GROUP_SIZE = 1_000_000


class Compression(StrEnum):
    """Codecs supported by the publication profile."""

    NONE = "NONE"
    ZSTD = "zstd"


@dataclass(frozen=True)
class WriterSettings:
    """Execution inputs, not financial-content identity."""

    compression: Compression
    row_group_size: int
    dictionary: bool
    statistics: bool

    def __post_init__(self) -> None:
        """Reject implicit casts and unbounded row-group configuration."""
        if not isinstance(self.compression, Compression):
            msg = "compression must be a supported Compression"
            raise TypeError(msg)
        if type(self.row_group_size) is not int:
            msg = "row_group_size must be an integer"
            raise TypeError(msg)
        if not 1 <= self.row_group_size <= MAX_ROW_GROUP_SIZE:
            msg = "row_group_size must be between 1 and 1000000"
            raise ValueError(msg)
        if type(self.dictionary) is not bool or type(self.statistics) is not bool:
            msg = "dictionary and statistics must be booleans"
            raise TypeError(msg)


def arrow_schema(table: Table) -> pa.Schema:
    """Expand the catalog without inferring types from observed rows."""
    types = {
        "string": pa.string(),
        "integer": pa.int64(),
        "boolean": pa.bool_(),
        "date": pa.date32(),
        "list": pa.list_(pa.field("element", pa.string(), nullable=False)),
    }
    return pa.schema(
        [
            pa.field(column.name, types[column.kind], nullable=column.nullable)
            for column in table.columns
        ],
    )


def write_table(
    path: Path,
    table: Table,
    rows: Sequence[Mapping[str, object]],
    settings: WriterSettings,
) -> None:
    """Write already validated rows; date conversion belongs at this edge."""
    encoded: list[dict[str, object]] = []
    for row in rows:
        converted = dict(row)
        for column in table.columns:
            value = converted[column.name]
            if column.kind == "date" and value is not None:
                if not isinstance(value, str):
                    msg = f"{table.name}.{column.name} requires an ISO date string"
                    raise TypeError(msg)
                converted[column.name] = date.fromisoformat(value)
        encoded.append(converted)
    relation = pa.Table.from_pylist(encoded, schema=arrow_schema(table))
    pq.write_table(
        relation,
        path,
        compression=settings.compression.value,
        row_group_size=settings.row_group_size,
        use_dictionary=settings.dictionary,
        write_statistics=settings.statistics,
        version="2.6",
        store_schema=True,
    )
