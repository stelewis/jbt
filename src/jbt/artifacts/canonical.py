"""Typed logical table digests independent of Parquet encoding."""

import hashlib
from typing import TYPE_CHECKING

from jbt.domain.canonical import encode_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def table_digest(
    columns: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
    *,
    schema_version: int,
) -> str:
    """Hash typed ordered rows, including nulls and duplicate multiplicity."""
    names: list[str] = []
    for column in columns:
        name = column["name"]
        if not isinstance(name, str):
            msg = "column names must be strings"
            raise TypeError(msg)
        names.append(name)
    digest = hashlib.sha256(
        encode_json(
            {"schema_version": schema_version, "columns": columns},
            integer_strings=True,
        ),
    )
    for row in rows:
        digest.update(encode_json([row[name] for name in names], integer_strings=True))
    return digest.hexdigest()
