"""Tests for ReviewOrchestrator: auto-review, verdicts, approve/revise, cleanup, commands."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment
from core.state_machine import InvalidTransition

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# SSM-7: Review flow SSM transition tests
# ---------------------------------------------------------------------------

class TestSSMTransitions:
    """Verify review flow uses SSM-validated state transitions."""

    def test_launch_transitions_to_reviewing(self, tmp_path):
        """launch_review() must use SSM transition to 'reviewing'.

        SSM-7: Review launch should validate transition via SSM.
        Valid: waiting:review -> reviewing
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w.reviews._launch_background = lambda cmd, sid: True

        w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "reviewing"

    def test_launch_from_invalid_state_raises(self, tmp_path):
        """Launching review from invalid state should raise InvalidTransition.

        SSM-7: Only waiting:review -> reviewing is valid (plus working->reviewing
        for revert, and reviewing->reviewing for re-launch).
        """
        from host.session_utils import update_status

        w = _make_watcher(tmp_path)
        # Use 'starting' which cannot transition to 'reviewing'
        sd = _make_session(w.sessions_dir, "abc", status="starting", issue_id="issue-abc")

        with pytest.raises(InvalidTransition):
            update_status(sd, "reviewing")

    def test_done_transitions_to_human_review(self, tmp_path):
        """Review done (escalate) must use SSM transition to 'waiting:human-review'.

        SSM-7: Escalation should validate transition via SSM.
        Valid: waiting:review -> waiting:human-review
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 1\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w.reviews._rounds["abc"] = 1  # Already at max rounds
        w.reviews._launch_background = lambda cmd, sid: True
        w.telegram.notify = MagicMock()

        w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:human-review"


# ---------------------------------------------------------------------------
# Review launch vs coder relaunch tests
# ---------------------------------------------------------------------------

class TestReviewVsCoderLaunch:
    """Watcher should launch review, not coder, when coder completes."""

    def test_launches_review_not_coder_after_completion(self, tmp_path):
        """When coder session is in waiting:review, watcher launches review, not coder.

        This is the key test for the bug: the watcher was trying to relaunch
        the coder session instead of launching a review session, causing
        'session already exists' errors.
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")

        # Create coder session in waiting:review status
        coder_dir = _make_session(w.sessions_dir, "abc123", status="waiting:review",
                                  issue_id="abc123def456full")

        # Track what gets launched
        launched_cmds = []
        w.reviews._launch_background = lambda cmd, sid: launched_cmds.append((cmd, sid)) or True

        # Run auto-review check
        w.reviews.check_for_auto_review()

        # Should launch a REVIEW session, not a coder session
        assert len(launched_cmds) == 1, "Expected exactly one launch"
        cmd, sid = launched_cmds[0]
        assert sid == "review-abc123", f"Expected review session ID, got {sid}"
        assert "--step" in cmd, "Expected --step in command"
        step_idx = cmd.index("--step")
        assert cmd[step_idx + 1] == "review", f"Expected step=review, got {cmd[step_idx + 1]}"


# ---------------------------------------------------------------------------
# check_for_auto_review tests
# ---------------------------------------------------------------------------

