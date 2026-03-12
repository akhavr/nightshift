"""Tests for PROJECT_NAME prefix in notifiers."""

import os
from unittest.mock import patch, MagicMock

from adapters.notifiers._utils import project_prefix


class FakeTracker:
    def add_comment(self, *a, **kw): pass
    def get_issue(self, *a, **kw): return None
    def list_issues(self, **kw): return []
    def get_comments(self, *a, **kw): return []
    def add_label(self, *a, **kw): pass
    def remove_label(self, *a, **kw): pass
    def sync(self): pass


class TestProjectPrefix:
    def test_prefix_set(self):
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            assert project_prefix("hello") == "[myrepo] hello"

    def test_prefix_empty(self):
        env = os.environ.copy()
        env.pop("PROJECT_NAME", None)
        with patch.dict(os.environ, env, clear=True):
            assert project_prefix("hello") == "hello"


class TestTelegramPrefix:
    @patch("adapters.notifiers.telegram.requests.post")
    def test_notify_includes_prefix(self, mock_post):
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            from adapters.notifiers.telegram import TelegramNotifier
            n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123")
            n.notify("test message")
            call_args = mock_post.call_args
            text = call_args[1]["json"]["text"] if "json" in call_args[1] else call_args[0][0]
            assert text.startswith("[myrepo]")

    @patch("adapters.notifiers.telegram.requests.post")
    def test_notify_logs_on_error(self, mock_post):
        import requests as req
        mock_post.side_effect = req.RequestException("fail")
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            from adapters.notifiers.telegram import TelegramNotifier
            n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123")
            # Should not raise — but should log
            n.notify("test")

    @patch("adapters.notifiers.telegram.requests.post")
    def test_send_question_includes_prefix(self, mock_post):
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={"ok": True, "result": {"message_id": 1}})
        )
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            from adapters.notifiers.telegram import TelegramNotifier
            n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123")
            n.send_question("issue-1", "What next?", "abc123")
            text = mock_post.call_args[1]["json"]["text"]
            assert text.startswith("[myrepo]")


class TestWebhookPrefix:
    @patch("adapters.notifiers.webhook.requests.post")
    def test_notify_includes_prefix(self, mock_post):
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            from adapters.notifiers.webhook import WebhookNotifier
            n = WebhookNotifier(url="http://example.com/hook")
            n.notify("test message")
            text = mock_post.call_args[1]["json"]["text"]
            assert text.startswith("[myrepo]")

    @patch("adapters.notifiers.webhook.requests.post")
    def test_notify_no_prefix_when_unset(self, mock_post):
        env = os.environ.copy()
        env.pop("PROJECT_NAME", None)
        with patch.dict(os.environ, env, clear=True):
            from adapters.notifiers.webhook import WebhookNotifier
            n = WebhookNotifier(url="http://example.com/hook")
            n.notify("test message")
            text = mock_post.call_args[1]["json"]["text"]
            assert text == "test message"

    @patch("adapters.notifiers.webhook.requests.post")
    def test_notify_logs_on_error(self, mock_post):
        mock_post.side_effect = Exception("fail")
        with patch.dict(os.environ, {"PROJECT_NAME": "myrepo"}, clear=False):
            from adapters.notifiers.webhook import WebhookNotifier
            n = WebhookNotifier(url="http://example.com/hook")
            # Should not raise
            n.notify("test")


class TestLaunchProjectName:
    def test_launch_passes_project_name(self):
        """Verify docker_cmd.py includes PROJECT_NAME in docker command."""
        from pathlib import Path
        docker_cmd_py = Path(__file__).parent.parent / "host" / "docker_cmd.py"
        content = docker_cmd_py.read_text()
        assert "PROJECT_NAME" in content
        assert 'f"PROJECT_NAME={repo.name}"' in content
