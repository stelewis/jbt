import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import duckdb
import pytest
from beancount import loader
from tests.integration.conformance.corpus import approved_schemas
from tests.integration.conformance.reader import (
    check_assertions,
    read_snapshot,
)

pytestmark = pytest.mark.e2e
_LOG = logging.getLogger(__name__)


def _install_tool(wheel: Path, directory: Path) -> Path:
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


@pytest.fixture
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
    success: bool = True,
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
    if not success:
        assert completed.returncode != 0
        assert completed.stderr
        return {}
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture
def installed_cli(tmp_path: Path, pytestconfig: pytest.Config) -> tuple[Path, Path]:
    selected = pytestconfig.getoption("release_wheel")
    if selected is not None:
        assert isinstance(selected, Path)
        wheel = selected.resolve(strict=True)
        assert wheel.suffix == ".whl"
        return _install_tool(wheel, tmp_path), wheel
    repository = Path(__file__).resolve().parents[2]
    uv = shutil.which("uv")
    assert uv is not None
    distributions = tmp_path / "distributions"
    started = perf_counter()
    subprocess.run(  # noqa: S603 - ordinary packaging of the working checkout
        [
            uv,
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(distributions),
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        timeout=180,
    )
    _LOG.info("Provisioning release build: %.2fs", perf_counter() - started)
    (wheel,) = distributions.glob("*.whl")
    return _install_tool(wheel, tmp_path), wheel


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _check_financial_outputs(
    generation: Path, descriptor_digest: str, schema_pins: dict[str, str]
) -> None:
    snapshot = read_snapshot(
        generation,
        descriptor_digest=descriptor_digest,
        expected_schema_digests=schema_pins,
    )
    assert all("is_dirty" not in row for row in snapshot.tables["build"])
    execution = json.loads((generation / "execution.json").read_bytes())
    assert execution["producer"]["kind"] == "release"
    assert execution["producer"]["source_commit"] is None
    assert execution["runtime"]["version"]
    assert execution["distributions"]
    assert [
        result.status
        for result in check_assertions(
            snapshot.tables, entity="invented-owner", date_roles={"posted": "posted"}
        )
    ] == ["pass", "pass"]
    assert [
        (check["check_kind"], check["status"])
        for check in snapshot.manifest["checks"]
        if check["check_kind"] == "coverage"
    ] == [("coverage", "not_evaluable")]
    ledgers = tuple(generation.rglob("*.beancount"))
    assert len(ledgers) == 1
    text = ledgers[0].read_text()
    _, errors, _ = loader.load_string(text)
    assert not errors
    assert "2026-01-01 balance Assets:Bank 100 ~ 0 USD" in text
    assert "2026-02-01 balance Assets:Bank 115 ~ 0 USD" in text
    with duckdb.connect(
        config={
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "allow_community_extensions": "false",
        }
    ) as connection:
        connection.execute(
            "SET allowed_paths = ?", [[str(generation / "postings.parquet")]]
        )
        connection.execute("SET enable_external_access = false")
        amounts = connection.execute(
            "SELECT amount_coefficient, amount_scale FROM read_parquet(?) "
            "WHERE leg_kind = 'position'",
            [str(generation / "postings.parquet")],
        ).fetchall()
    assert sorted(amounts) == [("-5", 0), ("100", 0), ("20", 0)]
    summaries = tuple(generation.rglob("summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_bytes())
    january = next(
        row for row in summary["monthly_quantities"] if row["month"] == "2026-01"
    )
    assert [
        january[key]["coefficient"]
        for key in ("opening", "credits", "debits", "closing")
    ] == [
        "100",
        "20",
        "-5",
        "115",
    ]
    coverage = next(
        check for check in summary["checks"] if check["check_kind"] == "coverage"
    )
    assert coverage["status"] == "not_evaluable"
    assert all(
        check["status"] == "pass"
        for check in summary["checks"]
        if check["check_kind"] != "coverage"
    )


def _acquire(root: Path, inbox: Path, project: dict, executable: Path) -> list[dict]:
    receipts = []
    for selection, name in zip(
        project["sources"], ("prior.ofx", "main.ofx"), strict=True
    ):
        receipt = _cli(
            "acquire",
            "--root",
            str(root),
            str(inbox / name),
            "--retry-key",
            name,
            "--acquired-at",
            "2026-02-02T00:00:00Z",
            "--source-scope",
            "bank-feed",
            "--importer",
            "ofx",
            cwd=inbox,
            executable=executable,
        )
        assert selection["accession_id"] == receipt["accession_id"]
        receipts.append(receipt)
    return receipts


def test_release_cash_build_reuse_and_relocated_uncached_verify(
    tmp_path: Path,
    project_document: dict,
    installed_cli: tuple[Path, Path],
    non_c_locale: str,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    root = tmp_path / "project"
    root.mkdir()
    inbox = tmp_path / "inbox"
    shutil.copytree(repository / "tests/jbt/importers/fixtures_synthetic", inbox)
    executable, wheel = installed_cli
    receipts = _acquire(root, inbox, project_document, executable)
    project_path = root / "project.json"
    project_path.write_text(json.dumps(project_document))
    first = _cli(
        "build",
        "--root",
        str(root),
        "--project",
        "project.json",
        cwd=tmp_path,
        executable=executable,
    )
    generation = root / first["generation"]
    _check_financial_outputs(generation, first["descriptor_digest"], approved_schemas())
    original = _files(generation)
    warm = _cli(
        "build",
        "--root",
        str(root),
        "--project",
        "project.json",
        cwd=inbox,
        executable=executable,
        ambient=("America/New_York", non_c_locale, "901"),
    )
    assert warm["executed"] == 0
    assert warm["reused"]
    assert warm["descriptor_digest"] == first["descriptor_digest"]
    independent = tmp_path / "independent"
    independent.mkdir()
    _acquire(independent, inbox, project_document, executable)
    (independent / "project.json").write_text(json.dumps(project_document))
    second = _cli(
        "build",
        "--root",
        str(independent),
        "--project",
        "project.json",
        cwd=tmp_path,
        executable=executable,
        ambient=("America/New_York", non_c_locale, "313"),
    )
    assert second["reused"] == 0
    assert second["financial_digest"] == first["financial_digest"]
    assert second["descriptor_digest"] == first["descriptor_digest"]
    _check_financial_outputs(
        independent / second["generation"],
        second["descriptor_digest"],
        approved_schemas(),
    )
    missing = root / "objects" / receipts[0]["object_digest"]
    displaced = tmp_path / "displaced-object"
    missing.rename(displaced)
    _cli(
        "build",
        "--root",
        str(root),
        "--project",
        "project.json",
        cwd=tmp_path,
        success=False,
        executable=executable,
    )
    displaced.rename(missing)
    assert _files(generation) == original
    shutil.rmtree(inbox)
    project_path.unlink()
    shutil.rmtree(root / "cache")
    restored = tmp_path / "restored"
    root.rename(restored)
    shutil.rmtree(tmp_path / "tools")
    executable.unlink()
    executable = _install_tool(wheel, tmp_path)
    pin = first["descriptor_digest"]
    selected_files = {
        path.name: path.read_bytes() for path in restored.iterdir() if path.is_file()
    }
    _cli(
        "verify",
        "--root",
        str(restored),
        "--baseline",
        pin,
        cwd=tmp_path,
        executable=executable,
        ambient=("America/New_York", "C", "611"),
    )
    assert _files(restored / first["generation"]) == original
    assert not (restored / "cache").exists()
    assert selected_files == {
        path.name: path.read_bytes() for path in restored.iterdir() if path.is_file()
    }
    assert not (restored / "environment").exists()
    assert not (restored / "uv.lock").exists()