class TestCheckForAutoReview:
    def test_no_sessions_dir(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.reviews.sessions_dir = tmp_path / "nonexistent"
        w.reviews.check_for_auto_review()  # should not raise

    def test_no_review_md(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.check_for_auto_review()
        assert launched == []

    def test_review_md_triggers_reviewer_launch(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.check_for_auto_review()
        assert "review-abc" in launched

    def test_skips_review_prefixed_sessions(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\n---\nReview\n")
        _make_session(w.sessions_dir, "review-abc", status="waiting:review")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.check_for_auto_review()
        assert launched == []

    def test_non_waiting_review_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\n---\nReview\n")
        _make_session(w.sessions_dir, "abc", status="working")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.check_for_auto_review()
        assert launched == []


# ---------------------------------------------------------------------------
# maybe_launch_review tests
# ---------------------------------------------------------------------------

class TestMaybeLaunchReview:
    def test_launches_reviewer_and_increments_rounds(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert "review-abc" in launched
        assert w.reviews._rounds["abc"] == 1
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "reviewing"

    def test_max_rounds_escalates(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 2\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w.reviews._rounds["abc"] = 2
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.telegram.notify = MagicMock()

        w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w.telegram.notify.assert_called_once()

    def test_round_count_increments_each_time(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 5\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w.reviews._rounds["abc"] = 1
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert w.reviews._rounds["abc"] == 2

    def test_empty_branch_not_reviewed(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text(
            "---\nworkspace:\n  base_branch: master\nreview:\n  max_rounds: 3\n---\n"
        )
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.telegram.notify = MagicMock()

        with patch("host.watcher.review_orchestrator.check_empty_session", return_value=True):
            w.reviews.maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w.telegram.notify.assert_called_once()


# ---------------------------------------------------------------------------
# extract_reviewer_verdict tests
# ---------------------------------------------------------------------------

class TestExtractReviewerVerdict:
    def test_approve_from_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "All good. @nightshift approve"}) + "\n"
        )
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"

    def test_revise_from_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "Errors found. @nightshift revise"}) + "\n"
        )
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "revise"

    def test_no_verdict_in_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "Still reviewing..."}) + "\n"
        )
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict is None

    def test_missing_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w.reviews.extract_reviewer_verdict(tmp_path / "nonexistent.jsonl", "issue-123")
        assert verdict is None

    def test_approve_from_tracker_comments(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(json.dumps({"role": "thought", "content": "Regular content"}) + "\n")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("@nightshift approve")]
        w._tracker = tracker
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(json.dumps({"role": "thought", "content": "no command here"}) + "\n")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker down")
        w._tracker = tracker
        # should not raise
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict is None

    def test_invalid_json_lines_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text("not json\n" + json.dumps({"content": "@nightshift approve"}) + "\n")
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w.reviews.extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"


# ---------------------------------------------------------------------------
# handle_reviewer_approve tests
# ---------------------------------------------------------------------------

