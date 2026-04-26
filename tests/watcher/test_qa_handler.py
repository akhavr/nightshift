"""Tests for QAHandler: scan_for_waiting, check_for_answers."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# scan_for_waiting tests
# ---------------------------------------------------------------------------

class TestScanForWaiting:
    def test_no_sessions_dir(self, tmp_path):
        """Missing sessions dir does not raise."""
        w = _make_watcher(tmp_path)
        w.qa.sessions_dir = tmp_path / "nonexistent"
        # Should not raise
        w.qa.scan_for_waiting()

    def test_new_waiting_json_pauses_container(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        waiting = {"question": "What is X?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        with patch("host.watcher.docker_pause", return_value=True) as mock_pause, \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")
        assert "abc" in w.qa._paused
        assert w.qa._paused["abc"]["question"] == "What is X?"

    def test_already_paused_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))
        w.qa._paused["abc"] = {"container": "nightshift-abc"}

        with patch("host.watcher.docker_pause") as mock_pause:
            w.qa.scan_for_waiting()
        mock_pause.assert_not_called()

    def test_pause_failure_logs_warning(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=False), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        assert "abc" not in w.qa._paused

    def test_invalid_waiting_json_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text("not valid json{{{")

        with patch("host.watcher.docker_pause") as mock_pause:
            w.qa.scan_for_waiting()
        mock_pause.assert_not_called()

    def test_no_waiting_json_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc")

        with patch("host.watcher.docker_pause") as mock_pause:
            w.qa.scan_for_waiting()
        mock_pause.assert_not_called()

    def test_tg_send_question_called_when_enabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "What?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=True), \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.telegram.send_question = MagicMock(return_value=42)
            w.qa.scan_for_waiting()

        w.telegram.send_question.assert_called_once()
        assert w.qa._paused["abc"]["tg_msg_id"] == 42


# ---------------------------------------------------------------------------
# check_for_answers tests
# ---------------------------------------------------------------------------

class TestCheckForAnswers:
    def test_answer_txt_unpauses_container(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("The answer")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({})

        mock_unpause.assert_called_once_with("nightshift-abc")
        assert "abc" not in w.qa._paused

    def test_telegram_reply_writes_answer_and_unpauses(self, tmp_path):
        w = _make_watcher(tmp_path)
        # State must be waiting:question to receive an answer
        sd = _make_session(w.sessions_dir, "abc", status="waiting:question")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({"abc": "My telegram answer"})

        mock_unpause.assert_called_once_with("nightshift-abc")
        assert "abc" not in w.qa._paused
        assert (sd / "answer.txt").read_text() == "My telegram answer"

    def test_no_answer_stays_paused(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({})

        mock_unpause.assert_not_called()
        assert "abc" in w.qa._paused

    def test_cli_answer_takes_priority_over_telegram(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("CLI answer")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({"abc": "TG answer"})

        mock_unpause.assert_called_once_with("nightshift-abc")
        # answer.txt content unchanged (CLI wrote it)
        assert (sd / "answer.txt").read_text() == "CLI answer"


# ---------------------------------------------------------------------------
# SSM validation tests
# ---------------------------------------------------------------------------

class TestQAValidatesTransition:
    """Test that QA handler validates SSM transitions."""

    def test_qa_validates_transition(self, tmp_path):
        """QA handler validates state is waiting:question before delivering answer."""
        import json

        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")

        # Set up state.json with waiting:question status
        state = {
            "issue_id": "issue-abc",
            "branch": "agent/abc",
            "status": "waiting:question",
        }
        (sd / "state.json").write_text(json.dumps(state))

        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        # Deliver answer via Telegram
        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({"abc": "The answer"})

        # Should unpause because state was valid
        mock_unpause.assert_called_once_with("nightshift-abc")
        assert (sd / "answer.txt").read_text() == "The answer"

    def test_qa_skips_invalid_state(self, tmp_path):
        """QA handler skips answer delivery if state is not waiting:question."""
        import json

        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")

        # Set up state.json with WRONG status (e.g., accepted = terminal)
        state = {
            "issue_id": "issue-abc",
            "branch": "agent/abc",
            "status": "accepted",
        }
        (sd / "state.json").write_text(json.dumps(state))

        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        # Try to deliver answer via Telegram
        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({"abc": "The answer"})

        # Should NOT unpause because state was invalid
        mock_unpause.assert_not_called()
        # answer.txt should NOT be written
        assert not (sd / "answer.txt").exists()
        # Session should still be in paused dict (waiting for valid state)
        assert "abc" in w.qa._paused
