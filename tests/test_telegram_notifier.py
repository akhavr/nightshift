"""Tests for Telegram notifier secret redaction."""

import logging
import os
from unittest.mock import patch, MagicMock

import requests

from adapters.notifiers.telegram import TelegramNotifier


class FakeTracker:
    def add_comment(self, *a, **kw): pass
    def get_issue(self, *a, **kw): return None
    def list_issues(self, **kw): return []
    def get_comments(self, *a, **kw): return []
    def add_label(self, *a, **kw): pass
    def remove_label(self, *a, **kw): pass
    def sync(self): pass


class TestExceptionRedaction:
    """Test that exception logs redact sensitive data."""

    @patch("adapters.notifiers.telegram.requests.post")
    def test_exception_log_redacts_token_in_notify(self, mock_post, caplog):
        """When notify() fails, logged message contains /bot<REDACTED>/ not the actual token."""
        token = "8367824483:AAEnnIQeRDv-F6V9NxUYAwzvdxsqXc4Ef5E"
        exc_msg = f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot{token}/sendMessage"
        mock_post.side_effect = requests.RequestException(exc_msg)

        n = TelegramNotifier(FakeTracker(), token=token, chat_id="123")
        with caplog.at_level(logging.WARNING):
            n.notify("test message")

        assert len(caplog.records) == 1
        log_msg = caplog.records[0].message
        assert token not in log_msg
        assert "/bot<REDACTED>/" in log_msg

    @patch("adapters.notifiers.telegram.requests.post")
    def test_exception_log_redacts_token_in_send_question(self, mock_post, caplog):
        """When send_question() fails, logged message contains /bot<REDACTED>/ not the actual token."""
        token = "8367824483:AAEnnIQeRDv-F6V9NxUYAwzvdxsqXc4Ef5E"
        exc_msg = f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot{token}/sendMessage"
        mock_post.side_effect = requests.RequestException(exc_msg)

        n = TelegramNotifier(FakeTracker(), token=token, chat_id="123")
        with caplog.at_level(logging.WARNING):
            result = n.send_question("issue-1", "What next?", "abc")

        assert result is False
        assert len(caplog.records) == 1
        log_msg = caplog.records[0].message
        assert token not in log_msg
        assert "/bot<REDACTED>/" in log_msg

    @patch("adapters.notifiers.telegram.requests.get")
    def test_exception_log_redacts_token_in_poll(self, mock_get, caplog):
        """When _poll() fails, logged message redacts both token and query params."""
        token = "8367824483:AAEnnIQeRDv-F6V9NxUYAwzvdxsqXc4Ef5E"
        exc_msg = f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot{token}/getUpdates?offset=123&timeout=2"
        mock_get.side_effect = requests.RequestException(exc_msg)

        n = TelegramNotifier(FakeTracker(), token=token, chat_id="123")
        # Call _poll directly (don't start thread)
        n._running = True

        with caplog.at_level(logging.WARNING):
            # Run one poll iteration - it will catch exception and sleep
            # We need to stop it after one iteration
            def stop_after_error(*args, **kwargs):
                n._running = False
                raise requests.RequestException(exc_msg)
            mock_get.side_effect = stop_after_error
            n._poll()

        assert len(caplog.records) >= 1
        log_msg = caplog.records[0].message
        assert token not in log_msg
        assert "/bot<REDACTED>/" in log_msg
        assert "?<PARAMS>" in log_msg
