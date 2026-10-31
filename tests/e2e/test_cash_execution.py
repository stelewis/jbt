import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest
from tests.fixtures.synthetic_importer.synthetic_statements.adapter import (
    SECTION,
    Entry,
    Statement,
)
from tests.integration.conformance.corpus import approved_schemas
from tests.integration.conformance.reader import read_snapshot

from jbt.runtime.execution import observe_execution

pytestmark = pytest.mark.e2e
_LOG = logging.getLogger(__name__)


def _install_tool(wheel: Path, plugin: Path, directory: Path) -> Path:
    uv = shutil.which("uv")
    assert uv is not None
    started = perf_counter()
    completed = subprocess.run(  # noqa: S603 - isolated ordinary tool provisioning
        [
            uv,
            "tool",
            "install",
            "--python",
            sys.executable,
            "--from",
            str(wheel),
            "--with",
            str(plugin),
            "jbt",
        ],
        cwd=directory,
        env={
            **os.environ,
            "UV_TOOL_DIR": str(directory / "tools"),
            "UV_TOOL_BIN_DIR": str(directory / "bin"),
        },
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    _LOG.info("Provisioning tool installation: %.2fs", perf_counter() - started)
    return directory / "bin/jbt"


def _build_wheel(project: Path, directory: Path) -> Path:
    uv = shutil.which("uv")
    assert uv is not None
    started = perf_counter()
    subprocess.run(  # noqa: S603 - ordinary packaging of reviewed synthetic inputs
        [
            uv,
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(directory),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        timeout=180,
    )
    _LOG.info("Provisioning %s wheel: %.2fs", project.name, perf_counter() - started)
    (wheel,) = directory.glob("*.whl")
    return wheel


@pytest.fixture(scope="session")
def non_c_locale() -> str:
    executable = shutil.which("locale")
    assert executable is not None
    result = subprocess.run(  # noqa: S603 - discover host-supported test locales
        [executable, "-a"], check=True, capture_output=True, text=True, timeout=30
    )
    candidates = [
        value
        for value in result.stdout.splitlines()
        if value.lower() in {"c.utf8", "c.utf-8", "en_us.utf-8", "en_us.utf8"}
    ]
    assert candidates, (
        "the installed determinism test requires a supported UTF-8 locale"
    )
    return min(candidates)


def _cli(
    *arguments: str,
    executable: Path,
    cwd: Path,
    ambient: tuple[str, str, str] = ("UTC", "C", "17"),
) -> dict:
    started = perf_counter()
    completed = subprocess.run(  # noqa: S603 - installed CLI, synthetic test arguments
        [str(executable), *arguments],
        cwd=cwd,
        env={
            **os.environ,
            "TZ": ambient[0],
            "LC_ALL": ambient[1],
            "PYTHONHASHSEED": ambient[2],
            "PATH": "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    _LOG.info("Processing %s: %.2fs", arguments[0], perf_counter() - started)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope="session")
def installed_cli(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config
) -> Path:
    directory = tmp_path_factory.mktemp("installed-cli")
    repository = Path(__file__).resolve().parents[2]
    selected = pytestconfig.getoption("release_wheel")
    if selected is not None:
        assert isinstance(selected, Path)
        wheel = selected.resolve(strict=True)
        assert wheel.suffix == ".whl"
    else:
        wheel = _build_wheel(repository, directory / "distributions")
    plugin = _build_wheel(
        repository / "tests/fixtures/synthetic_importer", directory / "plugin"
    )
    return _install_tool(wheel, plugin, directory)


def test_installed_release_cli_reuse_and_relocated_replay(
    tmp_path: Path,
    project_document: dict,
    installed_cli: Path,
    non_c_locale: str,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    root = tmp_path / "project"
    root.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    statement = Statement(
        "2026-01-01",
        "2026-02-01",
        115,
        (
            Entry("credit", "2026-01-12", 20, "Invented income"),
            Entry("debit", "2026-01-20", -5, "Invented expense"),
        ),
        complete=True,
        replaces=None,
    )
    sources = (
        (
            "prior.ofx",
            "ofx",
            "bank-feed",
            (
                repository / "tests/jbt/importers/fixtures_synthetic/prior.ofx"
            ).read_bytes(),
        ),
        (
            "opening.json",
            "synthetic-statements",
            "synthetic-feed",
            Statement(
                "2025-12-31", "2026-01-01", 100, (), complete=True, replaces=None
            ).encode(),
        ),
        ("main.json", "synthetic-statements", "synthetic-feed", statement.encode()),
    )
    accessions = []
    for name, importer, scope, data in sources:
        path = inbox / name
        path.write_bytes(data)
        receipt = _cli(
            "acquire",
            "--root",
            str(root),
            str(path),
            "--retry-key",
            name,
            "--acquired-at",
            "2026-02-02T00:00:00Z",
            "--source-scope",
            scope,
            "--importer",
            importer,
            cwd=inbox,
            executable=installed_cli,
        )
        accessions.append(receipt["accession_id"])
    project_document["sources"] = accessions
    account = next(d for d in project_document["declarations"] if d["key"] == "bank")
    account["payload"]["bindings"].append(
        {
            "source_scope_id": "synthetic-feed",
            "source_section": SECTION,
            "source_position_key": None,
        }
    )
    project_path = root / "project.json"
    project_path.write_text(json.dumps(project_document))
    first = _cli(
        "build",
        "--root",
        str(root),
        "--project",
        "project.json",
        cwd=tmp_path,
        executable=installed_cli,
    )
    generation = root / first["generation"]
    read_snapshot(
        generation,
        descriptor_digest=first["descriptor_digest"],
        expected_schema_digests=approved_schemas(),
    )
    execution = json.loads((generation / "execution.json").read_bytes())
    assert execution["producer"]["kind"] == "release"
    assert execution["producer"]["source_commit"] is None
    assert execution["runtime"]["version"]
    builtin, external = execution["importers"]
    assert builtin["importer_id"] == "ofx"
    assert builtin["distribution"] == "jbt"
    assert builtin["kind"] == "release"
    assert external == {
        "importer_id": "synthetic-statements",
        "distribution": "jbt-synthetic-statements",
        "version": "1.2.3",
        "kind": "release",
        "profile": "synthetic-statements",
        "profile_version": 1,
        "contract_version": 1,
        "maximum_bytes": 16_384,
    }
    assert observe_execution().document()["producer"]["kind"] == "development"
    warm = _cli(
        "build",
        "--root",
        str(root),
        "--project",
        "project.json",
        cwd=inbox,
        executable=installed_cli,
        ambient=("America/New_York", non_c_locale, "901"),
    )
    assert warm["executed"] == 0
    assert warm["reused"]
    assert warm["descriptor_digest"] == first["descriptor_digest"]
    shutil.rmtree(inbox)
    project_path.unlink()
    shutil.rmtree(root / "cache")
    restored = tmp_path / "restored"
    root.rename(restored)
    verification = _cli(
        "verify",
        "--root",
        str(restored),
        "--baseline",
        first["descriptor_digest"],
        cwd=tmp_path,
        executable=installed_cli,
        ambient=("America/New_York", non_c_locale, "611"),
    )
    assert verification["equal"]
    assert verification["reused"] == 0
    assert not (restored / "cache").exists()
