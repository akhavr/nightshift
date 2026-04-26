"""Tests for CommandExecutor: CLI command execution and launch failure revert."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.protocols import TrackerComment
from tests.watcher.conftest import _make_watcher, _make_session


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
