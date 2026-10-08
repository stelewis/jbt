import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import byte_digest
from jbt.domain.canonical import encode_json
from jbt.importers.ofx import OfxContext, extract, to_extraction
from jbt.runtime.project import Project, read_project
from jbt.runtime.record import ExtractInput

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
                extract(raw), OfxContext("bank-feed", byte_digest(raw), "ofx")
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
    path = Path(__file__).resolve().parents[1] / "examples/cash/project.json"
    return json.loads(path.read_bytes())
