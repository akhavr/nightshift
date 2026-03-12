"""Tests for core/hooks.py — hook execution."""

from pathlib import Path

from core.hooks import run_hook
from core.protocols import Workspace
from tests.conftest import MockWorkspaceManager


class TestRunHook:
    def test_returns_true_when_no_script(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        assert run_hook(ws_mgr, tmp_path, None, "test") is True

    def test_returns_true_when_no_workspace(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        assert run_hook(ws_mgr, None, "echo hi", "test") is True

    def test_delegates_to_workspace_mgr_run_hook(self, tmp_path):
        ws_mgr = MockWorkspaceManager(tmp_path)
        calls = []
        ws_mgr.run_hook = lambda path, script, timeout: (calls.append(script), True)[1]
        result = run_hook(ws_mgr, tmp_path, "echo hello", "test_hook", timeout_s=30)
        assert result is True
        assert "echo hello" in calls

    def test_subprocess_fallback_when_no_run_hook(self, tmp_path):
        """Falls back to subprocess when workspace_mgr has no run_hook."""
        class MinimalWsMgr:
            pass

        result = run_hook(MinimalWsMgr(), tmp_path, "echo hello", "test_hook", timeout_s=10)
        assert result is True

    def test_subprocess_failure_returns_false(self, tmp_path):
        class MinimalWsMgr:
            pass

        result = run_hook(MinimalWsMgr(), tmp_path, "exit 1", "fail_hook", timeout_s=5)
        assert result is False

    def test_fatal_flag_logged_but_still_returns_false(self, tmp_path):
        class MinimalWsMgr:
            pass

        result = run_hook(MinimalWsMgr(), tmp_path, "exit 1", "fail_hook",
                         timeout_s=5, fatal=True)
        assert result is False
