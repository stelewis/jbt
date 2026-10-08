"""Check ordinary wheel and source artifacts without installing them."""
# ruff: noqa: INP001

import argparse
import tarfile
import tomllib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def check_publication(
    version: str, tag: str, release: object, *, release_id: int
) -> None:
    """Bind the triggering release to its documented REST API state."""
    if (
        type(release) is not dict
        or type(release_id) is not int
        or release_id <= 0
        or type(release.get("id")) is not int
        or release.get("id") != release_id
        or release.get("tag_name") != tag
    ):
        message = "Release API response does not match the triggering release"
        raise ValueError(message)
    if release.get("draft") is not False:
        message = "Publication requires a published GitHub release"
        raise ValueError(message)
    if release.get("immutable") is not True:
        message = "Publication requires an immutable GitHub release"
        raise ValueError(message)
    if tag != f"v{version}":
        message = f"Release tag {tag!r} must be 'v{version}'"
        raise ValueError(message)


def _check_metadata(
    payload: bytes, name: str, version: str
) -> tuple[list[str], list[str]]:
    fields = BytesParser().parsebytes(payload)
    if fields.get_all("Name") != [name] or fields.get_all("Version") != [version]:
        message = "Distribution metadata does not match the project name/version"
        raise ValueError(message)
    return fields.get_all("Requires-Python", []), sorted(
        fields.get_all("Requires-Dist", [])
    )


def _check_unique(names: list[str]) -> None:
    if len(names) != len(set(names)):
        message = "Duplicate distribution archive entries"
        raise ValueError(message)


def _source_bytes(source: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    if not member.isfile() or (stream := source.extractfile(member)) is None:
        message = "Expected a regular source distribution file"
        raise ValueError(message)
    with stream:
        return stream.read()


def check_release(distributions: Path, project: Path) -> None:
    """Require matching project metadata and complete package payloads."""
    project_bytes = project.read_bytes()
    configuration = tomllib.loads(project_bytes.decode("utf-8"))["project"]
    name, version = configuration["name"], configuration["version"]
    wheels = list(distributions.glob("*.whl"))
    sources = list(distributions.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        message = "Expected exactly one release wheel and source distribution"
        raise ValueError(message)
    with ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        _check_unique(names)
        wheel_metadata = [
            item for item in names if item.endswith(".dist-info/METADATA")
        ]
        if len(wheel_metadata) != 1:
            message = "Expected exactly one wheel metadata record"
            raise ValueError(message)
        wheel_requirements = _check_metadata(
            wheel.read(wheel_metadata[0]), name, version
        )
        wheel_package = {
            item.removeprefix("jbt/"): wheel.read(item)
            for item in names
            if item.startswith("jbt/") and not item.endswith("/")
        }
    with tarfile.open(sources[0]) as source:
        members = source.getmembers()
        _check_unique([member.name for member in members])
        source_metadata = [
            member for member in members if member.name == f"{name}-{version}/PKG-INFO"
        ]
        if len(source_metadata) != 1:
            message = "Expected exactly one source metadata record"
            raise ValueError(message)
        source_requirements = _check_metadata(
            _source_bytes(source, source_metadata[0]), name, version
        )
        project_members = [
            member
            for member in members
            if member.name == f"{name}-{version}/pyproject.toml"
        ]
        if (
            len(project_members) != 1
            or tomllib.loads(
                _source_bytes(source, project_members[0]).decode("utf-8")
            ).get("project")
            != configuration
        ):
            message = "Source archive differs from selected project configuration"
            raise ValueError(message)
        prefix = f"{name}-{version}/src/jbt/"
        source_package = {
            member.name.removeprefix(prefix): _source_bytes(source, member)
            for member in members
            if member.name.startswith(prefix) and not member.isdir()
        }
    if "__init__.py" not in wheel_package or wheel_package != source_package:
        message = "Wheel and source distribution package payloads disagree"
        raise ValueError(message)
    if wheel_requirements != source_requirements:
        message = "Wheel and source distribution dependency constraints disagree"
        raise ValueError(message)


def main() -> None:
    """Validate the final archives against the checked-out project."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    arguments = parser.parse_args()
    check_release(arguments.distributions, arguments.project)


if __name__ == "__main__":
    main()
