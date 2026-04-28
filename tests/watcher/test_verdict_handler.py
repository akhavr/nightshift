"""Tests for VerdictHandler: reviewer verdict handling and launch failure revert."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.watcher.conftest import _make_watcher, _make_session


class TestHandleReviewerReviseRevert:
    def test_revise_reverts_status_on_launch_failure(self, tmp_path):
        """When _launch_background fails, status should revert to reviewing."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing",
                                  issue_id="issue-abc")
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix the test. @nightshift revise"}) + "\n"
        )

        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        # Simulate launch failure
        w.reviews.verdicts._launch_background = lambda cmd, sid: False

        w.reviews.verdicts.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "reviewing", \
            f"Expected status to revert to reviewing, got {state['status']}"
