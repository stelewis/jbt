"""Observe installed producer and runtime facts without provisioning or hashing code."""

import json
import platform
import re
from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata
from typing import cast

from jsonschema import ValidationError

from jbt.contracts.primitives import ContractError, require, validate_json_values
from jbt.contracts.schemas import execution_record_schema, validate_document
from jbt.domain.canonical import encode_json

_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z", re.ASCII)
_SEPARATORS = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class ObservedExecution:
    """Immutable canonical observation; development identities are not cacheable."""

    payload: bytes
    digest: str
    producer_digest: str
    is_release: bool
    producer_version: str

    def document(self) -> dict:
        """Return an independent mutable copy of the retained facts."""
        return json.loads(self.payload)


def decode_execution(payload: bytes) -> ObservedExecution:
    """Validate a closed canonical execution record and its distribution inventory."""
    require(type(payload) is bytes, "execution_encoding", "execution")
    try:
        document = json.loads(payload.decode("utf-8"))
        validate_document(document, execution_record_schema())
    except UnicodeError, ValueError, RecursionError, ValidationError:
        code = "execution_record"
        raise ContractError(code, "execution") from None
    require(
        payload == encode_json(document, integer_strings=False),
        "execution_canonical",
        "execution",
    )
    require(type(document["schema_version"]) is int, "execution_schema", "execution")
    names = [item["name"] for item in document["distributions"]]
    require(
        all(
            _NAME.fullmatch(name) is not None
            and _SEPARATORS.sub("-", name).lower() == name
            for name in names
        ),
        "distribution_name",
        "execution.distributions",
    )
    require(
        names == sorted(set(names)),
        "distribution_order_unique",
        "execution.distributions",
    )
    producer = document["producer"]
    require(
        bool(producer["version"])
        and all(document["runtime"].values())
        and all(item["version"] for item in document["distributions"])
        and (producer["source_commit"] is None or bool(producer["source_commit"])),
        "execution_required_fact",
        "execution",
    )
    require(
        any(
            item["name"] == "jbt" and item["version"] == producer["version"]
            for item in document["distributions"]
        ),
        "producer_distribution",
        "execution.producer",
    )
    return ObservedExecution(
        payload,
        sha256(payload).hexdigest(),
        sha256(
            encode_json(
                {key: producer[key] for key in ("kind", "name", "version")},
                integer_strings=False,
            )
        ).hexdigest(),
        producer["kind"] == "release",
        producer["version"],
    )


def _distribution_record(distribution: metadata.Distribution) -> dict[str, str]:
    fields = distribution.metadata
    names = fields.get_all("Name") or []
    versions = fields.get_all("Version") or []
    require(
        len(names) == len(versions) == 1
        and _NAME.fullmatch(names[0]) is not None
        and bool(versions[0]),
        "distribution_metadata",
        "execution.distributions",
    )
    record = {"name": _SEPARATORS.sub("-", names[0]).lower(), "version": versions[0]}
    validate_json_values(record, "execution.distributions")
    return record


def _producer(distribution: metadata.Distribution, version: str) -> dict:
    producer = {
        "kind": "release",
        "name": "jbt",
        "version": version,
        "source_commit": None,
    }
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is not None:
        try:
            direct = json.loads(direct_url)
            require(type(direct) is dict, "producer_metadata", "execution.producer")
            kinds = [
                key for key in ("archive_info", "dir_info", "vcs_info") if key in direct
            ]
            require(
                len(kinds) == 1 and type(direct[kinds[0]]) is dict,
                "producer_metadata",
                "execution.producer",
            )
            if kinds[0] != "archive_info":
                producer["kind"] = "development"
            if kinds[0] == "dir_info":
                require(
                    type(direct["dir_info"].get("editable", False)) is bool,
                    "producer_metadata",
                    "execution.producer",
                )
            if kinds[0] == "vcs_info":
                commit = direct["vcs_info"].get("commit_id")
                require(
                    type(commit) is str and bool(commit),
                    "producer_metadata",
                    "execution.producer",
                )
                producer["source_commit"] = commit
        except ValueError, RecursionError:
            code = "producer_metadata"
            raise ContractError(code, "execution.producer") from None
    validate_json_values(producer, "execution.producer")
    return producer


def observe_execution() -> ObservedExecution:
    """Capture all visible distribution metadata once, without importing packages.

    Index and archive installs trust the immutable publication policy.
    Directory and VCS installs are development, including noneditable source.
    """
    records = []
    producer_distribution = None
    names = set()
    try:
        for distribution in metadata.distributions():
            record = _distribution_record(distribution)
            require(
                record["name"] not in names,
                "distribution_duplicate",
                "execution.distributions",
            )
            names.add(record["name"])
            records.append(record)
            if record["name"] == "jbt":
                producer_distribution = distribution
        require(
            producer_distribution is not None,
            "producer_missing",
            "execution.producer",
        )
        version = next(
            record["version"] for record in records if record["name"] == "jbt"
        )
        producer = _producer(
            cast("metadata.Distribution", producer_distribution), version
        )
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, ContractError):
            raise
        code = "distribution_metadata"
        raise ContractError(code, "execution.distributions") from None
    return decode_execution(
        encode_json(
            {
                "schema_version": 1,
                "producer": producer,
                "runtime": {
                    "implementation": platform.python_implementation(),
                    "version": platform.python_version(),
                    "platform": platform.system(),
                    "architecture": platform.machine(),
                },
                "distributions": sorted(records, key=lambda record: record["name"]),
            },
            integer_strings=False,
        )
    )
