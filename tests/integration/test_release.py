import io
import runpy
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZipFile

import pytest

_METADATA = b"Metadata-Version: 2.4\nName: jbt\nVersion: 1.0\n\n"
_PROJECT = b'[project]\nname = "jbt"\nversion = "1.0"\n'
_WHEEL = (
    ("jbt-1.0.dist-info/METADATA", _METADATA),
    ("jbt/__init__.py", b'"""Synthetic package."""\n'),
    ("jbt/contracts/resources/catalog.json", b'{"synthetic":true}\n'),
)
_SOURCE = (
    ("jbt-1.0/PKG-INFO", _METADATA),
    ("jbt-1.0/pyproject.toml", _PROJECT),
    ("jbt-1.0/src/jbt/__init__.py", _WHEEL[1][1]),
    ("jbt-1.0/src/jbt/contracts/resources/catalog.json", _WHEEL[2][1]),
)


def _release(
    root: Path,
    *,
    wheel_entries: tuple[tuple[str, bytes], ...] = _WHEEL,
    source_entries: tuple[tuple[str, bytes], ...] = _SOURCE,
) -> tuple[Path, Path]:
    project = root / "pyproject.toml"
    project.write_bytes(_PROJECT)
    distributions = root / "dist"
    distributions.mkdir()
    with ZipFile(distributions / "jbt-1.0-py3-none-any.whl", "w") as wheel:
        for name, payload in wheel_entries:
            wheel.writestr(name, payload)
    with tarfile.open(distributions / "jbt-1.0.tar.gz", "w:gz") as source:
        for name, payload in source_entries:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            source.addfile(member, io.BytesIO(payload))
    return distributions, project


def _check(distributions: Path, project: Path) -> None:
    runpy.run_path(".github/scripts/check_release.py")["check_release"](
        distributions, project
    )


@pytest.mark.parametrize(
    ("tag", "immutable", "error"),
    [
        ("v1.0", True, None),
        ("v1.0", False, "immutable GitHub release"),
        ("v2.0", True, "Release tag"),
        ("", True, "Release tag"),
    ],
)
def test_publication_requires_immutable_matching_version(
    tag: str, *, immutable: bool, error: str | None
) -> None:
    check = runpy.run_path(".github/scripts/check_release.py")["check_publication"]
    release = {"id": 7, "tag_name": tag, "immutable": immutable, "draft": False}
    if error is None:
        check("1.0", tag, release, release_id=7)
    else:
        with pytest.raises(ValueError, match=error):
            check("1.0", tag, release, release_id=7)


@pytest.mark.parametrize("immutable", [None, "true", 1, False])
def test_rest_immutability_must_be_explicit_boolean_true(
    immutable: object,
) -> None:
    check = runpy.run_path(".github/scripts/check_release.py")["check_publication"]
    release = {"id": 7, "tag_name": "v1.0", "immutable": immutable, "draft": False}
    with pytest.raises(ValueError, match="immutable GitHub release"):
        check("1.0", "v1.0", release, release_id=7)
    del release["immutable"]
    with pytest.raises(ValueError, match="immutable GitHub release"):
        check("1.0", "v1.0", release, release_id=7)


@pytest.mark.parametrize(
    "release",
    [
        None,
        [],
        {"id": 8, "tag_name": "v1.0", "immutable": True, "draft": False},
        {"id": "7", "tag_name": "v1.0", "immutable": True, "draft": False},
        {"id": True, "tag_name": "v1.0", "immutable": True, "draft": False},
        {"id": 7, "tag_name": "v2.0", "immutable": True, "draft": False},
    ],
)
def test_rest_response_must_match_event_identity(release: object) -> None:
    check = runpy.run_path(".github/scripts/check_release.py")["check_publication"]
    with pytest.raises(ValueError, match="match the triggering release"):
        check("1.0", "v1.0", release, release_id=7)


@pytest.mark.parametrize("draft", [True, None, "false", 0])
def test_rest_response_must_identify_a_published_release(draft: object) -> None:
    check = runpy.run_path(".github/scripts/check_release.py")["check_publication"]
    release = {"id": 7, "tag_name": "v1.0", "immutable": True, "draft": draft}
    with pytest.raises(ValueError, match="published GitHub release"):
        check("1.0", "v1.0", release, release_id=7)


def test_standard_distributions_need_no_custom_provenance(tmp_path: Path) -> None:
    _check(*_release(tmp_path))


