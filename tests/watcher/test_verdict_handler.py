"""Tests for VerdictHandler: reviewer verdict handling and launch failure revert."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.state_machine import InvalidTransition
from tests.watcher.conftest import _make_watcher, _make_session


# ---------------------------------------------------------------------------
# SSM-7: Accept/Reject SSM transition tests
# ---------------------------------------------------------------------------

class TestAcceptRejectSSMTransitions:
    """Verify accept/reject use SSM-validated state transitions."""

    def test_accept_transitions_to_accepted(self, tmp_path):
        """Accept must use SSM transition to 'accepted'.

        SSM-7: Valid transitions to accepted:
        - waiting:review -> accepted
        - waiting:human-review -> accepted
        """
        from host.session_utils import update_status

        sd = tmp_path / "sessions" / "abc"
        sd.mkdir(parents=True)
        state = {"issue_id": "issue-abc", "branch": "agent/abc", "status": "waiting:human-review"}
        (sd / "state.json").write_text(json.dumps(state))

        update_status(sd, "accepted")

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "accepted"

    def test_reject_transitions_to_rejected(self, tmp_path):
        """Reject must use SSM transition to 'rejected'.

        SSM-7: Valid transitions to rejected:
        - waiting:review -> rejected
        - waiting:human-review -> rejected
        """
        from host.session_utils import update_status

        sd = tmp_path / "sessions" / "abc"
        sd.mkdir(parents=True)
        state = {"issue_id": "issue-abc", "branch": "agent/abc", "status": "waiting:human-review"}
        (sd / "state.json").write_text(json.dumps(state))

        update_status(sd, "rejected")

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "rejected"

    def test_accept_from_invalid_state_raises(self, tmp_path):
        """Accept from invalid state should raise InvalidTransition."""
        from host.session_utils import update_status

        sd = tmp_path / "sessions" / "abc"
        sd.mkdir(parents=True)
        state = {"issue_id": "issue-abc", "branch": "agent/abc", "status": "working"}
        (sd / "state.json").write_text(json.dumps(state))

        with pytest.raises(InvalidTransition):
            update_status(sd, "accepted")

    def test_reject_from_invalid_state_raises(self, tmp_path):
        """Reject from invalid state should raise InvalidTransition."""
        from host.session_utils import update_status

        sd = tmp_path / "sessions" / "abc"
        sd.mkdir(parents=True)
        state = {"issue_id": "issue-abc", "branch": "agent/abc", "status": "working"}
        (sd / "state.json").write_text(json.dumps(state))

        with pytest.raises(InvalidTransition):
            update_status(sd, "rejected")


class TestHandleReviewerSkipsMissingDir:
    """Tests for guards that skip operations when session directories are missing."""

    def test_handle_reviewer_approve_skips_missing_dir(self, tmp_path):
        """When coder_dir doesn't exist, handle_reviewer_approve should skip gracefully.

        Guard added to prevent crashes when the watcher tries to approve
        a session whose directory has been deleted (e.g., by cleanup).
        """
        w = _make_watcher(tmp_path)

        # Point to a non-existent directory
        missing_dir = w.sessions_dir / "nonexistent"
        assert not missing_dir.exists()

        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()

        # Should not raise, should return early
        w.reviews.verdicts.handle_reviewer_approve("nonexistent", missing_dir, "issue-abc")

        # Verify telegram.notify was never called (early return before notification)
        w.telegram.notify.assert_not_called()

    def test_handle_reviewer_revise_skips_missing_dir(self, tmp_path):
        """When coder_dir doesn't exist, handle_reviewer_revise should skip gracefully.

        Guard added to prevent crashes when the watcher tries to revise
        a session whose directory has been deleted (e.g., by cleanup).
        """
        w = _make_watcher(tmp_path)

        # Point to a non-existent directory
        missing_coder_dir = w.sessions_dir / "nonexistent"
        assert not missing_coder_dir.exists()

        review_dir = tmp_path / "review-nonexistent"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix the test. @nightshift revise"}) + "\n"
        )

        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        launched = []
        w.reviews.verdicts._launch_background = lambda cmd, sid: launched.append(sid) or True

        # Should not raise, should return early
        w.reviews.verdicts.handle_reviewer_revise("nonexistent", missing_coder_dir, "issue-abc", review_dir)

        # Verify nothing was launched (early return before launch)
        assert len(launched) == 0


class TestHandleReviewerReviseRevert:
    def test_revise_keeps_status_on_launch_failure(self, tmp_path):
        """When _launch_background fails, status should remain unchanged (reviewing).

        SSM-7: Status is only updated after successful launch to avoid
        invalid SSM transitions on revert (working -> reviewing is not valid).
        """
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
            f"Expected status to remain reviewing on launch failure, got {state['status']}"


class TestReviseLaunchFailureRecovery:
    """Test that failed revise launches write a marker for later retry."""

    def test_revise_launch_failure_leaves_session_resumable(self, tmp_path):
        """When launch fails, session stays in a state that allows retry (not done:pending-review).

        A marker file (revise-pending.json) is written with enough context for
        the SessionMonitor to retry the revise launch later.
        """
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

        # Verify marker file is written
        marker = coder_dir / "revise-pending.json"
        assert marker.exists(), "revise-pending.json should be written on launch failure"

        marker_data = json.loads(marker.read_text())
        assert marker_data["issue_id"] == "issue-abc"
        assert "review_dir" in marker_data

        # Status should remain in a retryable state
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "reviewing", \
            "Session should remain in reviewing (not done:pending-review)"


class TestReviseResumesCoderSSM11:
    """SSM-11: Verify revise clears completed_at when resuming coder."""

    def test_revise_clears_completed_at(self, tmp_path):
        """When reviewer requests revisions, completed_at should be cleared.

        SSM-11: Sessions in completion states (waiting:review) have completed_at
        set. When resuming for revisions, completed_at must be cleared so the
        orphan detector doesn't treat it as a crashed completed session.
        """
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing",
                                  issue_id="issue-abc")
        # Set completed_at to simulate a session that completed before revise
        state = json.loads((coder_dir / "state.json").read_text())
        state["completed_at"] = "2025-01-01T00:00:00Z"
        (coder_dir / "state.json").write_text(json.dumps(state))

        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix the test. @nightshift revise"}) + "\n"
        )

        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        # Simulate successful launch
        launched = []
        w.reviews.verdicts._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.verdicts.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        # Verify completed_at is cleared
        state = json.loads((coder_dir / "state.json").read_text())
        assert "completed_at" not in state, \
            f"completed_at should be cleared on revise, but found: {state.get('completed_at')}"
        assert state["status"] == "working"
        assert "abc" in launched


class TestFlexibleVerdictExtraction:
    """Test that verdict extraction handles various formats (not just @nightshift)."""

    def test_extracts_bold_verdict(self, tmp_path):
        """Conversation with **APPROVE** (no @nightshift) extracts approve verdict."""
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir(parents=True)

        # Reviewer used **APPROVE** instead of @nightshift approve
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "The code looks good.\n\n**APPROVE**"}) + "\n"
        )

        conv_log = review_dir / "conversation.jsonl"
        verdict = w.reviews.verdicts.extract_reviewer_verdict(conv_log, "issue-abc")
        assert verdict == "approve"

    def test_extracts_bold_reject(self, tmp_path):
        """Conversation with **REJECT** extracts reject verdict."""
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir(parents=True)

        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Issues found:\n- Missing tests\n\n**REJECT**"}) + "\n"
        )

        conv_log = review_dir / "conversation.jsonl"
        verdict = w.reviews.verdicts.extract_reviewer_verdict(conv_log, "issue-abc")
        assert verdict == "reject"

    def test_extracts_verdict_heading_format(self, tmp_path):
        """Conversation with 'Verdict: APPROVE' extracts approve verdict."""
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir(parents=True)

        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "All tests pass.\n\nVerdict: APPROVE"}) + "\n"
        )

        conv_log = review_dir / "conversation.jsonl"
        verdict = w.reviews.verdicts.extract_reviewer_verdict(conv_log, "issue-abc")
        assert verdict == "approve"

    def test_prefers_nightshift_command_over_bold(self, tmp_path):
        """@nightshift command takes precedence when both are present."""
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir(parents=True)

        # Both formats present - should prefer @nightshift
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "**REJECT**\n\nActually: @nightshift approve"}) + "\n"
        )

        conv_log = review_dir / "conversation.jsonl"
        verdict = w.reviews.verdicts.extract_reviewer_verdict(conv_log, "issue-abc")
        assert verdict == "approve"
