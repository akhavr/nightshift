"""Tests for host/merge.py — merge execution and conflict validation."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from host.merge import check_branch_not_behind_base


def _clean_git_env():
    """Return env dict without GIT_DIR/GIT_WORK_TREE."""
    import os
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear GIT_DIR/GIT_WORK_TREE so subprocess calls use the temp repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)


class TestMergeSanitizesConfig:
    """Tests that merge functions sanitize git config first."""

    def test_merge_sanitizes_config_first(self, tmp_path):
        """check_branch_not_behind_base sanitizes core.worktree before git ops."""
        # Set up a git repo with main and agent branches
        repo = tmp_path / "repo"
        repo.mkdir()
        _run = lambda *args: subprocess.run(
            args, cwd=str(repo), capture_output=True, text=True, env=_clean_git_env()
        )

        _run("git", "init")
        _run("git", "config", "user.email", "test@test.com")
        _run("git", "config", "user.name", "Test")
        (repo / "file.txt").write_text("initial")
        _run("git", "add", ".")
        _run("git", "commit", "-m", "initial")
        _run("git", "checkout", "-b", "main")
        _run("git", "checkout", "-b", "agent/test")
        (repo / "new.txt").write_text("agent work")
        _run("git", "add", ".")
        _run("git", "commit", "-m", "agent commit")
        _run("git", "checkout", "main")

        # Set core.worktree to container path (simulates container corruption)
        _run("git", "config", "core.worktree", "/workspace")

        # Verify it was set
        result = _run("git", "config", "--get", "core.worktree")
        assert result.stdout.strip() == "/workspace"

        # Call the function that should sanitize
        behind_msg = check_branch_not_behind_base(repo, "agent/test", "main")
        assert behind_msg is None  # branches are in sync

        # Verify core.worktree was sanitized
        result = _run("git", "config", "--get", "core.worktree")
        assert result.returncode != 0  # config key not found
