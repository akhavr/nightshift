"""Tests for notification level filtering (REQ-010)."""

from unittest.mock import patch, MagicMock

from core.protocols import NotificationLevel, should_notify
from adapters.notifiers.composite import CompositeNotifier
from tests.conftest import MockNotifier


class TestShouldNotify:
    """Test the should_notify helper ordering."""

    def test_questions_allows_questions(self):
        assert should_notify(NotificationLevel.QUESTIONS, NotificationLevel.QUESTIONS)

    def test_questions_blocks_actions(self):
        assert not should_notify(NotificationLevel.QUESTIONS, NotificationLevel.ACTIONS)

    def test_questions_blocks_all(self):
        assert not should_notify(NotificationLevel.QUESTIONS, NotificationLevel.ALL)

    def test_actions_allows_questions(self):
        assert should_notify(NotificationLevel.ACTIONS, NotificationLevel.QUESTIONS)

    def test_actions_allows_actions(self):
        assert should_notify(NotificationLevel.ACTIONS, NotificationLevel.ACTIONS)

    def test_actions_blocks_all(self):
        assert not should_notify(NotificationLevel.ACTIONS, NotificationLevel.ALL)

    def test_all_allows_everything(self):
        for level in NotificationLevel:
            assert should_notify(NotificationLevel.ALL, level)


class TestWebhookLevelFiltering:
    @patch("adapters.notifiers.webhook.requests.post")
    def test_webhook_filters_by_level(self, mock_post):
        from adapters.notifiers.webhook import WebhookNotifier
        n = WebhookNotifier(url="http://example.com/hook", level="questions")
        # ALL-level message should be filtered
        n.notify("auto-start info")
        mock_post.assert_not_called()
        # QUESTIONS-level message should go through
        n.notify("question?", level=NotificationLevel.QUESTIONS)
        mock_post.assert_called_once()

    @patch("adapters.notifiers.webhook.requests.post")
    def test_webhook_default_all(self, mock_post):
        from adapters.notifiers.webhook import WebhookNotifier
        n = WebhookNotifier(url="http://example.com/hook")
        n.notify("anything")
        mock_post.assert_called_once()

    @patch("adapters.notifiers.webhook.requests.post")
    def test_webhook_send_question_bypasses_level_filter(self, mock_post):
        """send_question uses QUESTIONS level so it passes even with questions-only config."""
        from adapters.notifiers.webhook import WebhookNotifier
        n = WebhookNotifier(url="http://example.com/hook", level="questions")
        result = n.send_question("issue-1", "What color?", "abc")
        assert result is False
        mock_post.assert_called_once()


class FakeTracker:
    def add_comment(self, *a, **kw): pass
    def get_issue(self, *a, **kw): return None
    def list_issues(self, **kw): return []
    def get_comments(self, *a, **kw): return []
    def add_label(self, *a, **kw): pass
    def remove_label(self, *a, **kw): pass
    def sync(self): pass


class TestTelegramLevelFiltering:
    @patch("adapters.notifiers.telegram.requests.post")
    def test_telegram_filters_by_level(self, mock_post):
        from adapters.notifiers.telegram import TelegramNotifier
        n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123", level="actions")
        # ALL-level message should be filtered
        n.notify("auto-start info")
        mock_post.assert_not_called()
        # ACTIONS-level message should go through
        n.notify("done!", level=NotificationLevel.ACTIONS)
        mock_post.assert_called_once()

    @patch("adapters.notifiers.telegram.requests.post")
    def test_telegram_questions_only(self, mock_post):
        from adapters.notifiers.telegram import TelegramNotifier
        n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123", level="questions")
        n.notify("done!", level=NotificationLevel.ACTIONS)
        mock_post.assert_not_called()
        n.notify("question?", level=NotificationLevel.QUESTIONS)
        mock_post.assert_called_once()

    @patch("adapters.notifiers.telegram.requests.post")
    def test_send_question_bypasses_level_filter(self, mock_post):
        """send_question() is the Q&A round-trip and always goes through."""
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={"ok": True, "result": {"message_id": 1}})
        )
        from adapters.notifiers.telegram import TelegramNotifier
        n = TelegramNotifier(FakeTracker(), token="tok", chat_id="123", level="questions")
        result = n.send_question("issue-1", "What color?", "abc")
        assert result is True
        mock_post.assert_called_once()


class TestCompositeLevelPassthrough:
    def test_level_passed_to_children(self):
        """CompositeNotifier passes level kwarg to child notifiers."""
        received_levels = []

        class LevelCapture:
            notifications = []
            def notify(self, msg, *, level=NotificationLevel.ALL):
                received_levels.append(level)
            def start(self): pass
            def stop(self): pass

        comp = CompositeNotifier([LevelCapture(), LevelCapture()])
        comp.notify("test", level=NotificationLevel.ACTIONS)
        assert all(l == NotificationLevel.ACTIONS for l in received_levels)
        assert len(received_levels) == 2


class TestConfigLevelParsing:
    def test_notifier_config_default_level(self):
        from core.config.models import NotifierConfig
        nc = NotifierConfig(kind="telegram")
        assert nc.level == "all"

    def test_notifier_config_custom_level(self):
        from core.config.models import NotifierConfig
        nc = NotifierConfig(kind="telegram", level="questions")
        assert nc.level == "questions"

    def test_loader_parses_level(self, tmp_path):
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text(
            "---\n"
            "agent:\n  kind: claude-code\n"
            "tracker:\n  kind: static\n"
            "notifications:\n"
            "  - kind: webhook\n"
            "    url: http://example.com\n"
            "    level: actions\n"
            "---\nPrompt body\n"
        )
        from core.config.loader import load_workflow
        cfg = load_workflow(wf)
        assert len(cfg.notifications) == 1
        assert cfg.notifications[0].level == "actions"

    def test_loader_defaults_level_when_missing(self, tmp_path):
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text(
            "---\n"
            "agent:\n  kind: claude-code\n"
            "tracker:\n  kind: static\n"
            "notifications:\n"
            "  - kind: webhook\n"
            "    url: http://example.com\n"
            "---\nPrompt body\n"
        )
        from core.config.loader import load_workflow
        cfg = load_workflow(wf)
        assert cfg.notifications[0].level == "all"


class TestTelegramRelayLevelFiltering:
    @patch("host.watcher.HAS_REQUESTS", True)
    @patch("host.watcher.requests")
    def test_relay_filters_by_level(self, mock_req, tmp_path):
        from host.watcher.telegram_relay import TelegramRelay
        relay = TelegramRelay("tok", "123", "repo", tmp_path, level="actions")
        relay.notify("info msg")
        mock_req.post.assert_not_called()
        relay.notify("done!", level=NotificationLevel.ACTIONS)
        mock_req.post.assert_called_once()

    @patch("host.watcher.HAS_REQUESTS", True)
    @patch("host.watcher.requests")
    def test_relay_default_all(self, mock_req, tmp_path):
        from host.watcher.telegram_relay import TelegramRelay
        relay = TelegramRelay("tok", "123", "repo", tmp_path)
        relay.notify("info msg")
        mock_req.post.assert_called_once()
