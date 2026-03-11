"""Tests for HostWatcher init and Docker utility integration."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import host.watcher as wmod
from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------

class TestHostWatcherInit:
    def test_default_state(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.sessions_dir == tmp_path / "sessions"
        assert w.repo_dir == tmp_path / "repo"
        assert w.auto_start is False
        assert w.qa._paused == {}
        assert w.reviews._comment_counts == {}
        assert w.reviews._rounds == {}
        assert w.monitor._known_issue_ids == set()
        assert w._recently_launched == {}
        assert w.reviews._command_failures == {}
        assert w.telegram._offset == 0

    def test_telegram_disabled_without_env(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.telegram.enabled is False

    def test_telegram_enabled_with_token_and_chat(self, tmp_path):
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = True
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.telegram.enabled is True
                assert w.telegram.token == "tok"
                assert w.telegram.chat_id == "42"
        finally:
            wmod.HAS_REQUESTS = orig

    def test_telegram_disabled_without_requests(self, tmp_path):
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = False
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.telegram.enabled is False
        finally:
            wmod.HAS_REQUESTS = orig


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
            w.qa.scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")

    def test_docker_unpause_called_with_container_name(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("answer")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({})

        mock_unpause.assert_called_once_with("nightshift-abc")

    def test_docker_stop_called_on_closed_issue(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.return_value = _make_issue("issue-abc", status="closed")
        w._tracker = tracker
        w.monitor.cleanup_session = MagicMock()

        with patch("host.watcher.docker_stop") as mock_stop:
            w.monitor.check_closed_issues()

        mock_stop.assert_called_once_with("nightshift-abc")
