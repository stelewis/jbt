"""Single catalog for physical writers and logical boundary validation."""

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

type ColumnKind = Literal["string", "integer", "boolean", "date", "list"]


@dataclass(frozen=True)
class Column:
    """A physical column; decimal groups are expanded before construction."""

    name: str
    kind: ColumnKind
    nullable: bool


@dataclass(frozen=True)
class Table:
    """Entity-qualified keys and complete deterministic serialization order."""

    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    sort_key: tuple[str, ...]


def schema_document() -> dict:
    """Return a fresh JSON-safe expanded catalog, including semantic metadata."""
    raw = json.loads(
        files("jbt.contracts").joinpath("resources/catalog.json").read_text()
    )
    result = {
        "schema_version": raw["schema_version"],
        "numeric_limits": raw["numeric_limits"],
        "semantic_checks": json.loads(
            files("jbt.contracts").joinpath("resources/checks.json").read_text()
        )["checks"],
        "tables": [],
    }
    primary_keys = {spec["name"]: spec["primary_key"] for spec in raw["tables"]}
    for spec in raw["tables"]:
        logical = {"entity_id": "string", **spec["fields"]}
        for bundle in spec.get("bundles", []):
            logical.update(raw["bundles"][bundle])
        columns, groups = [], []
        for name, encoding in logical.items():
            nullable = encoding.endswith("?")
            kind = encoding.removesuffix("?")
            if kind == "decimal":
                groups.append({"name": name, "nullable": nullable})
                columns.extend(
                    [
                        {
                            "name": f"{name}_coefficient",
                            "kind": "string",
                            "nullable": nullable,
                        },
                        {
                            "name": f"{name}_scale",
                            "kind": "integer",
                            "nullable": nullable,
                        },
                        {
                            "name": f"{name}_source_scale",
                            "kind": "integer",
                            "nullable": True,
                        },
                    ]
                )
                continue
            column = {"name": name, "kind": kind, "nullable": nullable}
            enum = raw["enums"].get(f"{spec['name']}.{name}")
            if name.endswith("_mode") and name.startswith("date_"):
                enum = ["inherit", "value", "unknown"]
            if name == "timestamp_precision":
                enum = ["date", "minute", "second", "fractional_second"]
            if enum:
                column["enum"] = enum
            columns.append(column)
        refs = [
            {
                "columns": ["entity_id", field],
                "table": target,
                "target_columns": ["entity_id", *primary_keys[target]],
            }
            for field, target in raw["foreign_keys"].get(spec["name"], {}).items()
        ]
        primary = ["entity_id", *spec["primary_key"]]
        result["tables"].append(
            {
                "name": spec["name"],
                "columns": columns,
                "logical_fields": logical,
                "numeric_groups": groups,
                "primary_key": primary,
                "sort_key": ["entity_id", *spec.get("sort_key", spec["primary_key"])],
                "unique": [["entity_id", *key] for key in spec.get("unique", [])],
                "foreign_keys": refs,
            }
        )
    return result


def catalog() -> tuple[Table, ...]:
    """Return every required table, including the build relation."""
    return tuple(
        Table(
            spec["name"],
            tuple(Column(c["name"], c["kind"], c["nullable"]) for c in spec["columns"]),
            tuple(spec["primary_key"]),
            tuple(spec["sort_key"]),
        )
        for spec in schema_document()["tables"]
    )
