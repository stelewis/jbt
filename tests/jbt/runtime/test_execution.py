import json
from dataclasses import FrozenInstanceError
from email.message import Message
from hashlib import sha256
from importlib import metadata
from typing import TYPE_CHECKING

import pytest

from jbt.contracts.primitives import ContractError
from jbt.domain.canonical import encode_json
from jbt.runtime.execution import decode_execution, observe_execution

if TYPE_CHECKING:
    from collections.abc import Iterator
    from os import PathLike
    from pathlib import Path


class Distribution(metadata.Distribution):
    def __init__(
        self,
        name: str | None,
        version: str | None = "1.0",
        *,
        direct_url: str | None = None,
    ) -> None:
        self.fields = Message()
        if name is not None:
            self.fields["Name"] = name
        if version is not None:
            self.fields["Version"] = version
        self.direct_url = direct_url
        self.metadata_reads = 0

    @property
    def metadata(self) -> Message:
        self.metadata_reads += 1
        return self.fields

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self.direct_url

    def locate_file(self, path: str | PathLike[str]) -> Path:
        message = f"Unexpected producer file lookup: {path}"
        raise AssertionError(message)


@pytest.fixture
def release_producer() -> dict:
    return {
        "kind": "release",
        "name": "jbt",
        "version": "1.0",
        "source_commit": None,
    }


def test_index_install_observes_all_metadata_once_without_git(
    release_producer: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    distributions = [
        Distribution("Transitive__Thing", "2.0+EXACT"),
        Distribution("JBT"),
        Distribution("Extra.Unused", "7!1.0"),
    ]
    discoveries = []

    def discover() -> Iterator[Distribution]:
        discoveries.append(True)
        return iter(distributions)

    monkeypatch.setattr(metadata, "distributions", discover)
    monkeypatch.setenv("PATH", "")
    observed = observe_execution()
    assert observed.is_release
    assert observed.producer_version == "1.0"
    assert observed.document()["producer"] == release_producer
    assert observed.document()["distributions"] == [
        {"name": "extra-unused", "version": "7!1.0"},
        {"name": "jbt", "version": "1.0"},
        {"name": "transitive-thing", "version": "2.0+EXACT"},
    ]
    assert discoveries == [True]
    assert [item.metadata_reads for item in distributions] == [1, 1, 1]
    assert b'"schema_version":1' in observed.payload
    assert observed.digest == sha256(observed.payload).hexdigest()
    assert (
        observed.producer_digest
        == sha256(
            encode_json(
                {"kind": "release", "name": "jbt", "version": "1.0"},
                integer_strings=False,
            )
        ).hexdigest()
    )
    assert decode_execution(observed.payload) == observed


def test_ordinary_wheel_install_is_release_without_a_stamp(
    release_producer: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct_url = json.dumps(
        {
            "url": "file:///private-location/jbt-1.0-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": "a" * 64}},
        }
    )
    monkeypatch.setattr(
        metadata,
        "distributions",
        lambda: [Distribution("jbt", direct_url=direct_url)],
    )
    observed = observe_execution()
    assert observed.is_release
    assert observed.document()["producer"] == release_producer
    assert b"private-location" not in observed.payload
    assert b"archive_info" not in observed.payload


@pytest.mark.parametrize(
    ("information", "commit"),
    [
        ({"dir_info": {"editable": True}}, None),
        ({"dir_info": {"editable": False}}, None),
        ({"dir_info": {}}, None),
        ({"vcs_info": {"vcs": "git", "commit_id": "a" * 40}}, "a" * 40),
    ],
)
def test_source_installs_are_development(
    release_producer: dict,
    monkeypatch: pytest.MonkeyPatch,
    information: dict,
    commit: str | None,
) -> None:
    direct_url = json.dumps({"url": "file:///private-source", **information})
    monkeypatch.setattr(
        metadata,
        "distributions",
        lambda: [Distribution("jbt", direct_url=direct_url)],
    )
    observed = observe_execution()
    assert not observed.is_release
    assert observed.document()["producer"] == {
        **release_producer,
        "kind": "development",
        "source_commit": commit,
    }
    assert b"private-source" not in observed.payload


def test_development_observation_does_not_identify_mutable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = Distribution(
        "jbt",
        direct_url=json.dumps(
            {"url": "file:///first-checkout", "dir_info": {"editable": True}}
        ),
    )
    monkeypatch.setattr(metadata, "distributions", lambda: [distribution])
    first = observe_execution()
    distribution.direct_url = json.dumps(
        {"url": "file:///other-checkout", "dir_info": {"editable": True}}
    )
    second = observe_execution()
    assert not second.is_release
    assert first == second


