"""Compound reviewed facts over a bounded inventory of native targets."""

# Stable diagnostic codes and physical schema booleans are intentional.
# ruff: noqa: EM101, FBT003

import json
from hashlib import sha256
from itertools import product

from jbt.domain.canonical import encode_json
from jbt.domain.errors import GuardError
from jbt.domain.guards import GuardField, GuardReference, GuardTarget
from jbt.domain.ids import EntityId, SemanticKey


def compound_target(targets: tuple[GuardTarget, ...]) -> GuardTarget:
    """Seal exactly the operands of one structural decision."""
    if not targets or len({target.reference.entity for target in targets}) != 1:
        raise GuardError("same_entity_operands_required", "review.merge")
    ordered = tuple(
        sorted(
            targets,
            key=lambda target: (target.reference.table.value, target.reference.key),
        )
    )
    addresses = [
        [target.reference.table.value, list(target.reference.key)] for target in ordered
    ]
    if len({target.reference for target in ordered}) != len(ordered):
        raise GuardError("unique_operands_required", "review.merge")
    members = [
        {
            "table": target.reference.table.value,
            "key": list(target.reference.key),
            "fields": [
                {
                    "name": field.name,
                    "kind": field.kind,
                    "nullable": field.nullable,
                    "value": field.json_value(),
                }
                for field in target.fields
            ],
        }
        for target in ordered
    ]
    return GuardTarget(
        GuardReference(
            ordered[0].reference.entity,
            SemanticKey("review_merge"),
            (sha256(encode_json(addresses, integer_strings=False)).hexdigest(),),
        ),
        (
            GuardField(
                "members",
                "string",
                False,
                encode_json(members, integer_strings=False).decode().strip(),
            ),
        ),
        all(target.active for target in ordered),
    )


def compound_references(target: GuardTarget) -> tuple[GuardReference, ...]:
    """Read the immutable operand addresses without interpreting their facts."""
    if (
        target.reference.table.value != "review_merge"
        or len(target.fields) != 1
        or target.fields[0].name != "members"
        or target.fields[0].kind != "string"
        or target.fields[0].nullable
        or not isinstance(target.fields[0].value, str)
    ):
        raise GuardError("compound_profile_required", "review.merge")
    try:
        members = json.loads(target.fields[0].value)
    except (ValueError, RecursionError) as error:
        raise GuardError("compound_members_required", "review.merge") from error
    if not isinstance(members, list) or not members:
        raise GuardError("compound_members_required", "review.merge")
    references = []
    for member in members:
        if (
            not isinstance(member, dict)
            or set(member) != {"table", "key", "fields"}
            or member["table"]
            not in {
                "review_sources",
                "review_statements",
                "review_events",
                "review_legs",
                "review_documents",
            }
            or not isinstance(member["key"], list)
            or len(member["key"]) != 1
            or not isinstance(member["key"][0], str)
        ):
            raise GuardError("compound_member_required", "review.merge")
        references.append(
            GuardReference(
                EntityId(target.reference.entity.value),
                SemanticKey(member["table"]),
                (member["key"][0],),
            )
        )
    if len(set(references)) != len(references):
        raise GuardError("unique_operands_required", "review.merge")
    return tuple(references)


def compound_candidates(
    expected: GuardTarget, inventory: tuple[GuardTarget, ...]
) -> tuple[GuardTarget, ...]:
    """Resolve all operand combinations, including inactive and changed facts."""
    references = compound_references(expected)
    groups = tuple(
        tuple(target for target in inventory if target.reference == reference)
        for reference in references
    )
    return tuple(compound_target(tuple(targets)) for targets in product(*groups))
