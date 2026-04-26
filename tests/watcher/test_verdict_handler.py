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
