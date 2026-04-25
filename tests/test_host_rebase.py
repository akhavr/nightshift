"""Tests for host/rebase.py — host-side pre-review rebase."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from host.rebase import (
    attempt_pre_review_rebase,
    _rebase,
    _run_test_command,
    _build_rebase_conflict_prompt,
    _build_test_failure_prompt,
    RebaseResult,
    TEST_COMMAND_TIMEOUT_S,
)


class TestAttemptPreReviewRebase:
    """Tests for the main rebase entry point."""

    def test_success_no_tests(self, tmp_path):
        """Successful rebase with no test command returns None."""
        with patch("host.rebase._rebase") as mock_rebase:
            mock_rebase.return_value = RebaseResult(success=True)
            result = attempt_pre_review_rebase(tmp_path, "master")
        assert result is None
        mock_rebase.assert_called_once_with(tmp_path, "master")

    def test_success_with_passing_tests(self, tmp_path):
        """Successful rebase + passing tests returns None."""
        with patch("host.rebase._rebase") as mock_rebase, \
             patch("host.rebase._run_test_command", return_value=None):
            mock_rebase.return_value = RebaseResult(success=True)
            result = attempt_pre_review_rebase(
                tmp_path, "master", test_command="pytest")
        assert result is None

    def test_rebase_conflict_returns_prompt(self, tmp_path):
        """Rebase conflict returns a prompt for the agent."""
        with patch("host.rebase._rebase") as mock_rebase:
            mock_rebase.return_value = RebaseResult(
                success=False,
                conflict_details="Conflicting files:\nsrc/main.py")
            result = attempt_pre_review_rebase(tmp_path, "master")
        assert result is not None
        assert "REBASE CONFLICT" in result
        assert "src/main.py" in result
        assert "@@DONE@@" in result

    def test_test_failure_returns_prompt(self, tmp_path):
        """Test failure after rebase returns a prompt."""
        with patch("host.rebase._rebase") as mock_rebase, \
             patch("host.rebase._run_test_command",
                   return_value="Exit code 1\nstdout:\nFAILED test_foo"):
            mock_rebase.return_value = RebaseResult(success=True)
            result = attempt_pre_review_rebase(
                tmp_path, "master", test_command="pytest")
        assert result is not None
        assert "POST-REBASE TEST FAILURE" in result
        assert "FAILED test_foo" in result

    def test_nonexistent_worktree_skips(self, tmp_path):
        """Missing worktree returns None (skip rebase)."""
        result = attempt_pre_review_rebase(
            tmp_path / "nonexistent", "master")
        assert result is None

    def test_no_test_command_skips_tests(self, tmp_path):
        """When no test_command is provided, tests are skipped."""
        with patch("host.rebase._rebase") as mock_rebase, \
             patch("host.rebase._run_test_command") as mock_test:
            mock_rebase.return_value = RebaseResult(success=True)
            attempt_pre_review_rebase(tmp_path, "master", test_command=None)
        mock_test.assert_not_called()

    def test_custom_base_branch(self, tmp_path):
        """Rebase uses the provided base branch."""
        with patch("host.rebase._rebase") as mock_rebase:
            mock_rebase.return_value = RebaseResult(success=True)
            attempt_pre_review_rebase(tmp_path, "main")
        mock_rebase.assert_called_once_with(tmp_path, "main")


class TestRebase:
    """Tests for the _rebase function."""

    def test_success_with_remote_fetch(self, tmp_path):
        """Successful rebase after fetching from origin."""
        with patch("subprocess.run") as mock_run:
            # Fetch succeeds
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="No local changes to save"),  # stash
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=0),  # rebase
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Should have called stash, fetch, then rebase with origin/master
        calls = mock_run.call_args_list
        assert "stash" in str(calls[0])
        assert "fetch" in str(calls[1])
        assert "rebase" in str(calls[2])
        assert "origin/master" in str(calls[2])

    def test_success_without_remote(self, tmp_path):
        """Successful rebase using local branch when remote fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="No local changes to save"),  # stash
                MagicMock(returncode=1),  # fetch fails
                MagicMock(returncode=0),  # rebase succeeds
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Should rebase onto local master (not origin/master)
        rebase_call = mock_run.call_args_list[2]
        assert "master" in str(rebase_call)
        assert "origin/master" not in str(rebase_call)

    def test_conflict_returns_details(self, tmp_path):
        """Rebase conflict returns details."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="No local changes to save"),  # stash
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=1, stderr="CONFLICT"),  # rebase fails
                MagicMock(stdout="file1.py\nfile2.py"),  # diff conflicting files
                MagicMock(returncode=0),  # rebase --abort
            ]
            result = _rebase(tmp_path, "master")
        assert not result.success
        assert "CONFLICT" in result.conflict_details
        assert "file1.py" in result.conflict_details

    def test_rebase_abort_called_on_conflict(self, tmp_path):
        """On conflict, rebase --abort is called to restore clean state."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="No local changes to save"),  # stash
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=1, stderr="CONFLICT"),  # rebase fails
                MagicMock(stdout=""),  # diff
                MagicMock(returncode=0),  # rebase --abort
            ]
            _rebase(tmp_path, "master")
        # Last call should be rebase --abort
        last_call = mock_run.call_args_list[-1]
        assert "--abort" in str(last_call)

    def test_rebase_stashes_uncommitted_changes(self, tmp_path):
        """Uncommitted changes are stashed before rebase."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Saved working directory"),  # stash (had changes)
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=0),  # rebase
                MagicMock(returncode=0),  # stash pop
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Verify stash was called with correct args
        stash_call = mock_run.call_args_list[0]
        assert "stash" in str(stash_call)
        assert "--include-untracked" in str(stash_call)
        assert "pre-rebase-stash" in str(stash_call)

    def test_rebase_pops_stash_after_success(self, tmp_path):
        """Stash is popped after successful rebase."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Saved working directory"),  # stash (had changes)
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=0),  # rebase
                MagicMock(returncode=0),  # stash pop
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Last call should be stash pop
        last_call = mock_run.call_args_list[-1]
        assert "stash" in str(last_call)
        assert "pop" in str(last_call)

    def test_rebase_pops_stash_after_conflict(self, tmp_path):
        """Stash is restored after rebase conflict and abort."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Saved working directory"),  # stash (had changes)
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=1, stderr="CONFLICT"),  # rebase fails
                MagicMock(stdout="file.py"),  # diff conflicting files
                MagicMock(returncode=0),  # rebase --abort
                MagicMock(returncode=0),  # stash pop
            ]
            result = _rebase(tmp_path, "master")
        assert not result.success
        # Last call should be stash pop
        last_call = mock_run.call_args_list[-1]
        assert "stash" in str(last_call)
        assert "pop" in str(last_call)


class TestRunTestCommand:
    """Tests for the _run_test_command function."""

    def test_passing_returns_none(self, tmp_path):
        result = _run_test_command(tmp_path, "true")
        assert result is None

    def test_failing_returns_output(self, tmp_path):
        result = _run_test_command(tmp_path, "echo 'FAIL' && exit 1")
        assert result is not None
        assert "Exit code 1" in result
        assert "FAIL" in result

    def test_timeout_returns_message(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 0)):
            result = _run_test_command(tmp_path, "sleep 999")
        assert result is not None
        assert "timed out" in result

    def test_exception_returns_message(self, tmp_path):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = _run_test_command(tmp_path, "nonexistent")
        assert result is not None
        assert "error" in result.lower()

    def test_uses_configured_timeout(self, tmp_path):
        """_run_test_command uses timeout from parameter."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            _run_test_command(tmp_path, "true", timeout_s=300)
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 300

    def test_truncates_long_output(self, tmp_path):
        """Long output is truncated."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = "x" * 3000
            mock_run.return_value.stderr = "e" * 2000
            result = _run_test_command(tmp_path, "fail")
        # Should be truncated
        assert len(result) < 5000


class TestBuildPrompts:
    """Tests for prompt building functions."""

    def test_conflict_prompt_includes_branch(self):
        result = RebaseResult(success=False, conflict_details="conflicts here")
        prompt = _build_rebase_conflict_prompt("master", result)
        assert "master" in prompt
        assert "conflicts here" in prompt
        assert "@@DONE@@" in prompt

    def test_test_failure_prompt_includes_output(self):
        prompt = _build_test_failure_prompt("master", "test output here")
        assert "master" in prompt
        assert "test output here" in prompt
        assert "@@DONE@@" in prompt


class TestRebaseResult:
    """Tests for the RebaseResult class."""

    def test_success_result(self):
        result = RebaseResult(success=True)
        assert result.success
        assert result.conflict_details == ""

    def test_failure_result(self):
        result = RebaseResult(success=False, conflict_details="details")
        assert not result.success
        assert result.conflict_details == "details"


class TestFixContainerGitdir:
    """Tests for _fix_container_gitdir."""

    def test_rebase_fixes_container_gitdir(self, tmp_path):
        """Container gitdir path in .git file is fixed to host path before rebase."""
        from host.rebase import _fix_container_gitdir

        # Create worktree with container gitdir path
        worktree = tmp_path / "agent-abc123"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text("gitdir: /repo-git/worktrees/agent-abc123\n")

        # Create the repo root where .git/worktrees should exist
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git" / "worktrees" / "agent-abc123").mkdir(parents=True)

        _fix_container_gitdir(worktree, repo_root)

        # Should be rewritten to host path
        content = git_file.read_text()
        assert "/repo-git/" not in content
        expected = str(repo_root / ".git" / "worktrees" / "agent-abc123")
        assert f"gitdir: {expected}" in content

    def test_rebase_preserves_valid_gitdir(self, tmp_path):
        """Valid host gitdir path is not modified."""
        from host.rebase import _fix_container_gitdir

        worktree = tmp_path / "agent-xyz789"
        worktree.mkdir()
        git_file = worktree / ".git"
        host_path = "/home/user/repo/.git/worktrees/agent-xyz789"
        git_file.write_text(f"gitdir: {host_path}\n")

        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        _fix_container_gitdir(worktree, repo_root)

        # Should be unchanged
        content = git_file.read_text()
        assert f"gitdir: {host_path}" in content

    def test_rebase_handles_missing_git_file(self, tmp_path):
        """Missing .git file doesn't cause an error."""
        from host.rebase import _fix_container_gitdir

        worktree = tmp_path / "agent-nofile"
        worktree.mkdir()
        repo_root = tmp_path / "repo"

        # Should not raise
        _fix_container_gitdir(worktree, repo_root)

    def test_rebase_handles_git_directory(self, tmp_path):
        """Regular git repo (directory .git) is not modified."""
        from host.rebase import _fix_container_gitdir

        worktree = tmp_path / "regular-repo"
        worktree.mkdir()
        (worktree / ".git").mkdir()  # .git is a directory, not a file

        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        # Should not raise
        _fix_container_gitdir(worktree, repo_root)

    def test_attempt_rebase_fixes_gitdir_before_git_ops(self, tmp_path):
        """attempt_pre_review_rebase fixes gitdir before running git commands."""
        # Create worktree with container gitdir path
        worktree = tmp_path / "worktrees" / "agent-fix123"
        worktree.mkdir(parents=True)
        git_file = worktree / ".git"
        git_file.write_text("gitdir: /repo-git/worktrees/agent-fix123\n")

        # Create the expected host gitdir path
        repo_root = tmp_path
        (repo_root / ".git" / "worktrees" / "agent-fix123").mkdir(parents=True)

        with patch("host.rebase._rebase") as mock_rebase:
            mock_rebase.return_value = RebaseResult(success=True)
            attempt_pre_review_rebase(worktree, "master", repo_root=repo_root)

        # Verify gitdir was fixed before rebase was called
        content = git_file.read_text()
        assert "/repo-git/" not in content
        expected = str(repo_root / ".git" / "worktrees" / "agent-fix123")
        assert f"gitdir: {expected}" in content


