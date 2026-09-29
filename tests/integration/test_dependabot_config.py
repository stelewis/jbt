"""Integration tests for Dependabot github-actions coverage contracts."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml


def _load_dependabot_config(*, repo_root: Path) -> dict[str, Any]:
    """Load Dependabot configuration from the repository root."""
    dependabot_path = repo_root / ".github" / "dependabot.yml"
    loaded = yaml.safe_load(dependabot_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = f"Expected mapping in {dependabot_path}"
        raise TypeError(msg)
    return loaded


def _github_actions_updates(*, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return github-actions update blocks from Dependabot config."""
    updates = config.get("updates")
    if not isinstance(updates, list):
        msg = "Expected 'updates' to be a list in .github/dependabot.yml"
        raise TypeError(msg)

    return [
        update
        for update in updates
        if isinstance(update, dict)
        and update.get("package-ecosystem") == "github-actions"
    ]


def _collect_directory_patterns(*, updates: list[dict[str, Any]]) -> list[str]:
    """Collect directory and directories patterns from update blocks."""
    configured_patterns: list[str] = []
    for update in updates:
        directory_value = update.get("directory")
        if isinstance(directory_value, str):
            configured_patterns.append(directory_value)

        directories_value = update.get("directories")
        if isinstance(directories_value, list):
            configured_patterns.extend(
                value for value in directories_value if isinstance(value, str)
            )

    return configured_patterns


def _local_action_directories(*, repo_root: Path) -> list[str]:
    """Return local composite action directories in repository-root form."""
    actions_root = repo_root / ".github" / "actions"
    if not actions_root.exists():
        return []

    action_directories = {
        "/" + action_file.parent.relative_to(repo_root).as_posix().rstrip("/")
        for action_file in actions_root.rglob("action.y*ml")
    }
    return sorted(action_directories)


def _local_workflow_files(*, repo_root: Path) -> list[Path]:
    """Return workflow files under .github/workflows."""
    workflows_root = repo_root / ".github" / "workflows"
    if not workflows_root.exists():
        return []
    return sorted(workflows_root.glob("*.y*ml"))


def _uncovered_directories(
    *,
    directories: list[str],
    configured_patterns: list[str],
) -> list[str]:
    """Return directories not matched by Dependabot patterns."""
    return sorted(
        directory
        for directory in directories
        if not any(fnmatch(directory, pattern) for pattern in configured_patterns)
    )


@pytest.mark.integration
def test_dependabot_covers_all_local_github_actions() -> None:
    """Fail when local composite actions are not covered by Dependabot."""
    repo_root = Path(__file__).resolve().parents[2]
    config = _load_dependabot_config(repo_root=repo_root)
    github_actions_updates = _github_actions_updates(config=config)
    configured_patterns = _collect_directory_patterns(updates=github_actions_updates)

    local_action_directories = _local_action_directories(repo_root=repo_root)
    missing_directories = _uncovered_directories(
        directories=local_action_directories,
        configured_patterns=configured_patterns,
    )

    assert not missing_directories, (
        "Dependabot github-actions config does not cover local action "
        "directories: " + ", ".join(missing_directories)
    )


@pytest.mark.integration
def test_dependabot_covers_github_workflow_surface() -> None:
    """Require one github-actions block to cover both workflows and local actions."""
    repo_root = Path(__file__).resolve().parents[2]
    config = _load_dependabot_config(repo_root=repo_root)
    github_actions_updates = _github_actions_updates(config=config)

    assert len(github_actions_updates) == 1

    configured_patterns = _collect_directory_patterns(updates=github_actions_updates)
    workflow_files = _local_workflow_files(repo_root=repo_root)

    if workflow_files:
        assert "/" in configured_patterns

    assert "/.github/actions/*" in configured_patterns
