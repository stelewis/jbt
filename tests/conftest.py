import json
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import byte_digest
from jbt.domain.canonical import encode_json
from jbt.extraction import ExtractionContext
from jbt.importers.ofx import extract, importer, to_extraction
from jbt.runtime.execution import ObservedExecution, decode_execution
from jbt.runtime.project import Project, read_project
from jbt.runtime.resolution import ExtractInput

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def extraction_documents() -> tuple[dict, ...]:
    directory = Path(__file__).parent / "jbt" / "importers" / "fixtures_synthetic"
    result = []
    for name in ("prior.ofx", "main.ofx"):
        raw = (directory / name).read_bytes()
        result.append(
            to_extraction(
                extract(raw), ExtractionContext("bank-feed", byte_digest(raw), "ofx")
            )
        )
    return tuple(result)


@pytest.fixture
def make_inputs(
    extraction_documents: tuple[dict, ...],
) -> Callable[[dict], tuple[Project, tuple[ExtractInput, ...]]]:
    def capture(value: dict) -> tuple[Project, tuple[ExtractInput, ...]]:
        project = read_project(encode_json(value, integer_strings=False))
        return project, tuple(
            ExtractInput(selection, encode_json(document, integer_strings=False))
            for selection, document in zip(
                project.sources, extraction_documents, strict=True
            )
        )

    return capture


@pytest.fixture
def project_document() -> dict:
    path = Path(__file__).parent / "jbt/runtime/fixtures_synthetic/project.json"
    return json.loads(path.read_bytes())


@pytest.fixture
def installed_ofx_release(monkeypatch: pytest.MonkeyPatch) -> None:
    distribution = SimpleNamespace(
        metadata=SimpleNamespace(
            get_all=lambda key: ["jbt"] if key == "Name" else ["0.1.0a1"]
        ),
        read_text=lambda _name: None,
    )
    entry = SimpleNamespace(name="ofx", dist=distribution, load=lambda: importer)
    monkeypatch.setattr(metadata, "entry_points", lambda **_kwargs: [entry])


@pytest.fixture
def release_execution() -> ObservedExecution:
    return decode_execution(
        encode_json(
            {
                "schema_version": 1,
                "producer": {
                    "kind": "release",
                    "name": "jbt",
                    "version": "0.1.0a1",
                    "source_commit": "a" * 40,
                },
                "runtime": {
                    "implementation": "cpython",
                    "version": "3.14.7",
                    "platform": "linux",
                    "architecture": "x86_64",
                },
                "distributions": [{"name": "jbt", "version": "0.1.0a1"}],
                "importers": [],
            },
            integer_strings=False,
        )
    )
