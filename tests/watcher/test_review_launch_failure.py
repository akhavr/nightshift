"""Tests for review container launch failure handling.

Verifies that when _launch_background() fails (returns False), the coder session
status is reverted from 'reviewing' back to 'waiting:review' so auto-review can retry.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.watcher.conftest import _make_watcher, _make_session


class TestReviewLaunchFailure:
    """Test that launch failures are detected and handled."""

    def test_launch_background_failure_reverts_coder_status(self, tmp_path):
        """When _launch_background returns False, coder status should revert."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        # Make launch fail
        w.reviews._launch_background = lambda cmd, sid: False

        w.reviews.check_for_auto_review()

        # Coder should be back to waiting:review
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:review"

    def test_launch_background_success_keeps_reviewing_status(self, tmp_path):
        """When _launch_background returns True, coder stays in reviewing."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.check_for_auto_review()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "reviewing"
        assert "review-abc" in launched

    def test_launch_failure_does_not_increment_rounds(self, tmp_path):
        """When launch fails, round count should not increase."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        w.reviews._launch_background = lambda cmd, sid: False

        w.reviews.maybe_launch_review("abc", coder_dir, "issue-abc", w.repo_dir / "REVIEW.md")

        # Rounds should not have been incremented
        assert w.reviews._rounds.get("abc", 0) == 0

    def test_launch_failure_does_not_add_to_recently_launched(self, tmp_path):
        """When launch fails, session should not be marked as recently launched."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        w.reviews._launch_background = lambda cmd, sid: False

        w.reviews.check_for_auto_review()

        # Should NOT be in recently_launched
        assert "review-abc" not in w._recently_launched

    def test_launch_success_adds_to_recently_launched(self, tmp_path):
        """When launch succeeds, session should be marked as recently launched."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        w.reviews._launch_background = lambda cmd, sid: True

        w.reviews.check_for_auto_review()

        # Should be in recently_launched
        assert "review-abc" in w._recently_launched

    def test_failed_review_can_retry_on_next_loop(self, tmp_path):
        """After a failed launch, the next loop iteration should retry."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        # First attempt fails
        w.reviews._launch_background = lambda cmd, sid: False
        w.reviews.check_for_auto_review()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:review"

        # Second attempt succeeds
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.check_for_auto_review()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "reviewing"
        assert "review-abc" in launched

    def test_exception_in_launch_reverts_status(self, tmp_path):
        """If _launch_background raises, status should still be reverted."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        coder_dir = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                  issue_id="issue-abc")

        def raise_error(cmd, sid):
            raise RuntimeError("Popen failed")

        w.reviews._launch_background = raise_error

        # Should not raise, just handle gracefully
        w.reviews.check_for_auto_review()

        # Status should be back to waiting:review
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:review"
