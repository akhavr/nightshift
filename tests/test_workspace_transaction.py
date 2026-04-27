"""Tests for core/workspace_transaction.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.workspace_transaction import (
    WorktreeCorruptError,
    check_worktree_integrity,
)


def _make_worktree(tmp_path: Path, name: str = "agent-abc123") -> Path:
    """Create a minimal git worktree layout for integrity tests."""
    repo_root = tmp_path / "repo"
    metadata_dir = repo_root / ".git" / "worktrees" / name
    metadata_dir.mkdir(parents=True)

    worktree = tmp_path / name
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {metadata_dir}\n")
    return worktree


def test_check_worktree_integrity_valid(tmp_path):
    """A healthy worktree passes integrity checks."""
    worktree = _make_worktree(tmp_path)

    assert check_worktree_integrity(worktree) is True


def test_check_worktree_integrity_missing_metadata(tmp_path):
    """Missing worktree metadata raises a corruption error."""
    worktree = _make_worktree(tmp_path)
    metadata_dir = tmp_path / "repo" / ".git" / "worktrees" / worktree.name

    metadata_dir.rmdir()

    with pytest.raises(WorktreeCorruptError):
        check_worktree_integrity(worktree)


def test_check_worktree_integrity_missing_git_file(tmp_path):
    """A worktree without a .git file is rejected immediately."""
    worktree = tmp_path / "agent-abc123"
    worktree.mkdir()

    with pytest.raises(WorktreeCorruptError):
        check_worktree_integrity(worktree)


def test_check_worktree_integrity_auto_repair(tmp_path):
    """Auto-repair shells out to git worktree repair when metadata is missing."""
    worktree = _make_worktree(tmp_path)
    metadata_dir = tmp_path / "repo" / ".git" / "worktrees" / worktree.name
    metadata_dir.rmdir()

    with patch("core.workspace_transaction.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert check_worktree_integrity(worktree, auto_repair=True) is True

    mock_run.assert_called_once_with(
        ["git", "worktree", "repair"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