@pytest.mark.parametrize(
    "direct_url",
    [
        "private invalid metadata",
        "[]",
        "{}",
        '{"dir_info":[]}',
        '{"archive_info":null}',
        '{"dir_info":{},"archive_info":{}}',
        '{"dir_info":{"editable":"true"}}',
        '{"dir_info":{"editable":0}}',
        '{"vcs_info":{}}',
        '{"vcs_info":{"commit_id":null}}',
        '{"vcs_info":{"commit_id":""}}',
        '{"vcs_info":{"commit_id":42}}',
    ],
)
def test_bad_direct_install_metadata_fails_safely(
    monkeypatch: pytest.MonkeyPatch, direct_url: str
) -> None:
    monkeypatch.setattr(
        metadata,
        "distributions",
        lambda: [Distribution("jbt", direct_url=direct_url)],
    )
    with pytest.raises(ContractError) as error:
        observe_execution()
    assert str(error.value) == "producer_metadata: execution.producer"


@pytest.mark.parametrize(
    ("names", "versions", "code"),
    [
        (["jbt", None], ["1.0", "1"], "distribution_metadata"),
        (["jbt", "other"], ["1.0", None], "distribution_metadata"),
        (["jbt", "other"], ["1.0", "non-NFC-e\u0301"], "text_nfc"),
        (["jbt", "bad/name"], ["1.0", "1"], "distribution_metadata"),
        (["jbt", "a_b", "A.B"], ["1.0", "1", "1"], "distribution_duplicate"),
        (["jbt", "JBT"], ["1.0", "1.0"], "distribution_duplicate"),
        (["other"], ["1"], "producer_missing"),
    ],
)
def test_bad_visible_metadata_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str | None],
    versions: list[str | None],
    code: str,
) -> None:
    distributions = [
        Distribution(name, version)
        for name, version in zip(names, versions, strict=True)
    ]
    monkeypatch.setattr(metadata, "distributions", lambda: distributions)
    with pytest.raises(ContractError) as error:
        observe_execution()
    assert error.value.code == code
    assert str(error.value) == f"{code}: execution." + (
        "producer"
        if code == "producer_missing"
        else "distributions.version"
        if code == "text_nfc"
        else "distributions"
    )


@pytest.mark.parametrize("field", ["Name", "Version"])
def test_duplicate_metadata_headers_fail(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    distribution = Distribution("jbt")
    distribution.fields[field] = distribution.fields[field]
    monkeypatch.setattr(metadata, "distributions", lambda: [distribution])
    with pytest.raises(ContractError, match="distribution_metadata"):
        observe_execution()


def test_observation_immutable_and_document_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt")])
    observed = observe_execution()
    document = observed.document()
    document["producer"]["version"] = "different"
    document["distributions"].clear()
    assert observed.document()["producer"]["version"] == "1.0"
    assert observed.document()["distributions"] == [{"name": "jbt", "version": "1.0"}]
    with pytest.raises(FrozenInstanceError):
        observed.digest = "changed"  # ty: ignore[invalid-assignment]


def test_runtime_and_dependencies_change_observation_not_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt")])
    first = observe_execution()
    monkeypatch.setattr("jbt.runtime.execution.platform.system", lambda: "OtherOS")
    monkeypatch.setattr(
        metadata,
        "distributions",
        lambda: [Distribution("jbt"), Distribution("extra")],
    )
    second = observe_execution()
    assert first.digest != second.digest
    assert first.producer_digest == second.producer_digest


def test_release_version_is_identity_and_source_commit_is_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt")])
    document = observe_execution().document()
    observations = []
    for commit in ("a" * 40, "c" * 40, None):
        document["producer"]["source_commit"] = commit
        observations.append(
            decode_execution(encode_json(document, integer_strings=False))
        )
    assert len({observed.digest for observed in observations}) == 3
    assert len({observed.producer_digest for observed in observations}) == 1
    assert all(observed.is_release for observed in observations)
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt", "2.0")])
    assert observe_execution().producer_digest != observations[0].producer_digest


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("extra", "forbidden"),
        ("producer", {"kind": "release"}),
        ("distributions", [{"name": "Not_Normalized", "version": "1.0"}]),
        ("distributions", [{"name": "jbt", "version": ""}]),
        ("distributions", []),
    ],
)
def test_decode_rejects_invalid_records(
    monkeypatch: pytest.MonkeyPatch, field: str, replacement: object
) -> None:
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt")])
    document = observe_execution().document()
    document[field] = replacement
    with pytest.raises(ContractError):
        decode_execution(encode_json(document, integer_strings=False))


def test_decode_rejects_noncanonical_and_duplicate_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution("jbt")])
    observed = observe_execution()
    for payload in (
        observed.payload.rstrip(b"\n"),
        json.dumps(observed.document()).encode(),
        observed.payload.replace(
            b'"schema_version":1', b'"schema_version":1,"schema_version":1'
        ),
        b"\xff",
        b"{}",
    ):
        with pytest.raises(ContractError):
            decode_execution(payload)
