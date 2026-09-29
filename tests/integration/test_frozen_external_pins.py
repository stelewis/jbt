"""Integration tests for frozen external automation refs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ACTION_LINE_PATTERN = re.compile(
    r"^[^#\n]*uses:\s*[^@\s#]+@[a-f0-9]{40}\s+#\s+[^#\n]+\s*$"
)
PRE_COMMIT_REV_PATTERN = re.compile(r"^[^#\n]*rev:\s*[a-f0-9]{40}\s+# frozen: .+\s*$")


def _workflow_files(*, repo_root: Path) -> list[Path]:
    """Return workflow and composite action files."""
    patterns = (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        ".github/actions/**/action.yml",
        ".github/actions/**/action.yaml",
    )
    files: list[Path] = []
    for pattern in patterns:
        files.extend(repo_root.glob(pattern))
    return sorted(set(files))


def _iter_external_action_lines(*, repo_root: Path) -> list[tuple[Path, str]]:
    """Return external GitHub Action `uses:` lines."""
    external_lines: list[tuple[Path, str]] = []
    for file_path in _workflow_files(repo_root=repo_root):
        for line in file_path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip(" -")
            if not stripped.startswith("uses:"):
                continue

            ref = stripped.removeprefix("uses:").strip()
            if ref.startswith(("./", "docker://")):
                continue

            external_lines.append((file_path, line.rstrip()))
    return external_lines


def _load_pre_commit_config(*, repo_root: Path) -> dict[str, Any]:
    """Load the repository pre-commit config."""
    config_path = repo_root / ".pre-commit-config.yaml"
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = f"Expected mapping in {config_path}"
        raise TypeError(msg)
    return loaded


@pytest.mark.integration
def test_external_actions_use_frozen_shas_with_version_comments() -> None:
    """Require external GitHub Action refs to use pinned SHAs and version comments."""
    repo_root = Path(__file__).resolve().parents[2]
    violations = [
        f"{file_path}: {line}"
        for file_path, line in _iter_external_action_lines(repo_root=repo_root)
        if not ACTION_LINE_PATTERN.match(line)
    ]

    assert not violations, "Invalid external action refs:\n" + "\n".join(violations)


@pytest.mark.integration
def test_external_pre_commit_hooks_use_frozen_shas() -> None:
    """Require external pre-commit hooks to use frozen SHAs."""
    repo_root = Path(__file__).resolve().parents[2]
    config = _load_pre_commit_config(repo_root=repo_root)
    repos = config.get("repos")
    if not isinstance(repos, list):
        msg = "Expected 'repos' to be a list in .pre-commit-config.yaml"
        raise TypeError(msg)

    config_lines = (
        (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8").splitlines()
    )

    for repo_entry in repos:
        if not isinstance(repo_entry, dict):
            continue

        repo = repo_entry.get("repo")
        if not isinstance(repo, str) or repo == "local":
            continue

        rev = repo_entry.get("rev")
        assert isinstance(rev, str)
        assert re.fullmatch(r"[a-f0-9]{40}", rev), repo

        matching_lines = [
            line for line in config_lines if line.lstrip().startswith(f"rev: {rev}")
        ]
        assert matching_lines, f"No rev line found for {repo}"
        assert PRE_COMMIT_REV_PATTERN.match(matching_lines[0]), matching_lines[0]
