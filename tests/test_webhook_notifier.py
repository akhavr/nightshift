"""Tests for Webhook notifier secret redaction."""

import logging
from unittest.mock import patch

import requests

from adapters.notifiers.webhook import WebhookNotifier


class TestExceptionRedaction:
    """Test that exception logs redact sensitive data."""

    @patch("adapters.notifiers.webhook.requests.post")
    def test_exception_log_redacts_url(self, mock_post, caplog):
        """When notify() fails, logged message contains ?<PARAMS> not actual query string."""
        webhook_url = "https://hooks.slack.com/services/T00/B00/xxxyyyzzz123?token=secret&channel=foo"
        exc_msg = f"HTTPSConnectionPool(host='hooks.slack.com', port=443): Max retries exceeded with url: {webhook_url}"
        mock_post.side_effect = requests.RequestException(exc_msg)

        n = WebhookNotifier(url=webhook_url)
        with caplog.at_level(logging.WARNING):
            n.notify("test message")

        assert len(caplog.records) == 1
        log_msg = caplog.records[0].message
        assert "token=secret" not in log_msg
        assert "channel=foo" not in log_msg
        assert "?<PARAMS>" in log_msg

    @patch("adapters.notifiers.webhook.requests.post")
    def test_exception_log_redacts_query_params_only(self, mock_post, caplog):
        """Verify that only query params are redacted, not the base URL."""
        webhook_url = "https://example.com/webhook?secret=abc123"
        exc_msg = f"Connection refused: {webhook_url}"
        mock_post.side_effect = requests.RequestException(exc_msg)

        n = WebhookNotifier(url=webhook_url)
        with caplog.at_level(logging.WARNING):
            n.notify("test message")

        assert len(caplog.records) == 1
        log_msg = caplog.records[0].message
        assert "secret=abc123" not in log_msg
        # Base URL should remain visible
        assert "example.com/webhook" in log_msg
        assert "?<PARAMS>" in log_msg
