import pytest

from jbt.artifacts.projection import Rows
from jbt.contracts.primitives import ContractError


def test_nullable_source_disposition_is_not_an_invented_required_field() -> None:
    rows = Rows("owner")
    row = rows.add(
        "source_records",
        {
            "source_record_id": "a" * 64,
            "source_blob_digest": "b" * 64,
            "source_scope_id": "feed",
            "source_section": "section",
            "record_locator": "format:record",
            "input_kind": "bank_statement",
        },
        evidence=None,
    )
    assert row["ignored_reason"] is None
    assert row["statement_id"] is None
    with pytest.raises(ContractError, match="missing_projection_field"):
        Rows("owner").add("source_records", {}, evidence=None)
