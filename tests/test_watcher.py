"""Tests for host/watcher.py — HostWatcher coverage boost.

Covers: __init__, _scan_for_waiting, _check_for_answers, _check_for_auto_review,
_maybe_launch_review, _check_reviewer_done, _extract_reviewer_verdict,
_handle_reviewer_approve, _collect_reviewer_feedback, _handle_reviewer_revise,
_cleanup_review_session, _check_reviews, _poll_review_comments,
_check_orphaned_sessions, _check_closed_issues, _cleanup_session,
_do_revise, _do_cli_command, _poll_telegram_all, _route_tg_message,
_tg_notify, _tg_send_question.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_watcher(tmp_path, tg_enabled=False):
    """Build a HostWatcher with a sessions dir and Telegram disabled."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    w = HostWatcher(sessions, repo, auto_start=False)
    w.tg_enabled = tg_enabled
    w.tg_token = "tok" if tg_enabled else ""
    w.tg_chat = "123" if tg_enabled else ""
    return w


def _make_session(sessions_dir, sid, status="working", issue_id=None):
    """Create a minimal session directory with state.json."""
    sd = sessions_dir / sid
    sd.mkdir(exist_ok=True)
    state = {
        "issue_id": issue_id or f"issue-{sid}",
        "branch": f"agent/{sid}",
        "status": status,
        "step": 1,
        "checkpoints": [],
        "human_answers": [],
    }
    (sd / "state.json").write_text(json.dumps(state))
    return sd


def _make_issue(issue_id, title="Test Issue", labels=None, status="open"):
    return TrackerIssue(
        id=issue_id,
        identifier=issue_id[:12],
        title=title,
        body="",
        status=status,
        labels=labels or [],
    )


def _make_comment(body, author="human"):
    return TrackerComment(author=author, body=body)


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------

