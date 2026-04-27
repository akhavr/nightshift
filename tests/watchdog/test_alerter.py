"""Tests for watchdog alerter module."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from host.watchdog.alerter import Alerter, AlertConfig, DEFAULT_POLL_INTERVAL_S, DEFAULT_ALERT_COOLDOWN_S


def test_config_loads_from_yaml(tmp_path):
    """Config should load from YAML file."""
    config_file = tmp_path / "watchdog.yaml"
    config_file.write_text("""\
poll_interval_s: 120
alert_cooldown_s: 600
notifications:
  - kind: telegram
    token: test-token
    chat_id: "12345"
""")

    config = AlertConfig.load(config_file)
    assert config.poll_interval_s == 120
    assert config.alert_cooldown_s == 600
    assert config.telegram_token == "test-token"
    assert config.telegram_chat_id == "12345"


def test_config_resolves_env_vars(tmp_path, monkeypatch):
    """Config should resolve $VAR references."""
    monkeypatch.setenv("MY_TOKEN", "secret-token")
    monkeypatch.setenv("MY_CHAT", "999")

    config_file = tmp_path / "watchdog.yaml"
    config_file.write_text("""\
notifications:
  - kind: telegram
    token: $MY_TOKEN
    chat_id: $MY_CHAT
""")

    config = AlertConfig.load(config_file)
    assert config.telegram_token == "secret-token"
    assert config.telegram_chat_id == "999"


def test_config_falls_back_to_env(tmp_path, monkeypatch):
    """Config should fall back to env vars if not in file."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")

    config_file = tmp_path / "watchdog.yaml"
    config_file.write_text("poll_interval_s: 30\n")

    config = AlertConfig.load(config_file)
    assert config.telegram_token == "env-token"
    assert config.telegram_chat_id == "env-chat"


def test_config_defaults():
    """Config should have sensible defaults."""
    config = AlertConfig()
    assert config.poll_interval_s == DEFAULT_POLL_INTERVAL_S
    assert config.alert_cooldown_s == DEFAULT_ALERT_COOLDOWN_S


def test_alerter_respects_cooldown():
    """Alerter should suppress repeated alerts within cooldown."""
    config = AlertConfig(alert_cooldown_s=300)
    alerter = Alerter(config=config)

    assert alerter.should_alert("test-key") is True

    alerter.last_alert["test-key"] = 1000000000
    assert alerter.should_alert("test-key") is True

    import time
    alerter.last_alert["test-key"] = time.time()
    assert alerter.should_alert("test-key") is False


def test_sends_telegram_alert():
    """Alerter should send Telegram messages."""
    config = AlertConfig(
        telegram_token="test-token",
        telegram_chat_id="12345",
        alert_cooldown_s=0,
    )
    alerter = Alerter(config=config)

    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch("host.watchdog.alerter.requests.post", return_value=mock_response) as mock_post:
        result = alerter.send("test-key", "Test message")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "test-token" in call_args[0][0]
        assert call_args[1]["json"]["chat_id"] == "12345"
        assert "Test message" in call_args[1]["json"]["text"]


def test_alert_failure_handling():
    """Alerter should handle send failures gracefully."""
    config = AlertConfig(
        telegram_token="test-token",
        telegram_chat_id="12345",
        alert_cooldown_s=0,
    )
    alerter = Alerter(config=config)

    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": False}

    with patch("host.watchdog.alerter.requests.post", return_value=mock_response):
        result = alerter.send("test-key", "Test message")
        assert result is False


def test_alert_without_config():
    """Alerter should handle missing Telegram config."""
    config = AlertConfig()
    alerter = Alerter(config=config)

    result = alerter.send("test-key", "Test message")
    assert result is False


def test_config_handles_missing_file(tmp_path):
    """Config should handle missing config file."""
    config_file = tmp_path / "nonexistent.yaml"
    config = AlertConfig.load(config_file)
    assert config.poll_interval_s == DEFAULT_POLL_INTERVAL_S
