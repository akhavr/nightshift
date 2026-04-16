"""Tests for review container launch failure handling.

Addresses the bug where review container launch fails silently:
- No session directory created
- No error logged
- Coder session stuck in 'reviewing' status

The core issue is that _launch_background doesn't signal failure
to callers, so maybe_launch_review doesn't know the launch failed.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from tests.watcher.conftest import _make_watcher, _make_session


class TestReviewLaunchFailure:
    """Test handling of review container launch failures."""

    def test_launch_background_failure_reverts_coder_status(self, tmp_path):
        """When _launch_background fails, coder should revert to waiting:review.

        This is the key bug: maybe_launch_review changes coder status to
        'reviewing' BEFORE calling _launch_background. If _launch_background
        fails, the coder is stuck in 'reviewing' with no review running.
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text(
            "---\nagent:\n  kind: claude-code\n---\nReview\n"
        )
        coder_dir = _make_session(
            w.sessions_dir, "abc123", status="waiting:review", issue_id="issue-abc123"
        )

        # Simulate Popen raising an exception
        def failing_popen(*args, **kwargs):
            raise OSError("Command not found")

        with patch("subprocess.Popen", failing_popen):
            w.reviews.check_for_auto_review()

        # The coder session should NOT be stuck in 'reviewing'
        state = json.loads((coder_dir / "state.json").read_text())
        # Currently this fails because maybe_launch_review doesn't handle failure
        assert state["status"] == "waiting:review", (
            f"Coder session stuck in '{state['status']}' after launch failure"
        )

    def test_launch_background_failure_does_not_track_review_session(self, tmp_path):
        """When _launch_background fails, review_sid should not be in _recently_launched.

        If the launch failed, we shouldn't track it as 'recently launched' because
        that would prevent orphan detection from noticing something is wrong.
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text(
            "---\nagent:\n  kind: claude-code\n---\nReview\n"
        )
        _make_session(
            w.sessions_dir, "abc123", status="waiting:review", issue_id="issue-abc123"
        )

        # Simulate Popen raising an exception
        def failing_popen(*args, **kwargs):
            raise OSError("Command not found")

        with patch("subprocess.Popen", failing_popen):
            w.reviews.check_for_auto_review()

        # review-abc123 should not be in _recently_launched after failure
        assert "review-abc123" not in w._recently_launched, (
            "Failed launch should not be tracked in _recently_launched"
        )

    def test_launch_background_returns_failure_on_popen_exception(self, tmp_path):
        """_launch_background should return False when Popen fails."""
        w = _make_watcher(tmp_path)

        def failing_popen(*args, **kwargs):
            raise OSError("Command not found")

        with patch("subprocess.Popen", failing_popen):
            result = w._launch_background(["fake-cmd"], "test-sid")

        # Currently _launch_background doesn't return anything
        # After the fix, it should return False on failure
        assert result is False, "_launch_background should return False on failure"

    def test_launch_background_returns_success_on_successful_launch(self, tmp_path):
        """_launch_background should return True when Popen succeeds."""
        w = _make_watcher(tmp_path)
        # Ensure log file parent exists
        (w.sessions_dir.parent / "watcher.log").parent.mkdir(parents=True, exist_ok=True)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running

        with patch("subprocess.Popen", return_value=mock_proc):
            result = w._launch_background(["echo", "test"], "test-sid")

        # After the fix, it should return True on success
        assert result is True, "_launch_background should return True on success"

    def test_launch_background_cleans_up_on_failure(self, tmp_path):
        """When _launch_background fails, it should not leave dangling state."""
        w = _make_watcher(tmp_path)

        def failing_popen(*args, **kwargs):
            raise OSError("Command not found")

        with patch("subprocess.Popen", failing_popen):
            w._launch_background(["fake-cmd"], "test-sid")

        # The failed launch should not be in _background_procs
        assert "test-sid" not in w._background_procs, (
            "Failed launch should not be tracked in _background_procs"
        )


class TestReviewLaunchSubprocessFailure:
    """Test handling when subprocess starts but then fails."""

    def test_subprocess_immediate_failure_logs_error(self, tmp_path, caplog):
        """When subprocess exits immediately with error, it should be logged."""
        import logging
        caplog.set_level(logging.INFO)

        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text(
            "---\nagent:\n  kind: claude-code\n---\nReview\n"
        )
        _make_session(
            w.sessions_dir, "abc123", status="waiting:review", issue_id="issue-abc123"
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited with error immediately

        with patch("subprocess.Popen", return_value=mock_proc):
            w.reviews.check_for_auto_review()
            # Simulate time passing and check_background_launches running
            w.check_background_launches()

        # Error should be logged
        assert any("launch failed" in record.message.lower() for record in caplog.records), (
            "Launch failure should be logged"
        )

    def test_subprocess_failure_reverts_coder_to_waiting_review(self, tmp_path):
        """When review subprocess fails, coder should revert to waiting:review."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text(
            "---\nagent:\n  kind: claude-code\n---\nReview\n"
        )
        coder_dir = _make_session(
            w.sessions_dir, "abc123", status="waiting:review", issue_id="issue-abc123"
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited with error

        with patch("subprocess.Popen", return_value=mock_proc):
            w.reviews.check_for_auto_review()
            # Simulate watcher loop iteration
            w.check_background_launches()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:review", (
            f"Coder should be reverted to waiting:review, not '{state['status']}'"
        )
