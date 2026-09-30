import pytest

from jbt.domain.errors import IdentityError
from jbt.domain.ids import EntityId, RecordId, RecordKind, SemanticKey


@pytest.mark.parametrize("value", ["", "e\u0301", "x\n", "\ud800", "\x7f"])
def test_keys_reject_noncanonical_or_control_text(value: str) -> None:
    with pytest.raises(IdentityError):
        EntityId(value)
    with pytest.raises(IdentityError):
        SemanticKey(value)


def test_identity_dimensions_are_not_interchangeable() -> None:
    first = RecordId(EntityId("one"), RecordKind.EVENT, "a" * 64)
    second = RecordId(EntityId("two"), RecordKind.EVENT, "a" * 64)
    leg = RecordId(EntityId("one"), RecordKind.LEG, "a" * 64)
    assert len({first, second, leg}) == 3
    first.require(entity=EntityId("one"), kind=RecordKind.EVENT)
    with pytest.raises(IdentityError, match="reference_kind_or_entity"):
        first.require(entity=EntityId("two"), kind=RecordKind.EVENT)
    with pytest.raises(IdentityError, match="reference_kind_or_entity"):
        first.require(entity=EntityId("one"), kind=RecordKind.LEG)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "a" * 65, "z" * 64, ""])
def test_generated_ids_require_complete_lowercase_sha256(digest: str) -> None:
    with pytest.raises(IdentityError, match="sha256_digest_required"):
        RecordId(EntityId("one"), RecordKind.EVENT, digest)