class TestHandleReviewerApprove:
    def test_updates_status_to_human_review(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker

        w.reviews.handle_reviewer_approve("abc", coder_dir, "issue-abc")

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"

    def test_sends_tg_notification(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        tg_calls = []
        w.telegram.notify = lambda msg, **kw: tg_calls.append(msg)
        tracker = MagicMock()
        w._tracker = tracker

        w.reviews.handle_reviewer_approve("abc", coder_dir, "issue-abc")

        assert len(tg_calls) == 1
        assert "approved" in tg_calls[0].lower()

    def test_posts_tracker_comment(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker

        w.reviews.handle_reviewer_approve("abc", coder_dir, "issue-abc")

        tracker.add_comment.assert_called_once()
        call_args = tracker.add_comment.call_args[0]
        assert "APPROVED" in call_args[1]

    def test_tracker_failure_doesnt_crash(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        tracker.add_comment.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w.reviews.handle_reviewer_approve("abc", coder_dir, "issue-abc")

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"


# ---------------------------------------------------------------------------
# collect_reviewer_feedback tests
# ---------------------------------------------------------------------------

class TestCollectReviewerFeedback:
    def test_feedback_from_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix the test. @nightshift revise"}) + "\n"
        )
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        parts = w.reviews.collect_reviewer_feedback("abc", "issue-abc", review_dir)

        assert any("Fix the test" in p for p in parts)

    def test_feedback_from_tracker_comments(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("Bad code. @nightshift revise")]
        w._tracker = tracker

        parts = w.reviews.collect_reviewer_feedback("abc", "issue-abc", review_dir)

        assert any("Bad code" in p for p in parts)

    def test_fallback_message_when_no_feedback(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        parts = w.reviews.collect_reviewer_feedback("abc", "issue-abc", review_dir)

        assert len(parts) == 1
        assert "did not provide specific feedback" in parts[0]

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise, returns fallback
        parts = w.reviews.collect_reviewer_feedback("abc", "issue-abc", review_dir)
        assert len(parts) >= 1


# ---------------------------------------------------------------------------
# handle_reviewer_revise tests
# ---------------------------------------------------------------------------

class TestHandleReviewerRevise:
    def test_writes_resume_prompt_and_relaunches(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix error handling. @nightshift revise"}) + "\n"
        )
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "working"
        assert (coder_dir / "resume-prompt.md").exists()
        assert "abc" in launched

    def test_sends_tg_notification(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        tg_calls = []
        w.telegram.notify = lambda msg, **kw: tg_calls.append(msg)
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w.reviews._launch_background = lambda cmd, sid: True

        w.reviews.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        assert any("revise" in m.lower() or "revision" in m.lower() for m in tg_calls)

    def test_marks_recently_launched(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w.reviews._launch_background = lambda cmd, sid: True

        w.reviews.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        assert "abc" in w._recently_launched


# ---------------------------------------------------------------------------
# cleanup_review_session tests
# ---------------------------------------------------------------------------

class TestCleanupReviewSession:
    def test_removes_review_dir(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()
        (review_dir / "state.json").write_text("{}")
        w._recently_launched["review-abc"] = time.time()
        w.reviews._comment_counts["review-abc"] = 5

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"):
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg
            w.reviews.cleanup_review_session("review-abc", review_dir)

        assert not review_dir.exists()
        assert "review-abc" not in w._recently_launched
        assert "review-abc" not in w.reviews._comment_counts

    def test_cleanup_failure_does_not_raise(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()

        with patch("core.config.load_workflow", side_effect=RuntimeError("boom")):
            # should not raise
            w.reviews.cleanup_review_session("review-abc", review_dir)


# ---------------------------------------------------------------------------
# check_reviewer_done tests
# ---------------------------------------------------------------------------

class TestCheckReviewerDone:
    def test_approve_verdict_transitions_coder(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "@nightshift approve"}) + "\n"
        )
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w.reviews.cleanup_review_session.assert_called_once()

    def test_revise_verdict_resumes_coder(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix this. @nightshift revise"}) + "\n"
        )
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "working"
        assert "abc" in launched

    def test_no_verdict_does_nothing(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="reviewing")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Still reviewing..."}) + "\n"
        )
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        w.reviews.cleanup_review_session.assert_not_called()

    def test_non_review_sessions_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review")  # coder, not reviewer
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        w.reviews.cleanup_review_session.assert_not_called()


# ---------------------------------------------------------------------------
# check_reviews / poll_review_comments tests
# ---------------------------------------------------------------------------

class TestCheckReviews:
    def test_poll_skipped_within_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.reviews._last_poll = time.time()  # just polled
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w.reviews.check_reviews({})

        tracker.get_comments.assert_not_called()

    def test_poll_runs_after_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.reviews._last_poll = 0.0  # long ago
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = []
        w._tracker = tracker

        w.reviews.check_reviews({})

        tracker.get_comments.assert_called_once_with("issue-abc")

    def test_nightshift_command_triggers_action(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.reviews._last_poll = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("Looks good. @nightshift accept")
        ]
        w._tracker = tracker
        w.reviews._comment_counts["abc"] = 0

        actions = []
        w.reviews.handle_review_command = lambda sid, iid, cmd, sd: actions.append((sid, cmd))

        w.reviews.check_reviews({})

        assert ("abc", "accept") in actions

    def test_non_review_session_comment_count_cleared(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.reviews._last_poll = 0.0
        _make_session(w.sessions_dir, "abc", status="working")
        w.reviews._comment_counts["abc"] = 5

        w.reviews.check_reviews({})

        assert "abc" not in w.reviews._comment_counts


class TestPollReviewComments:
    def test_first_poll_checks_last_comment(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("No command"),
            _make_comment("Looks good. @nightshift accept"),
        ]
        w._tracker = tracker
        # last_count == 0 (first poll)

        actions = []
        w.reviews.handle_review_command = lambda sid, iid, cmd, s: actions.append((sid, cmd))

        w.reviews.poll_review_comments("abc", "issue-abc", sd)

        assert ("abc", "accept") in actions
        assert w.reviews._comment_counts["abc"] == 2

    def test_new_comments_processed(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("Old comment"),
            _make_comment("@nightshift revise Fix the bug"),
        ]
        w._tracker = tracker
        w.reviews._comment_counts["abc"] = 1  # already seen first comment

        actions = []
        w.reviews.handle_review_command = lambda sid, iid, cmd, s: actions.append((sid, cmd))

        w.reviews.poll_review_comments("abc", "issue-abc", sd)

        assert ("abc", "revise") in actions

    def test_no_new_comments_no_action(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("Old")]
        w._tracker = tracker
        w.reviews._comment_counts["abc"] = 1

        actions = []
        w.reviews.handle_review_command = lambda *a: actions.append(a)

        w.reviews.poll_review_comments("abc", "issue-abc", sd)

        assert actions == []

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w.reviews.poll_review_comments("abc", "issue-abc", sd)


# ---------------------------------------------------------------------------
# do_revise tests
# ---------------------------------------------------------------------------

class TestDoRevise:
    def test_revise_writes_resume_prompt_and_launches(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("Work complete. Awaiting review."),
            _make_comment("Please fix error handling."),
        ]
        w._tracker = tracker
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.do_revise("abc", "issue-abc", sd)

        assert (sd / "resume-prompt.md").exists()
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"
        assert "abc" in launched

    def test_revise_no_feedback_skips_launch(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = []
        w._tracker = tracker
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.reviews.do_revise("abc", "issue-abc", sd)

        assert launched == []

    def test_revise_clears_comment_count(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("fix this")]
        w._tracker = tracker
        w.reviews._comment_counts["abc"] = 5
        w.reviews._launch_background = lambda cmd, sid: True

        w.reviews.do_revise("abc", "issue-abc", sd)

        assert "abc" not in w.reviews._comment_counts

    def test_revise_exception_logged(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("DB error")
        w._tracker = tracker

        # should not raise
        w.reviews.do_revise("abc", "issue-abc", sd)


# ---------------------------------------------------------------------------
# do_cli_command tests
# ---------------------------------------------------------------------------

class TestDoCliCommand:
    def test_successful_accept_command(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.telegram.notify = MagicMock()
        w.reviews._comment_counts["abc"] = 3

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
            w.reviews.do_cli_command("abc", "accept", "issue-abc")

        mock_run.assert_called_once()
        assert "abc" not in w.reviews._comment_counts
        assert "abc" not in w.reviews._command_failures

    def test_failed_command_sets_backoff(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.telegram.notify = MagicMock()

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            w.reviews.do_cli_command("abc", "accept", "issue-abc")

        assert "abc" in w.reviews._command_failures
        _, attempts = w.reviews._command_failures["abc"]
        assert attempts == 1

    def test_successful_command_clears_failures(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.telegram.notify = MagicMock()
        w.reviews._command_failures["abc"] = (time.time(), 3)

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            w.reviews.do_cli_command("abc", "accept", "issue-abc")

        assert "abc" not in w.reviews._command_failures

    def test_reject_command(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.telegram.notify = MagicMock()

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            w.reviews.do_cli_command("abc", "reject", "issue-abc")

        call_args = mock_run.call_args[0][0]
        assert "reject" in call_args

    def test_subprocess_exception_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.telegram.notify = MagicMock()

        with patch("host.watcher.subprocess.run", side_effect=OSError("no such file")):
            # should not raise
            w.reviews.do_cli_command("abc", "accept", "issue-abc")


# ---------------------------------------------------------------------------
# handle_review_command backoff tests
# ---------------------------------------------------------------------------

class TestHandleReviewCommandBackoff:
    def test_command_blocked_during_backoff(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        # Set command failure with recent time
        w.reviews._command_failures["abc"] = (time.time(), 1)  # 1 attempt, backoff = 60s

        actions = []
        w.reviews.do_cli_command = lambda *a: actions.append(a)
        w.reviews.do_revise = lambda *a: actions.append(a)

        w.reviews.handle_review_command("abc", "issue-abc", "accept", sd)

        assert actions == []  # blocked by backoff

    def test_command_allowed_after_backoff_expires(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        # Set failure in the past (well beyond backoff)
        w.reviews._command_failures["abc"] = (time.time() - 9999, 1)

        actions = []
        w.reviews.do_cli_command = lambda sid, cmd, iid: actions.append(cmd)

        w.reviews.handle_review_command("abc", "issue-abc", "accept", sd)

        assert "accept" in actions


# ---------------------------------------------------------------------------
# Review no-verdict fallback tests
# ---------------------------------------------------------------------------

class TestReviewNoVerdict:
    """Review sessions that hit max-turns without a verdict fall back to human review."""

    def test_no_verdict_transitions_coder_to_human_review(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(
            w.sessions_dir, "review-abc",
            status="suspended:review-no-verdict", issue_id="issue-abc")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w.reviews.cleanup_review_session.assert_called_once()

    def test_no_verdict_posts_tracker_comment(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        _make_session(
            w.sessions_dir, "review-abc",
            status="suspended:review-no-verdict", issue_id="issue-abc")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        tracker.add_comment.assert_called_once()
        comment_body = tracker.add_comment.call_args[0][1]
        assert "human review" in comment_body.lower()

    def test_no_verdict_sends_notification(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        _make_session(
            w.sessions_dir, "review-abc",
            status="suspended:review-no-verdict", issue_id="issue-abc")
        tg_calls = []
        w.telegram.notify = lambda msg, **kw: tg_calls.append(msg)
        w._tracker = MagicMock()
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        assert len(tg_calls) == 1
        assert "human review" in tg_calls[0].lower()

    def test_no_verdict_missing_coder_dir_no_crash(self, tmp_path):
        """If coder session dir is missing, no crash but no status update."""
        w = _make_watcher(tmp_path)
        # No coder session created
        _make_session(
            w.sessions_dir, "review-abc",
            status="suspended:review-no-verdict", issue_id="issue-abc")
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()  # should not raise

        w.reviews.cleanup_review_session.assert_called_once()

    def test_no_verdict_tracker_failure_no_crash(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        _make_session(
            w.sessions_dir, "review-abc",
            status="suspended:review-no-verdict", issue_id="issue-abc")
        w.telegram.notify = MagicMock()
        tracker = MagicMock()
        tracker.add_comment.side_effect = RuntimeError("tracker down")
        w._tracker = tracker
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()  # should not raise

        # Coder should still be transitioned despite tracker failure
        coder_state = json.loads(
            (w.sessions_dir / "abc" / "state.json").read_text())
        assert coder_state["status"] == "waiting:human-review"


# ---------------------------------------------------------------------------
# Stale review session cleanup before launch tests
# ---------------------------------------------------------------------------

class TestStaleReviewCleanupBeforeLaunch:
    """Pre-launch cleanup for stale review sessions with completed_at set."""

    def test_launch_review_cleans_stale_session_first(self, tmp_path):
        """maybe_launch_review() cleans up stale review sessions before launching.

        This is the fix for the race condition where:
        1. Review container sets completed_at and exits
        2. Watcher restarts before cleanup_review_session() is called
        3. Watcher tries to launch new review, fails with 'session already exists'

        The fix: check for stale review session and clean it up before launching.
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")

        # Create coder session in waiting:review
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        # Create STALE review session (completed_at set but not cleaned up)
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (review_sd / "state.json").write_text(json.dumps(state))
        # Add verdict to conversation log - cleanup only happens after verdict processing
        (review_sd / "conversation.jsonl").write_text(
            '{"role":"assistant","content":"@@NIGHTSHIFT approve"}\n')

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            cfg.review.max_rounds = 3
            mock_lw.return_value = cfg

            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        # Stale review session should be cleaned up after verdict processed
        assert any("review-abc" in str(call) for call in mock_rmtree.call_args_list)
        # Coder should transition to waiting:human-review (approve verdict)
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "waiting:human-review"
        # No new review launched - verdict was processed
        assert "review-abc" not in launched

    def test_launch_review_does_not_clean_incomplete_session(self, tmp_path):
        """maybe_launch_review() does NOT clean up review sessions without completed_at."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")

        # Create coder session in waiting:review
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        # Create review session WITHOUT completed_at (still running or crashed)
        review_sd = _make_session(w.sessions_dir, "review-abc", status="working",
                                  issue_id="issue-abc")
        # No completed_at set

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            cfg.review.max_rounds = 3
            mock_lw.return_value = cfg

            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        # Incomplete review session should NOT be cleaned up
        # Note: the launch may fail because session exists, but that's OK for this test
        # The point is that we don't delete an active review session
        assert not any("review-abc" in str(call) for call in mock_rmtree.call_args_list)


# ---------------------------------------------------------------------------
# Review launch without pre-review rebase tests
# ---------------------------------------------------------------------------

class TestReviewLaunchWithoutPreReviewRebase:
    def test_no_prereview_rebase(self, tmp_path):
        """maybe_launch_review() must not call host-side pre-review rebase."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid)) or True

        with patch("host.rebase.attempt_pre_review_rebase",
                   side_effect=AssertionError("pre-review rebase should not run")) as mock_rebase:
            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        mock_rebase.assert_not_called()

    def test_review_launches_without_rebase(self, tmp_path):
        """Review launches directly from waiting:review without a rebase step."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid)) or True

        with patch("host.rebase.attempt_pre_review_rebase") as mock_rebase:
            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        assert len(launched) == 1
        cmd, sid = launched[0]
        assert sid == "review-abc"
        assert "--step" in cmd
        assert cmd[cmd.index("--step") + 1] == "review"
        assert json.loads((coder_sd / "state.json").read_text())["status"] == "reviewing"
        mock_rebase.assert_not_called()

    def test_rebase_conflict_handled_at_accept(self, tmp_path):
        """Review launch ignores rebase conflicts; accept-time flow owns them."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid)) or True
        w.telegram.notify = MagicMock()

        with patch("host.rebase.attempt_pre_review_rebase",
                   return_value="REBASE CONFLICT: fix conflicts") as mock_rebase:
            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        mock_rebase.assert_not_called()
        assert any(sid == "review-abc" for _, sid in launched)
        assert not (coder_sd / "resume-prompt.md").exists()
        assert json.loads((coder_sd / "state.json").read_text())["status"] == "reviewing"


# ---------------------------------------------------------------------------
# Stale review cleanup processes verdict first (REQ-036)
# ---------------------------------------------------------------------------

class TestStaleCleanupProcessesVerdictFirst:
    """Stale review cleanup must process verdict before cleanup to avoid infinite loop.

    Bug scenario:
    1. Review session completes (@@DONE@@), sets completed_at
    2. Watcher's _maybe_cleanup_stale_review() sees completed_at -> cleans up review
    3. Coder session is still waiting:review (verdict not yet processed)
    4. maybe_launch_review() sees coder in waiting:review with no review -> launches NEW review
    5. Repeat from step 1 (infinite loop)

    Fix: Process the verdict BEFORE cleanup in _maybe_cleanup_stale_review().
    """

    def test_stale_cleanup_processes_verdict_first(self, tmp_path):
        """Stale review cleanup processes verdict before cleanup to prevent relaunch loop."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")

        # Create coder session in waiting:review (simulating state when review completes)
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        # Create STALE review session with completed_at AND an approve verdict
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (review_sd / "state.json").write_text(json.dumps(state))
        # Write verdict to conversation log
        (review_sd / "conversation.jsonl").write_text(
            json.dumps({"role": "assistant", "content": "LGTM. @nightshift approve"}) + "\n"
        )

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree"):
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            cfg.review.max_rounds = 3
            mock_lw.return_value = cfg

            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        # KEY ASSERTION: Coder should be transitioned to waiting:human-review
        # (because verdict was "approve") BEFORE new review was launched
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "waiting:human-review", (
            f"Expected status 'waiting:human-review' after approve verdict processed, "
            f"got '{coder_state['status']}'"
        )

        # No new review should be launched (coder is no longer in waiting:review)
        # If _maybe_cleanup_stale_review() didn't process verdict, coder would still be
        # waiting:review and a new review would be launched -> infinite loop
        assert "review-abc" not in launched, (
            "New review should NOT be launched after approve verdict processed"
        )

    def test_stale_cleanup_processes_revise_verdict(self, tmp_path):
        """Stale review cleanup handles revise verdict - resumes coder."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")

        # Create coder session in waiting:review
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        # Create STALE review session with completed_at AND a revise verdict
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (review_sd / "state.json").write_text(json.dumps(state))
        (review_sd / "conversation.jsonl").write_text(
            json.dumps({"role": "assistant", "content": "Fix error handling. @nightshift revise"}) + "\n"
        )

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.telegram.notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree"):
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            cfg.review.max_rounds = 3
            mock_lw.return_value = cfg

            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        # Coder should be resumed (status=working) after revise verdict
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "working", (
            f"Expected status 'working' after revise verdict processed, "
            f"got '{coder_state['status']}'"
        )

        # Coder should be relaunched (not review)
        assert "abc" in launched, "Coder should be relaunched after revise verdict"
        assert "review-abc" not in launched, "New review should NOT be launched"

    def test_stale_cleanup_no_verdict_blocks_until_verdict(self, tmp_path):
        """If stale review has no verdict, cleanup is blocked and review does NOT relaunch.

        This prevents the race condition where:
        1. Review finishes but verdict extraction fails (wrong format)
        2. Cleanup happens anyway (old buggy behavior)
        3. Coder stuck in reviewing/waiting:review forever

        Fix: Leave stale review intact until verdict is found. check_reviewer_done()
        can retry verdict extraction later.
        """
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")

        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")

        # Create STALE review session with completed_at but NO verdict
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (review_sd / "state.json").write_text(json.dumps(state))
        # Empty conversation log - no verdict
        (review_sd / "conversation.jsonl").write_text("")

        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree") as mock_remove_worktree, \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            cfg.review.max_rounds = 3
            mock_lw.return_value = cfg

            w.reviews.maybe_launch_review("abc", coder_sd, "issue-abc",
                                          w.repo_dir / "REVIEW.md")

        # No verdict found - cleanup should NOT happen
        mock_rmtree.assert_not_called()
        mock_remove_worktree.assert_not_called()
        # Review session should still exist (not cleaned up)
        assert review_sd.exists()
        # No new review launched - stale one still blocking
        assert "review-abc" not in launched
        # Coder should stay in waiting:review (not moved to reviewing)
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "waiting:review"


# ---------------------------------------------------------------------------
# Missing session directory handling tests
# ---------------------------------------------------------------------------

class TestMissingSessionDirHandled:
    """Watcher should handle missing session directories gracefully.

    Bug scenario:
    1. Issue is accepted (code merged)
    2. Tracker update fails (lock contention)
    3. Session directory is cleaned up
    4. Watcher sees open issue with nightshift label
    5. Tries to resume → crash with FileNotFoundError
    """

    def test_maybe_launch_review_skips_missing_session_dir(self, tmp_path):
        """maybe_launch_review should skip if session_dir doesn't exist."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        w.telegram.notify = MagicMock()
        launched = []
        w.reviews._launch_background = lambda cmd, sid: launched.append(sid) or True

        # Session directory does NOT exist
        missing_dir = w.sessions_dir / "abc"
        assert not missing_dir.exists()

        # Should not crash
        w.reviews.maybe_launch_review("abc", missing_dir, "issue-abc",
                                       w.repo_dir / "REVIEW.md")

        # Should not launch anything
        assert launched == []

    def test_escalate_to_human_skips_missing_dir(self, tmp_path):
        """_escalate_to_human should skip if session_dir doesn't exist."""
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 1\n---\n")
        w.telegram.notify = MagicMock()
        w.reviews._rounds["abc"] = 1  # At max rounds

        # Session directory does NOT exist
        missing_dir = w.sessions_dir / "abc"
        assert not missing_dir.exists()

        # Should not crash
        w.reviews._escalate_to_human("abc", missing_dir, "issue-abc", 1)

        # Should still send notification (no session dir update needed)
        w.telegram.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Review cleanup race condition tests (REQ-033)
# ---------------------------------------------------------------------------

class TestNoArchiveWithoutVerdictProcessing:
    """Review sessions must NOT be archived until verdict is processed.

    With Option A (REQ-033), cleanup only happens via check_reviewer_done()
    after a verdict is extracted and processed. No periodic cleanup path exists.
    """

    def _completed_at(self, seconds_ago: int) -> str:
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()

    def test_no_archive_without_verdict_processing(self, tmp_path):
        """Review with completed_at but no processed verdict is NOT archived.

        When verdict extraction fails, cleanup_review_session() is NOT called,
        so the review session remains for retry or manual intervention.
        """
        w = _make_watcher(tmp_path)
        # Coder is still in 'reviewing' - verdict NOT yet processed
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")
        # Review finished (completed_at set) but verdict extraction failed
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = self._completed_at(120)
        (review_sd / "state.json").write_text(json.dumps(state))
        # Empty conversation - no verdict to extract
        (review_sd / "conversation.jsonl").write_text("")

        # Simulate check_reviewer_done() failing to find verdict
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w.reviews.cleanup_review_session = MagicMock()

        w.reviews.check_reviewer_done()

        # Verdict extraction should fail (no verdict in conversation or tracker)
        # So cleanup should NOT be called - review session preserved
        w.reviews.cleanup_review_session.assert_not_called()

        # Review session should still exist (not archived)
        assert review_sd.exists()
        # Coder should still be in 'reviewing' (unchanged)
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "reviewing"