class TestHostWatcherInit:
    def test_default_state(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.sessions_dir == tmp_path / "sessions"
        assert w.repo_dir == tmp_path / "repo"
        assert w.auto_start is False
        assert w._paused == {}
        assert w._review_comment_counts == {}
        assert w._review_rounds == {}
        assert w._known_issue_ids == set()
        assert w._recently_launched == {}
        assert w._command_failures == {}
        assert w._tg_offset == 0

    def test_telegram_disabled_without_env(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.tg_enabled is False

    def test_telegram_enabled_with_token_and_chat(self, tmp_path):
        import host.watcher as wmod
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = True
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.tg_enabled is True
                assert w.tg_token == "tok"
                assert w.tg_chat == "42"
        finally:
            wmod.HAS_REQUESTS = orig

    def test_telegram_disabled_without_requests(self, tmp_path):
        import host.watcher as wmod
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = False
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.tg_enabled is False
        finally:
            wmod.HAS_REQUESTS = orig


# ---------------------------------------------------------------------------
# _scan_for_waiting tests
# ---------------------------------------------------------------------------

class TestScanForWaiting:
    def test_no_sessions_dir(self, tmp_path):
        """Missing sessions dir does not raise."""
        w = _make_watcher(tmp_path)
        w.sessions_dir = tmp_path / "nonexistent"
        # Should not raise
        w._scan_for_waiting()

    def test_new_waiting_json_pauses_container(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        waiting = {"question": "What is X?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True) as mock_pause, \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w._scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")
        assert "abc" in w._paused
        assert w._paused["abc"]["question"] == "What is X?"

    def test_already_paused_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))
        w._paused["abc"] = {"container": "nightshift-abc"}

        with patch("host.watcher.docker_pause") as mock_pause:
            w._scan_for_waiting()
        mock_pause.assert_not_called()

    def test_pause_failure_logs_warning(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=False), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w._scan_for_waiting()

        assert "abc" not in w._paused

    def test_invalid_waiting_json_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text("not valid json{{{")

        with patch("host.watcher.docker_pause") as mock_pause:
            w._scan_for_waiting()
        mock_pause.assert_not_called()

    def test_no_waiting_json_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc")

        with patch("host.watcher.docker_pause") as mock_pause:
            w._scan_for_waiting()
        mock_pause.assert_not_called()

    def test_tg_send_question_called_when_enabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "What?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w._tg_send_question = MagicMock(return_value=42)
            w._scan_for_waiting()

        w._tg_send_question.assert_called_once()
        assert w._paused["abc"]["tg_msg_id"] == 42


# ---------------------------------------------------------------------------
# _check_for_answers tests
# ---------------------------------------------------------------------------

class TestCheckForAnswers:
    def test_answer_txt_unpauses_container(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("The answer")
        w._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w._check_for_answers({})

        mock_unpause.assert_called_once_with("nightshift-abc")
        assert "abc" not in w._paused

    def test_telegram_reply_writes_answer_and_unpauses(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        w._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w._check_for_answers({"abc": "My telegram answer"})

        mock_unpause.assert_called_once_with("nightshift-abc")
        assert "abc" not in w._paused
        assert (sd / "answer.txt").read_text() == "My telegram answer"

    def test_no_answer_stays_paused(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        w._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w._check_for_answers({})

        mock_unpause.assert_not_called()
        assert "abc" in w._paused

    def test_cli_answer_takes_priority_over_telegram(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("CLI answer")
        w._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w._check_for_answers({"abc": "TG answer"})

        mock_unpause.assert_called_once_with("nightshift-abc")
        # answer.txt content unchanged (CLI wrote it)
        assert (sd / "answer.txt").read_text() == "CLI answer"


# ---------------------------------------------------------------------------
# _check_for_auto_review tests
# ---------------------------------------------------------------------------

class TestCheckForAutoReview:
    def test_no_sessions_dir(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.sessions_dir = tmp_path / "nonexistent"
        w._check_for_auto_review()  # should not raise

    def test_no_review_md(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._check_for_auto_review()
        assert launched == []

    def test_review_md_triggers_reviewer_launch(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._check_for_auto_review()
        assert "review-abc" in launched

    def test_skips_review_prefixed_sessions(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\n---\nReview\n")
        _make_session(w.sessions_dir, "review-abc", status="waiting:review")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._check_for_auto_review()
        assert launched == []

    def test_non_waiting_review_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\n---\nReview\n")
        _make_session(w.sessions_dir, "abc", status="working")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._check_for_auto_review()
        assert launched == []


# ---------------------------------------------------------------------------
# _maybe_launch_review tests
# ---------------------------------------------------------------------------

class TestMaybeLaunchReview:
    def test_launches_reviewer_and_increments_rounds(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 3\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        w._maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert "review-abc" in launched
        assert w._review_rounds["abc"] == 1
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "reviewing"

    def test_max_rounds_escalates(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 2\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w._review_rounds["abc"] = 2
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._tg_notify = MagicMock()

        w._maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w._tg_notify.assert_called_once()

    def test_round_count_increments_each_time(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.repo_dir / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 5\n---\n")
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w._review_rounds["abc"] = 1
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        w._maybe_launch_review("abc", sd, "issue-abc", w.repo_dir / "REVIEW.md")

        assert w._review_rounds["abc"] == 2


# ---------------------------------------------------------------------------
# _extract_reviewer_verdict tests
# ---------------------------------------------------------------------------

class TestExtractReviewerVerdict:
    def test_approve_from_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "All good. @nightshift approve"}) + "\n"
        )
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"

    def test_revise_from_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "Errors found. @nightshift revise"}) + "\n"
        )
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "revise"

    def test_no_verdict_in_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(
            json.dumps({"role": "thought", "content": "Still reviewing..."}) + "\n"
        )
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict is None

    def test_missing_conv_log(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w._extract_reviewer_verdict(tmp_path / "nonexistent.jsonl", "issue-123")
        assert verdict is None

    def test_approve_from_tracker_comments(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(json.dumps({"role": "thought", "content": "Regular content"}) + "\n")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("@nightshift approve")]
        w._tracker = tracker
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text(json.dumps({"role": "thought", "content": "no command here"}) + "\n")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker down")
        w._tracker = tracker
        # should not raise
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict is None

    def test_invalid_json_lines_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        conv = tmp_path / "conversation.jsonl"
        conv.write_text("not json\n" + json.dumps({"content": "@nightshift approve"}) + "\n")
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        verdict = w._extract_reviewer_verdict(conv, "issue-123")
        assert verdict == "approve"


# ---------------------------------------------------------------------------
# _handle_reviewer_approve tests
# ---------------------------------------------------------------------------

class TestHandleReviewerApprove:
    def test_updates_status_to_human_review(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w._tg_notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker

        w._handle_reviewer_approve("abc", coder_dir, "issue-abc")

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"

    def test_sends_tg_notification(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        tg_calls = []
        w._tg_notify = lambda msg: tg_calls.append(msg)
        tracker = MagicMock()
        w._tracker = tracker

        w._handle_reviewer_approve("abc", coder_dir, "issue-abc")

        assert len(tg_calls) == 1
        assert "approved" in tg_calls[0].lower()

    def test_posts_tracker_comment(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w._tg_notify = MagicMock()
        tracker = MagicMock()
        w._tracker = tracker

        w._handle_reviewer_approve("abc", coder_dir, "issue-abc")

        tracker.add_comment.assert_called_once()
        call_args = tracker.add_comment.call_args[0]
        assert "APPROVED" in call_args[1]

    def test_tracker_failure_doesnt_crash(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        w._tg_notify = MagicMock()
        tracker = MagicMock()
        tracker.add_comment.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w._handle_reviewer_approve("abc", coder_dir, "issue-abc")

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"


# ---------------------------------------------------------------------------
# _collect_reviewer_feedback tests
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

        parts = w._collect_reviewer_feedback("abc", "issue-abc", review_dir)

        assert any("Fix the test" in p for p in parts)

    def test_feedback_from_tracker_comments(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("Bad code. @nightshift revise")]
        w._tracker = tracker

        parts = w._collect_reviewer_feedback("abc", "issue-abc", review_dir)

        assert any("Bad code" in p for p in parts)

    def test_fallback_message_when_no_feedback(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []

        parts = w._collect_reviewer_feedback("abc", "issue-abc", review_dir)

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
        parts = w._collect_reviewer_feedback("abc", "issue-abc", review_dir)
        assert len(parts) >= 1


# ---------------------------------------------------------------------------
# _handle_reviewer_revise tests
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
        w._tg_notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        w._handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

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
        w._tg_notify = lambda msg: tg_calls.append(msg)
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w._launch_background = lambda cmd, sid: None

        w._handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        assert any("revise" in m.lower() or "revision" in m.lower() for m in tg_calls)

    def test_marks_recently_launched(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing")
        review_dir = tmp_path / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text("")
        w._tg_notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w._launch_background = lambda cmd, sid: None

        w._handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        assert "abc" in w._recently_launched


# ---------------------------------------------------------------------------
# _cleanup_review_session tests
# ---------------------------------------------------------------------------

class TestCleanupReviewSession:
    def test_removes_review_dir(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()
        (review_dir / "state.json").write_text("{}")
        w._recently_launched["review-abc"] = time.time()
        w._review_comment_counts["review-abc"] = 5

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"):
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg
            w._cleanup_review_session("review-abc", review_dir)

        assert not review_dir.exists()
        assert "review-abc" not in w._recently_launched
        assert "review-abc" not in w._review_comment_counts

    def test_cleanup_failure_does_not_raise(self, tmp_path):
        w = _make_watcher(tmp_path)
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()

        with patch("core.config.load_workflow", side_effect=RuntimeError("boom")):
            # should not raise
            w._cleanup_review_session("review-abc", review_dir)


# ---------------------------------------------------------------------------
# _check_reviewer_done tests
# ---------------------------------------------------------------------------

class TestCheckReviewerDone:
    def test_approve_verdict_transitions_coder(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "@nightshift approve"}) + "\n"
        )
        w._tg_notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        w._cleanup_review_session = MagicMock()

        w._check_reviewer_done()

        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        w._cleanup_review_session.assert_called_once()

    def test_revise_verdict_resumes_coder(self, tmp_path):
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "Fix this. @nightshift revise"}) + "\n"
        )
        w._tg_notify = MagicMock()
        w._tracker = MagicMock()
        w._tracker.get_comments.return_value = []
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)
        w._cleanup_review_session = MagicMock()

        w._check_reviewer_done()

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
        w._cleanup_review_session = MagicMock()

        w._check_reviewer_done()

        w._cleanup_review_session.assert_not_called()

    def test_non_review_sessions_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review")  # coder, not reviewer
        w._cleanup_review_session = MagicMock()

        w._check_reviewer_done()

        w._cleanup_review_session.assert_not_called()


# ---------------------------------------------------------------------------
# _check_reviews / _poll_review_comments tests
# ---------------------------------------------------------------------------

class TestCheckReviews:
    def test_poll_skipped_within_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_review_poll = time.time()  # just polled
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w._check_reviews({})

        tracker.get_comments.assert_not_called()

    def test_poll_runs_after_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_review_poll = 0.0  # long ago
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = []
        w._tracker = tracker

        w._check_reviews({})

        tracker.get_comments.assert_called_once_with("issue-abc")

    def test_nightshift_command_triggers_action(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_review_poll = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("Looks good. @nightshift accept")
        ]
        w._tracker = tracker
        w._review_comment_counts["abc"] = 0

        actions = []
        w._handle_review_command = lambda sid, iid, cmd, sd: actions.append((sid, cmd))

        w._check_reviews({})

        assert ("abc", "accept") in actions

    def test_non_review_session_comment_count_cleared(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_review_poll = 0.0
        _make_session(w.sessions_dir, "abc", status="working")
        w._review_comment_counts["abc"] = 5

        w._check_reviews({})

        assert "abc" not in w._review_comment_counts


# ---------------------------------------------------------------------------
# _poll_review_comments tests
# ---------------------------------------------------------------------------

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
        w._handle_review_command = lambda sid, iid, cmd, s: actions.append((sid, cmd))

        w._poll_review_comments("abc", "issue-abc", sd)

        assert ("abc", "accept") in actions
        assert w._review_comment_counts["abc"] == 2

    def test_new_comments_processed(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [
            _make_comment("Old comment"),
            _make_comment("@nightshift revise Fix the bug"),
        ]
        w._tracker = tracker
        w._review_comment_counts["abc"] = 1  # already seen first comment

        actions = []
        w._handle_review_command = lambda sid, iid, cmd, s: actions.append((sid, cmd))

        w._poll_review_comments("abc", "issue-abc", sd)

        assert ("abc", "revise") in actions

    def test_no_new_comments_no_action(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("Old")]
        w._tracker = tracker
        w._review_comment_counts["abc"] = 1

        actions = []
        w._handle_review_command = lambda *a: actions.append(a)

        w._poll_review_comments("abc", "issue-abc", sd)

        assert actions == []

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w._poll_review_comments("abc", "issue-abc", sd)


# ---------------------------------------------------------------------------
# _check_orphaned_sessions tests
# ---------------------------------------------------------------------------

class TestCheckOrphanedSessions:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = time.time()
        _make_session(w.sessions_dir, "abc", status="working")
        with patch("host.watcher.docker_container_status") as mock_cs:
            w._check_orphaned_sessions()
        mock_cs.assert_not_called()

    def test_running_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="running"):
            w._check_orphaned_sessions()

        assert launched == []

    def test_orphaned_session_auto_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w._check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" in w._recently_launched

    def test_recently_launched_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()  # just launched
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w._check_orphaned_sessions()

        assert launched == []

    def test_paused_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="paused"):
            w._check_orphaned_sessions()

        assert launched == []

    def test_non_active_status_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w._check_orphaned_sessions()

        assert launched == []

    def test_grace_period_expired_triggers_resume(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # Put in recently_launched but with old timestamp
        w._recently_launched["abc"] = time.time() - 9999  # way past grace period
        launched = []
        w._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w._check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" not in w._recently_launched or w._recently_launched["abc"] > time.time() - 5


# ---------------------------------------------------------------------------
# _check_closed_issues tests
# ---------------------------------------------------------------------------

class TestCheckClosedIssues:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = time.time()
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w._check_closed_issues()

        tracker.get_issue.assert_not_called()

    def test_working_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w._check_closed_issues()

        tracker.get_issue.assert_not_called()

    def test_closed_issue_triggers_cleanup(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w._cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        with patch("host.watcher.docker_stop"):
            w._check_closed_issues()

        assert "abc" in cleaned

    def test_open_issue_not_cleaned(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="open")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w._cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        w._check_closed_issues()

        assert cleaned == []

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w._check_closed_issues()


# ---------------------------------------------------------------------------
# _cleanup_session tests
# ---------------------------------------------------------------------------

class TestCleanupSession:
    def test_removes_session_dir_and_clears_tracking(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w._review_comment_counts["abc"] = 5
        w._recently_launched["abc"] = time.time()

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg
            w._cleanup_session("abc", "issue-abc", sd)

        mock_rmtree.assert_called_once_with(sd)
        assert "abc" not in w._review_comment_counts
        assert "abc" not in w._recently_launched

    def test_cleanup_failure_logged_not_raised(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")

        with patch("core.config.load_workflow", side_effect=RuntimeError("boom")):
            # should not raise
            w._cleanup_session("abc", "issue-abc", sd)


# ---------------------------------------------------------------------------
# _do_revise tests
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
        w._launch_background = lambda cmd, sid: launched.append(sid)

        w._do_revise("abc", "issue-abc", sd)

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
        w._launch_background = lambda cmd, sid: launched.append(sid)

        w._do_revise("abc", "issue-abc", sd)

        assert launched == []

    def test_revise_clears_comment_count(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.return_value = [_make_comment("fix this")]
        w._tracker = tracker
        w._review_comment_counts["abc"] = 5
        w._launch_background = lambda cmd, sid: None

        w._do_revise("abc", "issue-abc", sd)

        assert "abc" not in w._review_comment_counts

    def test_revise_exception_logged(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_comments.side_effect = RuntimeError("DB error")
        w._tracker = tracker

        # should not raise
        w._do_revise("abc", "issue-abc", sd)


# ---------------------------------------------------------------------------
# _do_cli_command tests
# ---------------------------------------------------------------------------

class TestDoCliCommand:
    def test_successful_accept_command(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tg_notify = MagicMock()
        w._review_comment_counts["abc"] = 3

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
            w._do_cli_command("abc", "accept", "issue-abc")

        mock_run.assert_called_once()
        assert "abc" not in w._review_comment_counts
        assert "abc" not in w._command_failures

    def test_failed_command_sets_backoff(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tg_notify = MagicMock()

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            w._do_cli_command("abc", "accept", "issue-abc")

        assert "abc" in w._command_failures
        _, attempts = w._command_failures["abc"]
        assert attempts == 1

    def test_successful_command_clears_failures(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tg_notify = MagicMock()
        w._command_failures["abc"] = (time.time(), 3)

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            w._do_cli_command("abc", "accept", "issue-abc")

        assert "abc" not in w._command_failures

    def test_reject_command(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tg_notify = MagicMock()

        with patch("host.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            w._do_cli_command("abc", "reject", "issue-abc")

        call_args = mock_run.call_args[0][0]
        assert "reject" in call_args

    def test_subprocess_exception_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._tg_notify = MagicMock()

        with patch("host.watcher.subprocess.run", side_effect=OSError("no such file")):
            # should not raise
            w._do_cli_command("abc", "accept", "issue-abc")


# ---------------------------------------------------------------------------
# _tg_notify tests
# ---------------------------------------------------------------------------

class TestTgNotify:
    def test_does_nothing_when_disabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=False)
        with patch("host.watcher.requests") as mock_req:
            w._tg_notify("hello")
        mock_req.post.assert_not_called()

    def test_sends_message_when_enabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w._tg_notify("hello")
        mock_req.post.assert_called_once()
        call_kwargs = mock_req.post.call_args[1]
        assert "repo" in call_kwargs["json"]["text"]

    def test_long_message_truncated(self, tmp_path):
        from host.constants import TG_MESSAGE_SOFT_LIMIT, TG_TRUNCATION_POINT
        w = _make_watcher(tmp_path, tg_enabled=True)
        long_msg = "x" * (TG_MESSAGE_SOFT_LIMIT + 100)
        with patch("host.watcher.requests") as mock_req:
            w._tg_notify(long_msg)
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert "(truncated" in sent_text
        assert len(sent_text) <= TG_TRUNCATION_POINT + 100  # truncation point + suffix

    def test_short_message_not_truncated(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w._tg_notify("short message")
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert "truncated" not in sent_text
        assert "short message" in sent_text

    def test_request_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.side_effect = Exception("network error")
            # should not raise
            w._tg_notify("hello")

    def test_project_name_in_message(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w._tg_notify("test notification")
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert w.repo_dir.name in sent_text


# ---------------------------------------------------------------------------
# _tg_send_question tests
# ---------------------------------------------------------------------------

class TestTgSendQuestion:
    def test_returns_message_id_on_success(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            msg_id = w._tg_send_question("abc", "What is X?", "issue-abc")
        assert msg_id == 99

    def test_returns_none_on_api_failure(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            msg_id = w._tg_send_question("abc", "What is X?", "issue-abc")
        assert msg_id is None

    def test_returns_none_on_exception(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.side_effect = Exception("network error")
            msg_id = w._tg_send_question("abc", "What is X?", "issue-abc")
        assert msg_id is None

    def test_force_reply_markup_included(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            w._tg_send_question("abc", "What?", "short-abc")
        payload = mock_req.post.call_args[1]["json"]
        assert payload["reply_markup"]["force_reply"] is True


# ---------------------------------------------------------------------------
# _poll_telegram_all tests
# ---------------------------------------------------------------------------

class TestPollTelegramAll:
    def test_returns_empty_dicts_on_error(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.side_effect = Exception("timeout")
            qa, reviews = w._poll_telegram_all()
        assert qa == {}
        assert reviews == {}

    def test_updates_offset_after_poll(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        w._tg_offset = 0
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {"update_id": 10, "message": {"text": "", "chat": {"id": "123"},
                                               "from": {"first_name": "User"}}},
            ]
        }
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            w._poll_telegram_all()
        assert w._tg_offset == 11

    def test_routes_reply_to_paused_qa(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        w._paused["abc"] = {"tg_msg_id": 5, "container": "nightshift-abc"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {
                    "update_id": 20,
                    "message": {
                        "message_id": 21,
                        "text": "my answer",
                        "chat": {"id": "123"},
                        "from": {"first_name": "User"},
                        "reply_to_message": {"message_id": 5, "text": "Question?"},
                    },
                }
            ]
        }
        w._tg_ack = MagicMock()
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            qa, reviews = w._poll_telegram_all()
        assert "abc" in qa
        assert qa["abc"] == "my answer"

    def test_ignores_messages_from_wrong_chat(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {
                    "update_id": 30,
                    "message": {
                        "message_id": 31,
                        "text": "@nightshift accept abc",
                        "chat": {"id": "9999"},  # wrong chat
                        "from": {"first_name": "User"},
                    },
                }
            ]
        }
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            qa, reviews = w._poll_telegram_all()
        assert qa == {}
        assert reviews == {}


# ---------------------------------------------------------------------------
# _route_tg_message tests
# ---------------------------------------------------------------------------

class TestRouteTgMessage:
    def test_qa_reply_routed_to_paused_session(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._paused["abc"] = {"tg_msg_id": 10, "container": "nightshift-abc"}
        w._tg_ack = MagicMock()

        msg = {
            "message_id": 11,
            "from": {"first_name": "Alice"},
            "reply_to_message": {"message_id": 10, "text": "What?"},
        }
        qa = {}
        reviews = {}
        w._route_tg_message(msg, "the answer", qa, reviews)

        assert "abc" in qa
        assert qa["abc"] == "the answer"

    def test_review_command_without_reply_matched(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        w._tg_ack = MagicMock()

        msg = {
            "message_id": 5,
            "from": {"first_name": "Bob"},
        }
        qa = {}
        reviews = {}
        text = f"@nightshift accept {sd.name}"
        w._route_tg_message(msg, text, qa, reviews)

        assert "abc" in reviews

    def test_non_command_message_ignored(self, tmp_path):
        w = _make_watcher(tmp_path)

        msg = {
            "message_id": 5,
            "from": {"first_name": "Bob"},
        }
        qa = {}
        reviews = {}
        w._route_tg_message(msg, "just a regular message", qa, reviews)

        assert qa == {}
        assert reviews == {}


# ---------------------------------------------------------------------------
# docker utils (through watcher) tests
# ---------------------------------------------------------------------------

class TestDockerUtils:
    def test_docker_pause_called_on_waiting(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=True) as mock_pause, \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w._scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")

    def test_docker_unpause_called_with_container_name(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("answer")
        w._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w._check_for_answers({})

        mock_unpause.assert_called_once_with("nightshift-abc")

    def test_docker_stop_called_on_closed_issue(self, tmp_path):
        w = _make_watcher(tmp_path)
        w._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.return_value = _make_issue("issue-abc", status="closed")
        w._tracker = tracker
        w._cleanup_session = MagicMock()

        with patch("host.watcher.docker_stop") as mock_stop:
            w._check_closed_issues()

        mock_stop.assert_called_once_with("nightshift-abc")


# ---------------------------------------------------------------------------
# _handle_review_command backoff tests
# ---------------------------------------------------------------------------

class TestHandleReviewCommandBackoff:
    def test_command_blocked_during_backoff(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        # Set command failure with recent time
        w._command_failures["abc"] = (time.time(), 1)  # 1 attempt, backoff = 60s

        actions = []
        w._do_cli_command = lambda *a: actions.append(a)
        w._do_revise = lambda *a: actions.append(a)

        w._handle_review_command("abc", "issue-abc", "accept", sd)

        assert actions == []  # blocked by backoff

    def test_command_allowed_after_backoff_expires(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        # Set failure in the past (well beyond backoff)
        w._command_failures["abc"] = (time.time() - 9999, 1)

        actions = []
        w._do_cli_command = lambda sid, cmd, iid: actions.append(cmd)

        w._handle_review_command("abc", "issue-abc", "accept", sd)

        assert "accept" in actions
