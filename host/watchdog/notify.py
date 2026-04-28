"""Alert delivery backends for watchdog anomalies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from host.watchdog.config import (
    NotifyConfig,
    TelegramNotifyConfig,
    WatchdogConfig,
    WebhookNotifyConfig,
)
from host.watchdog.rules import Anomaly


def _render_message(project: str, anomalies: Iterable[Anomaly], llm_summary: str) -> str:
    parts = [f"Watchdog alert for {project}"]
    for anomaly in anomalies:
        parts.append(f"- {anomaly.type}: {anomaly.message}")
    if llm_summary:
        parts.append("")
        parts.append(llm_summary)
    return "\n".join(parts)


def _send_telegram(text: str, telegram: TelegramNotifyConfig) -> bool:
    if not telegram.token or not telegram.chat_id:
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{telegram.token}/sendMessage",
        json={
            "chat_id": telegram.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    return bool(resp.json().get("ok"))


def _send_webhook(text: str, url: str) -> bool:
    if not url:
        return False
    resp = requests.post(url, json={"text": text}, timeout=30)
    return 200 <= resp.status_code < 300


def _send_file(text: str, path: Path | str | None) -> bool:
    if path is None:
        return False
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        fh.write("\n")
    return True


def send_alert(project: str, anomalies: Iterable[Anomaly], llm_summary: str, config: WatchdogConfig) -> bool:
    """Deliver a watchdog alert using configured backends."""
    text = _render_message(project, anomalies, llm_summary)
    sent = False

    if config.notify.telegram.token and config.notify.telegram.chat_id:
        sent = _send_telegram(text, config.notify.telegram) or sent

    if config.notify.webhook.url:
        sent = _send_webhook(text, config.notify.webhook.url) or sent

    if config.notify.file is not None:
        sent = _send_file(text, config.notify.file) or sent

    return sent
