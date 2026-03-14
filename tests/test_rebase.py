"""Tests for core/rebase.py — pre-review rebase logic."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.protocols import RebaseResult, Workspace
from core.rebase import (
    attempt_pre_review_rebase,
    _run_test_command,
    _build_rebase_conflict_prompt,
    _build_test_failure_prompt,
    REBASE_TIMEOUT_S,
)
from tests.conftest import MockWorkspaceManager


class TestAttemptPreReviewRebase:
    def test_success_no_tests(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws = Workspace(path=tmp_path, branch="agent/test")
        result = attempt_pre_review_rebase(ws_mgr, ws, "master")
        assert result is None
        assert len(ws_mgr.rebase_calls) == 1
        assert ws_mgr.rebase_calls[0] == (tmp_path, "master")

    def test_success_with_passing_tests(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws = Workspace(path=tmp_path, branch="agent/test")
        with patch("core.rebase._run_test_command", return_value=None):
            result = attempt_pre_review_rebase(
                ws_mgr, ws, "master", test_command="pytest")
        assert result is None

    def test_rebase_conflict_returns_prompt(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws_mgr.rebase_result = RebaseResult(
            success=False,
            conflict_details="Conflicting files:\nsrc/main.py")
        ws = Workspace(path=tmp_path, branch="agent/test")
        result = attempt_pre_review_rebase(ws_mgr, ws, "master")
        assert result is not None
        assert "REBASE CONFLICT" in result
        assert "src/main.py" in result
        assert "@@DONE@@" in result

    def test_test_failure_returns_prompt(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws = Workspace(path=tmp_path, branch="agent/test")
        with patch("core.rebase._run_test_command",
                    return_value="Exit code 1\nstdout:\nFAILED test_foo"):
            result = attempt_pre_review_rebase(
                ws_mgr, ws, "master", test_command="pytest")
        assert result is not None
        assert "POST-REBASE TEST FAILURE" in result
        assert "FAILED test_foo" in result

    def test_no_workspace_skips(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        result = attempt_pre_review_rebase(ws_mgr, None, "master")
        assert result is None
        assert len(ws_mgr.rebase_calls) == 0

    def test_no_test_command_skips_tests(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws = Workspace(path=tmp_path, branch="agent/test")
        with patch("core.rebase._run_test_command") as mock_test:
            result = attempt_pre_review_rebase(
                ws_mgr, ws, "master", test_command=None)
        mock_test.assert_not_called()
        assert result is None

    def test_custom_base_branch(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        ws = Workspace(path=tmp_path, branch="agent/test")
        attempt_pre_review_rebase(ws_mgr, ws, "main")
        assert ws_mgr.rebase_calls[0] == (tmp_path, "main")


class TestRunTestCommand:
    def test_passing_returns_none(self, tmp_path):
        result = _run_test_command(tmp_path, "true")
        assert result is None

    def test_failing_returns_output(self, tmp_path):
        result = _run_test_command(tmp_path, "echo 'FAIL' && exit 1")
        assert result is not None
        assert "Exit code 1" in result
        assert "FAIL" in result

    def test_timeout_returns_message(self, tmp_path):
        with patch("core.rebase.REBASE_TIMEOUT_S", 0):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 0)):
                result = _run_test_command(tmp_path, "sleep 999")
        assert result is not None
        assert "timed out" in result

    def test_exception_returns_message(self, tmp_path):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = _run_test_command(tmp_path, "nonexistent")
        assert result is not None
        assert "error" in result.lower()


class TestBuildPrompts:
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
