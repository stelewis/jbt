import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "package",
    [
        "jbt",
        "jbt.artifacts",
        "jbt.checks",
        "jbt.contracts",
        "jbt.domain",
        "jbt.importers",
        "jbt.model",
        "jbt.runtime",
        "jbt.storage",
    ],
)
def test_package_import_does_not_load_native_adapters(package: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed package names and isolated interpreter
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import importlib, sys; importlib.import_module(sys.argv[1]); "
                "assert not {'pyarrow', 'duckdb', 'beancount', 'jsonschema'} "
                "& sys.modules.keys()"
            ),
            package,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_internal_imports_follow_owning_boundaries() -> None:
    allowed = {
        "domain": {"domain"},
        "model": {"domain", "model"},
        "checks": {"domain", "checks"},
        "contracts": {"domain", "contracts"},
        "artifacts": {"domain", "contracts", "artifacts"},
        "extraction": {"domain", "contracts", "extraction"},
        "importers": {"domain", "extraction", "importers"},
        "storage": {"domain", "contracts", "artifacts", "storage"},
        "runtime": {
            "domain",
            "contracts",
            "artifacts",
            "extraction",
            "storage",
            "runtime",
            "model",
            "checks",
        },
        "cli": {"domain", "contracts", "artifacts", "extraction", "storage", "runtime"},
    }
    for owner, dependencies in allowed.items():
        location = ROOT / "src" / "jbt" / owner
        paths = (
            location.rglob("*.py")
            if location.is_dir()
            else (location.with_suffix(".py"),)
        )
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text())):
                imports = []
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 0, (path.name, "relative import")
                    if node.module is not None:
                        imports = [node.module]
                for imported in imports:
                    assert imported != "tests"
                    assert not imported.startswith("tests.")
                    if imported.startswith("jbt."):
                        assert imported.split(".")[1] in dependencies, (
                            path.name,
                            imported,
                        )


@pytest.mark.parametrize("owner", ["domain", "model", "checks"])
def test_core_imports_only_pure_standard_library_and_owning_modules(
    owner: str,
) -> None:
    forbidden = {
        "os",
        "pathlib",
        "subprocess",
        "socket",
        "urllib",
        "http",
        "importlib",
        "tempfile",
        "shutil",
        "pickle",
        "ctypes",
        "multiprocessing",
    }
    for path in (ROOT / "src" / "jbt" / owner).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imports: list[str] = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports = [node.module]
            for imported in imports:
                top = imported.split(".")[0]
                assert top not in forbidden, (path.name, imported)
                assert (
                    imported.startswith(("jbt.domain.", f"jbt.{owner}."))
                    or top in sys.stdlib_module_names
                ), (path.name, imported)


def test_independent_consumer_has_no_producer_import() -> None:
    paths = [
        ROOT / "tests/integration/conformance/consumer_arithmetic.py",
        ROOT / "tests/integration/conformance/reader.py",
    ]
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name != "jbt" and not alias.name.startswith("jbt.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "jbt"
                assert node.module is None or not node.module.startswith("jbt.")
