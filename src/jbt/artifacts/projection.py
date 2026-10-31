"""Catalog-aware physical rows and generic field provenance."""

from collections.abc import Mapping

from jbt.artifacts.integrity import byte_digest
from jbt.contracts.catalog import tabular_schema
from jbt.contracts.primitives import require
from jbt.domain.canonical import encode_json
from jbt.domain.numbers import ExactDecimal

type Evidence = tuple[str, str, str, str | None]


def projection_key(*parts: object) -> str:
    """Hash a structured grain without ambiguous textual concatenation."""
    return byte_digest(encode_json(list(parts), integer_strings=False))


class Rows:
    """Fill nullable cells only; required fields remain explicit."""

    def __init__(self, entity_id: str) -> None:
        """Capture one entity and the canonical physical catalog."""
        self.entity = entity_id
        self.specs = {item["name"]: item for item in tabular_schema()["tables"]}
        self.tables: dict[str, list[dict[str, object]]] = {
            name: [] for name in self.specs if name != "build"
        }

    def add(
        self,
        table: str,
        fields: Mapping[str, object],
        *,
        evidence: Evidence | Mapping[str, tuple[Evidence, ...]] | None,
    ) -> dict[str, object]:
        """Expand exact decimal groups and record non-null field provenance."""
        spec = self.specs[table]
        logical = {"entity_id": self.entity, **fields}
        require(
            set(logical) <= set(spec["logical_fields"]),
            "unknown_projection_field",
            table,
        )
        row: dict[str, object] = {}
        for name, encoding in spec["logical_fields"].items():
            require(
                name in logical or encoding.endswith("?"),
                "missing_projection_field",
                f"{table}.{name}",
            )
            value = logical.get(name)
            if encoding.removesuffix("?") == "decimal":
                require(
                    value is None or isinstance(value, ExactDecimal),
                    "projection_decimal",
                    table,
                )
                for suffix in ("coefficient", "scale", "source_scale"):
                    row[f"{name}_{suffix}"] = (
                        getattr(value, suffix) if value is not None else None
                    )
            else:
                row[name] = value
        self.tables[table].append(row)
        if evidence is not None:
            key = [logical[field] for field in spec["primary_key"][1:]]
            for field, value in fields.items():
                if field in spec["primary_key"] or value is None:
                    continue
                sources = (
                    evidence[field] if isinstance(evidence, Mapping) else (evidence,)
                )
                require(bool(sources), "missing_projection_evidence", table)
                for kind, identifier, origin, derivation in sources:
                    self.add(
                        "provenance",
                        {
                            "provenance_id": projection_key(
                                table, key, field, kind, identifier
                            ),
                            "record_kind": table,
                            "record_id": encode_json(key, integer_strings=False)
                            .decode()
                            .strip(),
                            "field_name": field,
                            "value_origin": origin,
                            "evidence_kind": kind,
                            "evidence_id": identifier,
                            "derivation_id": derivation,
                        },
                        evidence=None,
                    )
        return row

    def finish(self) -> dict[str, list[dict[str, object]]]:
        """Sort each grain by its catalog key for deterministic emission."""
        for name, rows in self.tables.items():
            keys = self.specs[name]["sort_key"]
            rows.sort(
                key=lambda row: tuple((row[key] is not None, row[key]) for key in keys)
            )
        return self.tables
