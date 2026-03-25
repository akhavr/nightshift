"""Tests for lifecycle comment posting to the issue tracker."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher.lifecycle_comments import (
    _safe_post, _truncate, post_start, post_resume, post_question,
    post_done, post_revise, read_checkpoint_count,
    _PREVIEW_LEN,
)
from tests.watcher.conftest import _make_watcher, _make_session


# ---------------------------------------------------------------------------
# _safe_post
# ---------------------------------------------------------------------------

class TestSafePost:
    def test_posts_comment_without_sync(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        _safe_post(get_tracker, "issue-1", "hello", "test", "sid-1")

        tracker.add_comment.assert_called_once_with("issue-1", "hello")
        # sync() is NOT called — the watcher syncs periodically via _maybe_sync_tracker()
        tracker.sync.assert_not_called()

    def test_logs_on_tracker_failure(self):
        get_tracker = MagicMock(side_effect=RuntimeError("tracker down"))

        # Should not raise
        _safe_post(get_tracker, "issue-1", "hello", "test", "sid-1")

    def test_logs_on_add_comment_failure(self):
        tracker = MagicMock()
        tracker.add_comment.side_effect = RuntimeError("write failed")
        get_tracker = MagicMock(return_value=tracker)

        # Should not raise
        _safe_post(get_tracker, "issue-1", "hello", "test", "sid-1")


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_exact_length_unchanged(self):
        text = "x" * _PREVIEW_LEN
        assert _truncate(text) == text

    def test_long_text_truncated_with_ellipsis(self):
        text = "x" * 500
        result = _truncate(text)
        assert result.endswith("...")
        assert len(result) == _PREVIEW_LEN + 3

    def test_custom_max_len(self):
        result = _truncate("abcdefgh", max_len=5)
        assert result == "abcde..."


# ---------------------------------------------------------------------------
# post_start
# ---------------------------------------------------------------------------

class TestPostStart:
    def test_posts_start_comment_with_title(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_start(get_tracker, "issue-abc", "abc", title="Fix login bug")

        tracker.add_comment.assert_called_once()
        body = tracker.add_comment.call_args[0][1]
        assert "abc" in body
        assert "started" in body
        assert "working on: Fix login bug" in body
        assert "nightshift logs" in body
        assert "nightshift history" in body

    def test_posts_start_comment_without_title(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_start(get_tracker, "issue-abc", "abc")

        body = tracker.add_comment.call_args[0][1]
        assert "started" in body
        assert "working on" not in body


# ---------------------------------------------------------------------------
# post_resume
# ---------------------------------------------------------------------------

class TestPostResume:
    def test_posts_resume_comment_with_reason_and_checkpoints(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_resume(get_tracker, "issue-abc", "abc",
                    reason="orphaned (container gone)", checkpoint_count=3)

        body = tracker.add_comment.call_args[0][1]
        assert "resumed" in body
        assert "orphaned" in body
        assert "Checkpoint count: 3" in body


# ---------------------------------------------------------------------------
# post_question
# ---------------------------------------------------------------------------

class TestPostQuestion:
    def test_posts_question_comment(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_question(get_tracker, "issue-abc", "abc", "What is the DB password?")

        body = tracker.add_comment.call_args[0][1]
        assert "blocked on question" in body
        assert "What is the DB password?" in body
        assert "nightshift answer" in body

    def test_truncates_long_question(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)
        long_q = "x" * 500

        post_question(get_tracker, "issue-abc", "abc", long_q)

        body = tracker.add_comment.call_args[0][1]
        assert "..." in body
        assert len(body) < 500 + 200  # body shouldn't contain full 500 chars


# ---------------------------------------------------------------------------
# post_done
# ---------------------------------------------------------------------------

class TestPostDone:
    def test_posts_done_comment(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_done(get_tracker, "issue-abc", "abc", checkpoint_count=5)

        body = tracker.add_comment.call_args[0][1]
        assert "complete" in body
        assert "Checkpoints: 5" in body
        assert "nightshift logs" in body


# ---------------------------------------------------------------------------
# post_revise
# ---------------------------------------------------------------------------

class TestPostRevise:
    def test_posts_revise_comment(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)

        post_revise(get_tracker, "issue-abc", "abc", "Fix the tests")

        body = tracker.add_comment.call_args[0][1]
        assert "revision" in body
        assert "Fix the tests" in body

    def test_truncates_long_reason(self):
        tracker = MagicMock()
        get_tracker = MagicMock(return_value=tracker)
        long_reason = "r" * 500

        post_revise(get_tracker, "issue-abc", "abc", long_reason)

        body = tracker.add_comment.call_args[0][1]
        assert "..." in body


# ---------------------------------------------------------------------------
# read_checkpoint_count
# ---------------------------------------------------------------------------

class TestReadCheckpointCount:
    def test_reads_checkpoint_count(self, tmp_path):
        sd = tmp_path / "session"
        sd.mkdir()
        state = {"checkpoints": [{"id": 1}, {"id": 2}, {"id": 3}]}
        (sd / "state.json").write_text(json.dumps(state))

        assert read_checkpoint_count(sd) == 3

    def test_returns_zero_on_missing_file(self, tmp_path):
        sd = tmp_path / "session"
        sd.mkdir()
        assert read_checkpoint_count(sd) == 0

    def test_returns_zero_on_invalid_json(self, tmp_path):
        sd = tmp_path / "session"
        sd.mkdir()
        (sd / "state.json").write_text("not json{{{")
        assert read_checkpoint_count(sd) == 0

    def test_returns_zero_on_missing_checkpoints_key(self, tmp_path):
        sd = tmp_path / "session"
        sd.mkdir()
        (sd / "state.json").write_text(json.dumps({"status": "working"}))
        assert read_checkpoint_count(sd) == 0


# ---------------------------------------------------------------------------
# Integration: QAHandler posts question comment
# ---------------------------------------------------------------------------

class TestQAHandlerPostsQuestion:
    def test_question_comment_posted_on_first_pause(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", issue_id="issue-abc")
        waiting = {"question": "What is X?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        tracker.add_comment.assert_called_once()
        body = tracker.add_comment.call_args[0][1]
        assert "What is X?" in body

    def test_same_question_not_posted_twice_without_answer(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", issue_id="issue-abc")
        waiting = {"question": "What is X?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        # Reset tracker, remove from paused to simulate re-scan
        tracker.reset_mock()
        w.qa._paused.clear()

        (sd / "waiting.json").write_text(json.dumps(waiting))
        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 2000.0
            w.qa.scan_for_waiting()

        # Comment should NOT be posted again (same question, no answer in between)
        tracker.add_comment.assert_not_called()

    def test_second_question_posted_after_answer_delivered(self, tmp_path):
        """Multi-round Q&A: second question gets its own lifecycle comment."""
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", issue_id="issue-abc")

        # First question
        waiting = {"question": "What is X?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.docker_unpause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        assert tracker.add_comment.call_count == 1
        assert "What is X?" in tracker.add_comment.call_args[0][1]

        # Deliver answer via CLI (write answer.txt)
        (sd / "answer.txt").write_text("42")

        with patch("host.watcher.docker_unpause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.time.return_value = 2000.0
            w.qa.check_for_answers({})

        # Verify answer was processed and dedup was reset
        assert "abc" not in w.qa._paused
        assert "abc" not in w.qa._posted_question

        # Clean up for second round
        (sd / "waiting.json").unlink()
        (sd / "answer.txt").unlink()
        tracker.reset_mock()

        # Second question
        waiting2 = {"question": "What is Y?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting2))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 3000.0
            w.qa.scan_for_waiting()

        # Second question SHOULD get its own comment
        tracker.add_comment.assert_called_once()
        assert "What is Y?" in tracker.add_comment.call_args[0][1]

    def test_no_tracker_no_crash(self, tmp_path):
        """QAHandler with no get_tracker should not crash."""
        from host.watcher.qa_handler import QAHandler
        from host.watcher.telegram_relay import TelegramRelay

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        tg = TelegramRelay("", "", "repo", sessions)
        qa = QAHandler(sessions, tg)  # no get_tracker
        sd = sessions / "abc"
        sd.mkdir()
        state = {"issue_id": "issue-abc", "branch": "b", "status": "working"}
        (sd / "state.json").write_text(json.dumps(state))
        waiting = {"question": "Q?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            qa.scan_for_waiting()  # should not raise


# ---------------------------------------------------------------------------
# Integration: SessionMonitor posts start comment
# ---------------------------------------------------------------------------

class TestSessionMonitorPostsStart:
    def test_start_comment_posted_on_auto_start(self, tmp_path):
        from core.protocols import TrackerIssue

        tracker = MagicMock()
        issue = TrackerIssue(
            id="issue-new-123456789012",
            identifier="issue-new-12",
            title="New issue",
            body="",
            status="open",
            labels=["nightshift"],
        )
        tracker.list_issues.return_value = [issue]

        w = _make_watcher(tmp_path)
        w._tracker = tracker
        w.auto_start = True
        w.monitor.auto_start = True
        w.monitor._last_auto_start_poll = 0.0
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        asc = MagicMock()
        asc.enabled = True
        asc.label = "nightshift"
        asc.poll_interval_s = 0
        asc.max_concurrent = 5
        w.monitor._get_auto_start_config = lambda: asc

        w.monitor.check_new_issues()

        assert len(launched) == 1
        # Verify start comment was posted with issue title
        tracker.add_comment.assert_called_once()
        body = tracker.add_comment.call_args[0][1]
        assert "started" in body
        assert "working on: New issue" in body


# ---------------------------------------------------------------------------
# Integration: SessionMonitor posts resume comment
# ---------------------------------------------------------------------------

class TestSessionMonitorPostsResume:
    def test_resume_comment_posted_on_orphan_recovery(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        tracker.add_comment.assert_called_once()
        body = tracker.add_comment.call_args[0][1]
        assert "resumed" in body
        assert "orphaned" in body


# ---------------------------------------------------------------------------
# Integration: ReviewOrchestrator posts done comment
# ---------------------------------------------------------------------------

class TestReviewOrchestratorPostsDone:
    def test_done_comment_posted_when_session_reaches_waiting_review(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        # Add some checkpoints to state
        state = json.loads((sd / "state.json").read_text())
        state["checkpoints"] = [{"id": 1}, {"id": 2}]
        (sd / "state.json").write_text(json.dumps(state))

        # No REVIEW.md -> won't launch review, but done comment should still post
        w.reviews.check_for_auto_review()

        tracker.add_comment.assert_called_once()
        body = tracker.add_comment.call_args[0][1]
        assert "complete" in body
        assert "Checkpoints: 2" in body

    def test_done_comment_reposted_after_revise_auto_review(self, tmp_path):
        """After auto-review revise verdict, second completion gets a new done comment."""
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        # First done comment
        w.reviews.check_for_auto_review()
        assert tracker.add_comment.call_count == 1
        assert "complete" in tracker.add_comment.call_args[0][1]

        # Simulate auto-review revise verdict clearing _posted_done
        w.reviews._posted_done.discard("abc")

        # Update state back to waiting:review (coder completed again)
        state = json.loads((sd / "state.json").read_text())
        state["status"] = "waiting:review"
        state["checkpoints"] = [{"id": 1}, {"id": 2}, {"id": 3}]
        (sd / "state.json").write_text(json.dumps(state))
        tracker.reset_mock()

        w.reviews.check_for_auto_review()

        # Second done comment SHOULD be posted
        tracker.add_comment.assert_called_once()
        assert "Checkpoints: 3" in tracker.add_comment.call_args[0][1]

    def test_done_comment_reposted_after_human_revise(self, tmp_path):
        """After human @nightshift revise, second completion gets a new done comment."""
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        # First done comment
        w.reviews.check_for_auto_review()
        assert tracker.add_comment.call_count == 1
        tracker.reset_mock()

        # Human revise command clears _posted_done
        launched = []
        w.reviews.commands._launch_background = lambda cmd, sid: launched.append(sid)
        tracker.get_comments.return_value = [
            MagicMock(body="@nightshift revise", author="human")
        ]
        w.reviews._dispatch_review_command("abc", "issue-abc", "revise", sd)

        # Verify _posted_done was cleared
        assert "abc" not in w.reviews._posted_done

        # Second completion
        state = json.loads((sd / "state.json").read_text())
        state["status"] = "waiting:review"
        (sd / "state.json").write_text(json.dumps(state))
        tracker.reset_mock()

        w.reviews.check_for_auto_review()
        tracker.add_comment.assert_called_once()
        assert "complete" in tracker.add_comment.call_args[0][1]

    def test_done_comment_not_posted_twice(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        w.reviews.check_for_auto_review()
        tracker.reset_mock()
        w.reviews.check_for_auto_review()

        tracker.add_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: VerdictHandler posts revise comment
# ---------------------------------------------------------------------------

class TestVerdictHandlerPostsRevise:
    def test_revise_comment_posted(self, tmp_path):
        tracker = MagicMock()
        w = _make_watcher(tmp_path)
        w._tracker = tracker

        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                   issue_id="issue-abc")

        # Write reviewer conversation with revise verdict
        conv = {"role": "assistant", "content": "@nightshift revise: fix the tests please"}
        (review_dir / "conversation.jsonl").write_text(json.dumps(conv) + "\n")

        launched = []
        w.reviews.verdicts._launch_background = lambda cmd, sid: launched.append(sid)

        w.reviews.verdicts.handle_reviewer_revise("abc", coder_dir, "issue-abc", review_dir)

        # Should have posted revise comment
        assert any("revision" in str(c) for c in tracker.add_comment.call_args_list)
