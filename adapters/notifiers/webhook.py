"""Generic webhook notifier."""

import logging
import os
from typing import Optional
import requests
from core.protocols import Notifier, NotificationLevel, should_notify
from adapters.notifiers._utils import project_prefix, redact_url

HTTP_REQUEST_TIMEOUT_S = 10  # Default timeout for outgoing HTTP calls

log = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(self, url: str | None = None, level: str = "all"):
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL", "")
        self._level = NotificationLevel[level.upper()]

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def notify(self, message: str, *, level: NotificationLevel = NotificationLevel.ALL) -> None:
        if not should_notify(self._level, level): return
        if self.url:
            try: requests.post(self.url, json={"text": project_prefix(message)}, timeout=HTTP_REQUEST_TIMEOUT_S)
            except Exception as e:
                log.warning(f"Webhook notify failed: {redact_url(e)}")

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.notify(f"❓ [{short_id}] {question}", level=NotificationLevel.QUESTIONS); return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        return None

    def clear_pending(self, issue_id: str) -> None:
        pass
