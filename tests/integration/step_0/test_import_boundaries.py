import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


def test_package_import_does_not_load_native_adapters() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import jbt, sys; "
                "assert not {'pyarrow', 'duckdb', 'beancount', 'jsonschema'} "
                "& sys.modules.keys()"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_domain_imports_only_pure_standard_library_and_domain() -> None:
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
    for path in (ROOT / "src" / "jbt" / "domain").rglob("*.py"):
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
                    imported.startswith("jbt.domain.") or top in sys.stdlib_module_names
                ), (path.name, imported)


def test_independent_consumer_has_no_producer_import() -> None:
    paths = [
        ROOT / "tests/integration/step_0/consumer_arithmetic.py",
        ROOT / "tests/integration/step_0/reader.py",
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