def test_final_wheel_option_is_registered_before_default_collection() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(), prefix=".release-wheel-option-", suffix=".whl"
    ) as wheel:
        result = subprocess.run(  # noqa: S603 - collect tests, never execute the fixture
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                "e2e",
                "--release-wheel",
                wheel.name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stderr
    assert "tests/e2e/test_cash_execution.py::" in result.stdout


@pytest.mark.parametrize("archive", ["wheel", "source"])
@pytest.mark.parametrize(
    "metadata",
    [
        b"Name: other\nVersion: 1.0\n\n",
        b"Name: jbt\nVersion: 2.0\n\n",
        b"Name: jbt\n\n",
        b"Version: 1.0\n\n",
        b"Name: jbt\nName: jbt\nVersion: 1.0\n\n",
        b"Name: jbt\nVersion: 1.0\nVersion: 1.0\n\n",
    ],
)
def test_metadata_must_identify_project_exactly_once(
    tmp_path: Path, archive: str, metadata: bytes
) -> None:
    entries = _WHEEL if archive == "wheel" else _SOURCE
    changed = ((entries[0][0], metadata), *entries[1:])
    distributions, project = _release(
        tmp_path,
        wheel_entries=changed if archive == "wheel" else _WHEEL,
        source_entries=changed if archive == "source" else _SOURCE,
    )
    with pytest.raises(ValueError, match="project name/version"):
        _check(distributions, project)


@pytest.mark.parametrize("archive", ["wheel", "source"])
def test_missing_metadata_is_rejected(tmp_path: Path, archive: str) -> None:
    distributions, project = _release(
        tmp_path,
        wheel_entries=_WHEEL[1:] if archive == "wheel" else _WHEEL,
        source_entries=_SOURCE[1:] if archive == "source" else _SOURCE,
    )
    with pytest.raises(ValueError, match=f"exactly one {archive} metadata"):
        _check(distributions, project)


@pytest.mark.parametrize("archive", ["wheel", "source"])
def test_duplicate_archive_entries_are_rejected(tmp_path: Path, archive: str) -> None:
    if archive == "wheel":
        with pytest.warns(UserWarning, match="Duplicate name"):
            distributions, project = _release(
                tmp_path, wheel_entries=(*_WHEEL, _WHEEL[0])
            )
    else:
        distributions, project = _release(
            tmp_path, source_entries=(*_SOURCE, _SOURCE[0])
        )
    with pytest.raises(ValueError, match="Duplicate distribution archive"):
        _check(distributions, project)


@pytest.mark.parametrize(
    "source_entries",
    [
        _SOURCE[:-1],
        (*_SOURCE[:-1], (_SOURCE[-1][0], b'{"different":true}\n')),
        (*_SOURCE, ("jbt-1.0/src/jbt/extra.py", b"")),
        _SOURCE[:2],
    ],
)
def test_package_inventory_and_bytes_must_match(
    tmp_path: Path, source_entries: tuple[tuple[str, bytes], ...]
) -> None:
    distributions, project = _release(tmp_path, source_entries=source_entries)
    with pytest.raises(ValueError, match="package payloads disagree"):
        _check(distributions, project)


def test_metadata_only_archives_are_not_a_package(tmp_path: Path) -> None:
    distributions, project = _release(
        tmp_path, wheel_entries=_WHEEL[:1], source_entries=_SOURCE[:2]
    )
    with pytest.raises(ValueError, match="package payloads disagree"):
        _check(distributions, project)


@pytest.mark.parametrize("suffix", [".whl", ".tar.gz"])
@pytest.mark.parametrize("count", [0, 2])
def test_requires_exactly_one_of_each_distribution(
    tmp_path: Path, suffix: str, count: int
) -> None:
    distributions, project = _release(tmp_path)
    (artifact,) = distributions.glob(f"*{suffix}")
    if count == 0:
        artifact.unlink()
    else:
        (distributions / f"duplicate{suffix}").write_bytes(artifact.read_bytes())
    with pytest.raises(ValueError, match="exactly one release wheel"):
        _check(distributions, project)


@pytest.mark.parametrize("replacement", [None, b'[project]\nversion = "other"\n'])
def test_source_configuration_must_match_selected_project(
    tmp_path: Path, replacement: bytes | None
) -> None:
    entries = (_SOURCE[0], *_SOURCE[2:])
    if replacement is not None:
        entries = (*entries, (_SOURCE[1][0], replacement))
    distributions, project = _release(tmp_path, source_entries=entries)
    with pytest.raises(ValueError, match="selected project configuration"):
        _check(distributions, project)


def test_source_configuration_may_be_normalized_by_backend(
    tmp_path: Path,
) -> None:
    normalized = b'[project]\nversion = "1.0"\n# Reformatted\nname = "jbt"\n'
    entries = (_SOURCE[0], (_SOURCE[1][0], normalized), *_SOURCE[2:])
    _check(*_release(tmp_path, source_entries=entries))


@pytest.mark.parametrize(
    "constraints", [b"Requires-Python: >=3.15", b"Requires-Dist: other>=1"]
)
def test_dependency_constraints_must_agree(tmp_path: Path, constraints: bytes) -> None:
    metadata = _METADATA.replace(b"\n\n", b"\n" + constraints + b"\n\n")
    entries = ((_SOURCE[0][0], metadata), *_SOURCE[1:])
    distributions, project = _release(tmp_path, source_entries=entries)
    with pytest.raises(ValueError, match="dependency constraints disagree"):
        _check(distributions, project)


@pytest.mark.parametrize(
    "name",
    [
        "jbt-1.0/PKG-INFO",
        "jbt-1.0/pyproject.toml",
        "jbt-1.0/src/jbt/__init__.py",
    ],
)
def test_source_metadata_and_payloads_must_be_regular_files(
    tmp_path: Path, name: str
) -> None:
    distributions, project = _release(tmp_path)
    with tarfile.open(distributions / "jbt-1.0.tar.gz", "w:gz") as source:
        for filename, payload in _SOURCE:
            member = tarfile.TarInfo(filename)
            if filename == name:
                member.type = tarfile.SYMTYPE
                member.linkname = "elsewhere"
                source.addfile(member)
            else:
                member.size = len(payload)
                source.addfile(member, io.BytesIO(payload))
    with pytest.raises(ValueError, match="regular source distribution file"):
        _check(distributions, project)