def _clean_git_env():
    """Return env dict without GIT_DIR/GIT_WORK_TREE (allows tests in temp repos)."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


class TestSanitizeGitConfig:
    """Tests for sanitize_git_config — removes core.worktree=/workspace."""

    def test_sanitize_removes_core_worktree(self, tmp_path, monkeypatch):
        """Container-set core.worktree=/workspace is removed."""
        from host.rebase import sanitize_git_config

        # Clear GIT_DIR/GIT_WORK_TREE so subprocess uses the temp repo
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)
        env = _clean_git_env()

        # Set up a git repo with core.worktree set to container path
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, env=env)
        subprocess.run(
            ["git", "config", "core.worktree", "/workspace"],
            cwd=str(repo), capture_output=True, env=env,
        )

        # Verify it was set
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=str(repo), capture_output=True, text=True, env=env,
        )
        assert result.stdout.strip() == "/workspace"

        # Sanitize
        changed = sanitize_git_config(repo)
        assert changed is True

        # Verify it was removed
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=str(repo), capture_output=True, text=True, env=env,
        )
        assert result.returncode != 0  # config key not found

    def test_sanitize_preserves_valid_config(self, tmp_path, monkeypatch):
        """core.worktree set to non-container path is preserved."""
        from host.rebase import sanitize_git_config

        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)
        env = _clean_git_env()

        # Set up a git repo with core.worktree set to a host path
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, env=env)
        host_path = str(tmp_path / "actual-worktree")
        subprocess.run(
            ["git", "config", "core.worktree", host_path],
            cwd=str(repo), capture_output=True, env=env,
        )

        # Sanitize
        changed = sanitize_git_config(repo)
        assert changed is False

        # Verify it was preserved
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=str(repo), capture_output=True, text=True, env=env,
        )
        assert result.stdout.strip() == host_path

    def test_sanitize_noop_when_not_set(self, tmp_path, monkeypatch):
        """No change when core.worktree is not set."""
        from host.rebase import sanitize_git_config

        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)
        env = _clean_git_env()

        # Set up a plain git repo
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, env=env)

        # Sanitize
        changed = sanitize_git_config(repo)
        assert changed is False

    def test_attempt_rebase_sanitizes_config(self, tmp_path, monkeypatch):
        """attempt_pre_review_rebase calls sanitize_git_config."""
        from host.rebase import sanitize_git_config

        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)
        env = _clean_git_env()

        # Set up repo and worktree
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, env=env)
        subprocess.run(
            ["git", "config", "core.worktree", "/workspace"],
            cwd=str(repo), capture_output=True, env=env,
        )

        worktree = tmp_path / "worktrees" / "agent-test"
        worktree.mkdir(parents=True)

        with patch("host.rebase._rebase") as mock_rebase, \
             patch("host.rebase._fix_container_gitdir"):
            mock_rebase.return_value = RebaseResult(success=True)
            attempt_pre_review_rebase(worktree, "master", repo_root=repo)

        # Verify config was sanitized
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=str(repo), capture_output=True, text=True, env=env,
        )
        assert result.returncode != 0  # config key not found
