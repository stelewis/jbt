# Strong Internal Types

Replace raw primitives used as identifiers or closed vocabularies with types that prevent accidental mixing.

## Choose the lightest fitting type

- `StrEnum` for a closed string vocabulary.
- `NewType` for identifiers that share a runtime representation but must not be mixed.
- A frozen dataclass or validated model for structured values with invariants.

## Refactor

1. Find repeated literals, string comparisons, dictionary-key vocabularies, and identifier-shaped strings.
2. Define the canonical type near the concept it represents.
3. Add strict parsing for wire, database, environment, file, or CLI inputs.
4. Move parsing to ingestion boundaries and update internal signatures.
5. Serialize explicitly at output boundaries, including logs, databases, JSON, hashes, and signatures.
6. Update tests to construct values through production parsers when validation is part of the contract.

Do not use `cast()` to turn untrusted data into a trusted type. Do not maintain both raw-string and typed internal APIs.

```python
from enum import StrEnum
from typing import NewType

OrderId = NewType("OrderId", str)


def parse_order_id(value: str) -> OrderId:
    normalized = value.strip()
    if not normalized:
        raise ValueError("order_id must not be empty")
    return OrderId(normalized)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"
```
