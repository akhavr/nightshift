"""Tests for host/workspace_setup.py — merge_base_into_worktree."""

import subprocess
from pathlib import Path

import pytest

from core.constants import MERGE_NEEDED_FILENAME
from host.workspace_setup import merge_base_into_worktree


def _init_repo(tmp_path):
    """Create a git repo with an initial commit on master."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(
        args, cwd=str(repo), capture_output=True, text=True,
    )
    run("git", "init", "-b", "master")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "initial")
    return repo, run


def _create_worktree(repo, run, branch_name="agent/test123"):
    """Create a worktree branching from master."""
    wt = repo.parent / "worktree"
    run("git", "branch", branch_name, "master")
    run("git", "worktree", "add", str(wt), branch_name)
    return wt


class TestMergeBaseIntoWorktree:

    def test_clean_merge_preserves_master_changes(self, tmp_path):
        """When master advances with non-conflicting changes, merge succeeds."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Advance master with a new file
        run("git", "checkout", "master")
        (repo / "master_change.txt").write_text("from master\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "master advance")
        run("git", "checkout", "-")  # back to previous

        result = merge_base_into_worktree(repo, wt, "master",
                                          session_dir=session_dir)

        assert result == "clean"
        assert (wt / "master_change.txt").exists()
        assert not (session_dir / MERGE_NEEDED_FILENAME).exists()

    def test_conflicting_merge_writes_merge_needed(self, tmp_path):
        """When master and agent both change the same file, conflict is detected."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Agent modifies file.txt in worktree
        wt_run = lambda *args: subprocess.run(
            args, cwd=str(wt), capture_output=True, text=True,
        )
        (wt / "file.txt").write_text("agent version\n")
        wt_run("git", "add", ".")
        wt_run("git", "commit", "-m", "agent change")

        # Master modifies same file differently
        run("git", "checkout", "master")
        (repo / "file.txt").write_text("master version\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "master change")
        run("git", "checkout", "-")

        result = merge_base_into_worktree(repo, wt, "master",
                                          session_dir=session_dir)

        assert result == "conflict"
        merge_needed = session_dir / MERGE_NEEDED_FILENAME
        assert merge_needed.exists()
        content = merge_needed.read_text()
        assert "merge_target:" in content
        assert "base_branch: master" in content

    def test_noop_when_base_has_not_advanced(self, tmp_path):
        """When master hasn't changed since branching, returns noop."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        result = merge_base_into_worktree(repo, wt, "master",
                                          session_dir=session_dir)

        assert result == "noop"
        assert not (session_dir / MERGE_NEEDED_FILENAME).exists()

    def test_conflict_without_session_dir(self, tmp_path):
        """When session_dir is None, no merge-needed.txt is written but status returned."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)

        # Create conflicting changes
        wt_run = lambda *args: subprocess.run(
            args, cwd=str(wt), capture_output=True, text=True,
        )
        (wt / "file.txt").write_text("agent version\n")
        wt_run("git", "add", ".")
        wt_run("git", "commit", "-m", "agent change")

        run("git", "checkout", "master")
        (repo / "file.txt").write_text("master version\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "master change")
        run("git", "checkout", "-")

        result = merge_base_into_worktree(repo, wt, "master")

        assert result == "conflict"

    def test_worktree_state_clean_after_conflict(self, tmp_path):
        """After a conflict, the worktree should be in a clean state (merge aborted)."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        wt_run = lambda *args: subprocess.run(
            args, cwd=str(wt), capture_output=True, text=True,
        )
        (wt / "file.txt").write_text("agent version\n")
        wt_run("git", "add", ".")
        wt_run("git", "commit", "-m", "agent change")

        run("git", "checkout", "master")
        (repo / "file.txt").write_text("master version\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "master change")
        run("git", "checkout", "-")

        merge_base_into_worktree(repo, wt, "master", session_dir=session_dir)

        # Worktree should be clean (no merge in progress)
        status = wt_run("git", "status", "--porcelain")
        assert status.stdout.strip() == ""

    def test_noop_when_agent_already_includes_base(self, tmp_path):
        """If agent branch already contains all base commits, returns noop."""
        repo, run = _init_repo(tmp_path)
        wt = _create_worktree(repo, run)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Agent makes a commit but master doesn't advance
        wt_run = lambda *args: subprocess.run(
            args, cwd=str(wt), capture_output=True, text=True,
        )
        (wt / "agent_file.txt").write_text("new work\n")
        wt_run("git", "add", ".")
        wt_run("git", "commit", "-m", "agent work")

        result = merge_base_into_worktree(repo, wt, "master",
                                          session_dir=session_dir)

        assert result == "noop"
