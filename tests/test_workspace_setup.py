"""Tests for host/workspace_setup.py — merge_base_into_worktree."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear GIT_DIR/GIT_WORK_TREE so subprocess calls use the temp repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

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


class TestSyncReviewWorktree:
    """Tests for syncing review worktree on resume after coder rebase."""

    def test_sync_review_worktree_updates_to_agent_head(self, tmp_path):
        """When coder rebases, review worktree is synced to new agent HEAD."""
        repo, run = _init_repo(tmp_path)

        # Create agent worktree and make a commit
        agent_wt = _create_worktree(repo, run, "agent/test123")
        agent_run = lambda *args: subprocess.run(
            args, cwd=str(agent_wt), capture_output=True, text=True,
        )
        (agent_wt / "agent_work.txt").write_text("v1\n")
        agent_run("git", "add", ".")
        agent_run("git", "commit", "-m", "agent work v1")
        old_commit = agent_run("git", "rev-parse", "HEAD").stdout.strip()

        # Create review worktree based on agent branch
        review_wt = repo.parent / "review_wt"
        run("git", "branch", "review/test123", "agent/test123")
        run("git", "worktree", "add", str(review_wt), "review/test123")
        review_session = tmp_path / "review_session"
        review_session.mkdir()
        (review_session / "diff.patch").write_text("old diff")
        (review_session / "issue.json").write_text('{"old": "data"}')

        # Coder rebases - adds another commit
        (agent_wt / "agent_work.txt").write_text("v2\n")
        agent_run("git", "add", ".")
        agent_run("git", "commit", "-m", "agent work v2")
        new_commit = agent_run("git", "rev-parse", "HEAD").stdout.strip()
        assert old_commit != new_commit

        # Coder session has updated issue.json
        coder_session = tmp_path / "coder_session"
        coder_session.mkdir()
        (coder_session / "issue.json").write_text('{"new": "data"}')

        from host.workspace_setup import sync_review_worktree

        sync_review_worktree(repo, review_wt, review_session,
                             coder_session, "test123")

        # Review worktree should be at the new commit
        review_run = lambda *args: subprocess.run(
            args, cwd=str(review_wt), capture_output=True, text=True,
        )
        current_commit = review_run("git", "rev-parse", "HEAD").stdout.strip()
        assert current_commit == new_commit

        # diff.patch should be regenerated
        diff = (review_session / "diff.patch").read_text()
        assert "v2" in diff or len(diff) > len("old diff")

        # issue.json should be copied from coder session
        issue_data = (review_session / "issue.json").read_text()
        assert '{"new": "data"}' == issue_data

    def test_sync_review_worktree_when_agent_not_changed(self, tmp_path):
        """When agent hasn't changed, sync is a noop but still succeeds."""
        repo, run = _init_repo(tmp_path)

        agent_wt = _create_worktree(repo, run, "agent/test123")
        agent_run = lambda *args: subprocess.run(
            args, cwd=str(agent_wt), capture_output=True, text=True,
        )
        (agent_wt / "agent_work.txt").write_text("v1\n")
        agent_run("git", "add", ".")
        agent_run("git", "commit", "-m", "agent work v1")
        agent_commit = agent_run("git", "rev-parse", "HEAD").stdout.strip()

        # Create review worktree at same commit
        review_wt = repo.parent / "review_wt"
        run("git", "branch", "review/test123", "agent/test123")
        run("git", "worktree", "add", str(review_wt), "review/test123")
        review_session = tmp_path / "review_session"
        review_session.mkdir()
        (review_session / "diff.patch").write_text("existing diff")

        coder_session = tmp_path / "coder_session"
        coder_session.mkdir()
        (coder_session / "issue.json").write_text('{"data": "same"}')

        from host.workspace_setup import sync_review_worktree

        sync_review_worktree(repo, review_wt, review_session,
                             coder_session, "test123")

        # Should still be at agent commit
        review_run = lambda *args: subprocess.run(
            args, cwd=str(review_wt), capture_output=True, text=True,
        )
        current_commit = review_run("git", "rev-parse", "HEAD").stdout.strip()
        assert current_commit == agent_commit