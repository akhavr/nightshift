"""Tests for host/rebase.py — host-side pre-review rebase."""

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
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=0),  # rebase
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Should have called fetch then rebase with origin/master
        calls = mock_run.call_args_list
        assert "fetch" in str(calls[0])
        assert "rebase" in str(calls[1])
        assert "origin/master" in str(calls[1])

    def test_success_without_remote(self, tmp_path):
        """Successful rebase using local branch when remote fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # fetch fails
                MagicMock(returncode=0),  # rebase succeeds
            ]
            result = _rebase(tmp_path, "master")
        assert result.success
        # Should rebase onto local master (not origin/master)
        rebase_call = mock_run.call_args_list[1]
        assert "master" in str(rebase_call)
        assert "origin/master" not in str(rebase_call)

    def test_conflict_returns_details(self, tmp_path):
        """Rebase conflict returns details."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
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
                MagicMock(returncode=0),  # fetch
                MagicMock(returncode=1, stderr="CONFLICT"),  # rebase fails
                MagicMock(stdout=""),  # diff
                MagicMock(returncode=0),  # rebase --abort
            ]
            _rebase(tmp_path, "master")
        # Last call should be rebase --abort
        last_call = mock_run.call_args_list[-1]
        assert "--abort" in str(last_call)


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
