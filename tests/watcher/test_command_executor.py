"""Tests for CommandExecutor: CLI command execution and launch failure revert."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.protocols import TrackerComment
from tests.watcher.conftest import _make_watcher, _make_session


class TestDoReviseSkipsMissingDir:
    def test_do_revise_skips_missing_session_dir(self, tmp_path):
        """When session_dir doesn't exist, do_revise should skip gracefully.

        Guard added to prevent crashes when the watcher tries to revise
        a session whose directory has been deleted (e.g., by cleanup).
        """
        w = _make_watcher(tmp_path)

        # Point to a non-existent directory
        missing_dir = w.sessions_dir / "nonexistent"
        assert not missing_dir.exists()

        tracker = MagicMock()
        # If the guard fails, this would be called and fail or cause issues
        tracker.get_comments.return_value = [
            TrackerComment(author="human", body="Please fix the bug."),
        ]
        w._tracker = tracker

        # Should not raise, should return early
        w.reviews.commands.do_revise("nonexistent", "issue-abc", missing_dir)

        # Verify tracker was never called (early return before tracker access)
        tracker.get_comments.assert_not_called()


class TestDoReviseRevert:
    def test_cmd_revise_keeps_status_on_launch_failure(self, tmp_path):
        """When _launch_background fails, status should remain unchanged.

        SSM-7: Status is only updated after successful launch to avoid
        invalid SSM transitions on revert.
        """
        w = _make_watcher(tmp_path)
        session_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                    issue_id="issue-abc")

        tracker = MagicMock()
        tracker.get_comments.return_value = [
            TrackerComment(author="human", body="Please fix the bug."),
        ]
        w._tracker = tracker

        # Simulate launch failure
        w.reviews.commands._launch_background = lambda cmd, sid: False

        w.reviews.commands.do_revise("abc", "issue-abc", session_dir)

        state = json.loads((session_dir / "state.json").read_text())
        assert state["status"] == "waiting:review", \
            f"Expected status to remain waiting:review on launch failure, got {state['status']}"

    def test_cmd_revise_keeps_status_on_exception(self, tmp_path):
        """When do_revise raises an exception, status should remain unchanged.

        SSM-7: Status is only updated after successful launch.
        """
        w = _make_watcher(tmp_path)
        session_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                    issue_id="issue-abc")

        # Make tracker.get_comments raise
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker failure")
        w._tracker = tracker

        w.reviews.commands.do_revise("abc", "issue-abc", session_dir)

        state = json.loads((session_dir / "state.json").read_text())
        assert state["status"] == "waiting:review", \
            f"Expected status to remain waiting:review on exception, got {state['status']}"
