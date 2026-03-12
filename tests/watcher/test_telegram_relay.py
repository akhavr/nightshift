"""Tests for TelegramRelay: notify, send_question, poll_all, route_message."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# notify tests
# ---------------------------------------------------------------------------

class TestTgNotify:
    def test_does_nothing_when_disabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=False)
        with patch("host.watcher.requests") as mock_req:
            w.telegram.notify("hello")
        mock_req.post.assert_not_called()

    def test_sends_message_when_enabled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w.telegram.notify("hello")
        mock_req.post.assert_called_once()
        call_kwargs = mock_req.post.call_args[1]
        assert "repo" in call_kwargs["json"]["text"]

    def test_long_message_truncated(self, tmp_path):
        from host.constants import TG_MESSAGE_SOFT_LIMIT, TG_TRUNCATION_POINT
        w = _make_watcher(tmp_path, tg_enabled=True)
        long_msg = "x" * (TG_MESSAGE_SOFT_LIMIT + 100)
        with patch("host.watcher.requests") as mock_req:
            w.telegram.notify(long_msg)
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert "(truncated" in sent_text
        assert len(sent_text) <= TG_TRUNCATION_POINT + 100  # truncation point + suffix

    def test_short_message_not_truncated(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w.telegram.notify("short message")
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert "truncated" not in sent_text
        assert "short message" in sent_text

    def test_request_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.side_effect = Exception("network error")
            # should not raise
            w.telegram.notify("hello")

    def test_project_name_in_message(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            w.telegram.notify("test notification")
        sent_text = mock_req.post.call_args[1]["json"]["text"]
        assert w.repo_dir.name in sent_text


# ---------------------------------------------------------------------------
# send_question tests
# ---------------------------------------------------------------------------

class TestTgSendQuestion:
    def test_returns_message_id_on_success(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            msg_id = w.telegram.send_question("abc", "What is X?", "issue-abc")
        assert msg_id == 99

    def test_returns_none_on_api_failure(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            msg_id = w.telegram.send_question("abc", "What is X?", "issue-abc")
        assert msg_id is None

    def test_returns_none_on_exception(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.side_effect = Exception("network error")
            msg_id = w.telegram.send_question("abc", "What is X?", "issue-abc")
        assert msg_id is None

    def test_force_reply_markup_included(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        with patch("host.watcher.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            w.telegram.send_question("abc", "What?", "short-abc")
        payload = mock_req.post.call_args[1]["json"]
        assert payload["reply_markup"]["force_reply"] is True


# ---------------------------------------------------------------------------
# poll_all tests
# ---------------------------------------------------------------------------

class TestPollTelegramAll:
    def test_returns_empty_dicts_on_error(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.side_effect = Exception("timeout")
            qa, reviews = w.telegram.poll_all(w.qa._paused)
        assert qa == {}
        assert reviews == {}

    def test_updates_offset_after_poll(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.telegram._offset = 0
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {"update_id": 10, "message": {"text": "", "chat": {"id": "123"},
                                               "from": {"first_name": "User"}}},
            ]
        }
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            w.telegram.poll_all(w.qa._paused)
        assert w.telegram._offset == 11

    def test_routes_reply_to_paused_qa(self, tmp_path):
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.qa._paused["abc"] = {"tg_msg_id": 5, "container": "nightshift-abc"}
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
        w.telegram.ack = MagicMock()
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            qa, reviews = w.telegram.poll_all(w.qa._paused)
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
            qa, reviews = w.telegram.poll_all(w.qa._paused)
        assert qa == {}
        assert reviews == {}


# ---------------------------------------------------------------------------
# route_message tests
# ---------------------------------------------------------------------------

class TestRouteTgMessage:
    def test_qa_reply_routed_to_paused_session(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.qa._paused["abc"] = {"tg_msg_id": 10, "container": "nightshift-abc"}
        w.telegram.ack = MagicMock()

        msg = {
            "message_id": 11,
            "from": {"first_name": "Alice"},
            "reply_to_message": {"message_id": 10, "text": "What?"},
        }
        qa = {}
        reviews = {}
        w.telegram.route_message(msg, "the answer", qa, reviews, w.qa._paused)

        assert "abc" in qa
        assert qa["abc"] == "the answer"

    def test_review_command_without_reply_matched(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        w.telegram.ack = MagicMock()

        msg = {
            "message_id": 5,
            "from": {"first_name": "Bob"},
        }
        qa = {}
        reviews = {}
        text = f"@nightshift accept {sd.name}"
        w.telegram.route_message(msg, text, qa, reviews, w.qa._paused)

        assert "abc" in reviews

    def test_non_command_message_ignored(self, tmp_path):
        w = _make_watcher(tmp_path)

        msg = {
            "message_id": 5,
            "from": {"first_name": "Bob"},
        }
        qa = {}
        reviews = {}
        w.telegram.route_message(msg, "just a regular message", qa, reviews, w.qa._paused)

        assert qa == {}
        assert reviews == {}
