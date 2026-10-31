"""Observe installed producer and runtime facts without provisioning or hashing code."""

import json
import platform
import re
from annotationlib import Format
from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata
from inspect import signature
from typing import cast

from jsonschema import ValidationError

from jbt.contracts.primitives import ContractError, require, validate_json_values
from jbt.contracts.schemas import execution_record_schema, validate_document
from jbt.domain.canonical import encode_json
from jbt.domain.ids import SemanticKey
from jbt.extraction import (
    CONTRACT_VERSION,
    ExtractionContext,
    ExtractionError,
    Importer,
)

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
    importers = document["importers"]
    identifiers = [item["importer_id"] for item in importers]
    require(
        identifiers == sorted(set(identifiers)),
        "importer_order_unique",
        "execution.importers",
    )
    for item in importers:
        for field in ("importer_id", "profile"):
            SemanticKey(item[field])
        require(
            {"name": item["distribution"], "version": item["version"]}
            in document["distributions"],
            "importer_distribution",
            "execution.importers",
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
        producer["kind"] == "release"
        and all(item["kind"] == "release" for item in document["importers"]),
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
                "importers": [],
            },
            integer_strings=False,
        )
    )


def select_importers(
    execution: ObservedExecution, names: tuple[str, ...]
) -> tuple[ObservedExecution, dict[str, Importer]]:
    """Resolve every name from metadata before loading only selected adapters."""
    selected = {}
    entries = metadata.entry_points(group="jbt.importers")
    for name in sorted(set(names)):
        SemanticKey(name)
        matches = [entry for entry in entries if entry.name == name]
        require(bool(matches), "importer_missing", name)
        require(len(matches) == 1, "importer_duplicate", name)
        entry = matches[0]
        require(entry.dist is not None, "importer_distribution_missing", name)
        distribution = cast("metadata.Distribution", entry.dist)
        owner = _distribution_record(distribution)
        provenance = _producer(distribution, owner["version"])
        selected[name] = (entry, owner, provenance)
    plugins = {}
    records = []
    for name, (entry, owner, provenance) in selected.items():
        try:
            plugin = entry.load()
        except ImportError, AttributeError, ValueError, TypeError:
            code = "importer_load"
            raise ExtractionError(code, name) from None
        require(isinstance(plugin, Importer), "importer_protocol", name)
        require(callable(plugin.extract), "importer_protocol", name)
        try:
            signature(plugin.extract, annotation_format=Format.STRING).bind(
                b"", ExtractionContext("preflight", "0" * 64, name)
            )
        except TypeError, ValueError:
            code = "importer_protocol"
            raise ContractError(code, name) from None
        require(
            type(plugin.contract_version) is int
            and plugin.contract_version == CONTRACT_VERSION,
            "importer_contract_version",
            name,
        )
        require(
            type(plugin.profile) is str
            and bool(plugin.profile)
            and type(plugin.profile_version) is int
            and plugin.profile_version > 0
            and type(plugin.maximum_bytes) is int
            and plugin.maximum_bytes > 0,
            "importer_profile",
            name,
        )
        SemanticKey(plugin.profile)
        plugins[name] = plugin
        records.append(
            {
                "importer_id": name,
                "distribution": owner["name"],
                "version": owner["version"],
                "kind": provenance["kind"],
                "profile": plugin.profile,
                "profile_version": plugin.profile_version,
                "contract_version": plugin.contract_version,
                "maximum_bytes": plugin.maximum_bytes,
            }
        )
    document = execution.document()
    document["importers"] = records
    return (
        decode_execution(encode_json(document, integer_strings=False)),
        plugins,
    )
