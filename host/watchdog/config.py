"""Load watchdog configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".nightshift" / "watchdog.yaml"


def _expand_env(value: Any) -> Any:
    """Resolve $VARS in scalar string values."""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:], "")
    return value


def _as_path(value: Any) -> Path | None:
    value = _expand_env(value)
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser()


@dataclass
class LlmConfig:
    provider: str = "none"
    model: str = "phi3:mini"
    api_key: str = ""
    base_url: str | None = None


@dataclass
class WatchConfig:
    interval_s: int = 60
    watcher_stale_s: int = 300
    log_lines: int = 100


@dataclass
class RulesConfig:
    error_threshold: int = 5
    repeat_threshold: int = 3


@dataclass
class TelegramNotifyConfig:
    token: str = ""
    chat_id: str = ""


@dataclass
class WebhookNotifyConfig:
    url: str = ""


@dataclass
class NotifyConfig:
    telegram: TelegramNotifyConfig = field(default_factory=TelegramNotifyConfig)
    webhook: WebhookNotifyConfig = field(default_factory=WebhookNotifyConfig)
    file: Path | None = None

    @property
    def webhook_url(self) -> str:
        return self.webhook.url

    @webhook_url.setter
    def webhook_url(self, value: str) -> None:
        self.webhook.url = value


@dataclass
class WatchdogConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def load_config(path: Path | None = None) -> WatchdogConfig:
    """Load watchdog.yaml using the schema from the issue."""
    cfg = WatchdogConfig()
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return cfg

    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, Mapping):
        raise ValueError("watchdog config root must be a mapping")

    llm = data.get("llm", {}) or {}
    watch = data.get("watch", {}) or {}
    rules = data.get("rules", {}) or {}
    notify = data.get("notify", {}) or {}
    telegram = notify.get("telegram", {}) or {}

    for section_name, section in (
        ("llm", llm),
        ("watch", watch),
        ("rules", rules),
        ("notify", notify),
        ("notify.telegram", telegram),
    ):
        if section and not isinstance(section, Mapping):
            raise ValueError(f"watchdog config section '{section_name}' must be a mapping")

    cfg.llm.provider = str(_expand_env(llm.get("provider", cfg.llm.provider)))
    cfg.llm.model = str(_expand_env(llm.get("model", cfg.llm.model)))
    cfg.llm.api_key = str(_expand_env(llm.get("api_key", cfg.llm.api_key)))
    base_url = _expand_env(llm.get("base_url", cfg.llm.base_url))
    cfg.llm.base_url = None if base_url in (None, "") else str(base_url)

    cfg.watch.interval_s = int(watch.get("interval_s", cfg.watch.interval_s))
    cfg.watch.watcher_stale_s = int(watch.get("watcher_stale_s", cfg.watch.watcher_stale_s))
    cfg.watch.log_lines = int(watch.get("log_lines", cfg.watch.log_lines))

    cfg.rules.error_threshold = int(rules.get("error_threshold", cfg.rules.error_threshold))
    cfg.rules.repeat_threshold = int(rules.get("repeat_threshold", cfg.rules.repeat_threshold))

    cfg.notify.telegram.token = str(_expand_env(telegram.get("token", cfg.notify.telegram.token)))
    cfg.notify.telegram.chat_id = str(_expand_env(telegram.get("chat_id", cfg.notify.telegram.chat_id)))
    webhook = notify.get("webhook", {}) or {}
    if isinstance(webhook, dict):
        cfg.notify.webhook.url = str(_expand_env(webhook.get("url", cfg.notify.webhook.url)))
    cfg.notify.file = _as_path(notify.get("file"))

    return cfg
