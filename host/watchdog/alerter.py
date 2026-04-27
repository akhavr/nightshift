"""Send alerts for watchdog issues."""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml

log = logging.getLogger("watchdog")

DEFAULT_CONFIG_PATH = Path.home() / ".nightshift" / "watchdog.yaml"
DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_ALERT_COOLDOWN_S = 300
HTTP_TIMEOUT_S = 10


@dataclass
class AlertConfig:
    """Watchdog configuration."""

    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    alert_cooldown_s: int = DEFAULT_ALERT_COOLDOWN_S
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "AlertConfig":
        """Load config from YAML file, falling back to env vars."""
        import os

        cfg = cls()
        config_path = path or DEFAULT_CONFIG_PATH
        if config_path.exists():
            try:
                data = yaml.safe_load(config_path.read_text()) or {}
                cfg.poll_interval_s = data.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
                cfg.alert_cooldown_s = data.get("alert_cooldown_s", DEFAULT_ALERT_COOLDOWN_S)

                for notif in data.get("notifications", []):
                    if notif.get("kind") == "telegram":
                        token = notif.get("token", "")
                        chat_id = notif.get("chat_id", "")
                        cfg.telegram_token = cls._resolve_env(token)
                        cfg.telegram_chat_id = cls._resolve_env(chat_id)
                        break
            except (yaml.YAMLError, OSError) as e:
                log.warning("Failed to load watchdog config %s: %s", config_path, e)

        if not cfg.telegram_token:
            cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not cfg.telegram_chat_id:
            cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        return cfg

    @staticmethod
    def _resolve_env(value: str) -> str:
        """Resolve $VAR references in value."""
        import os

        if value.startswith("$"):
            return os.environ.get(value[1:], "")
        return value


@dataclass
class Alerter:
    """Send alerts with cooldown to prevent spam."""

    config: AlertConfig
    last_alert: dict[str, float] = field(default_factory=dict)

    def should_alert(self, key: str) -> bool:
        """Check if enough time has passed since last alert for this key."""
        last = self.last_alert.get(key, 0)
        return time.time() - last > self.config.alert_cooldown_s

    def send(self, key: str, message: str) -> bool:
        """Send alert if cooldown has passed. Returns True if sent."""
        if not self.should_alert(key):
            log.debug("Alert suppressed (cooldown): %s", key)
            return False

        success = self._send_telegram(message)
        if success:
            self.last_alert[key] = time.time()
        return success

    def _send_telegram(self, message: str) -> bool:
        """Send message to Telegram."""
        if not self.config.telegram_token or not self.config.telegram_chat_id:
            log.warning("Telegram not configured, cannot send alert")
            return False

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage",
                json={
                    "chat_id": self.config.telegram_chat_id,
                    "text": f"🚨 [nightshift-watchdog] {message}",
                    "parse_mode": "Markdown",
                },
                timeout=HTTP_TIMEOUT_S,
            )
            if not resp.json().get("ok"):
                log.warning("Telegram alert failed: %s", resp.text)
                return False
            return True
        except requests.RequestException as e:
            log.warning("Telegram alert failed: %s", e)
            return False
