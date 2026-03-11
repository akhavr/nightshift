"""Generic webhook notifier."""

import logging
import os
from typing import Optional
import requests
from core.protocols import Notifier

log = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL", "")
        self._project = os.environ.get("PROJECT_NAME", "")

    def _prefix(self, text: str) -> str:
        if self._project:
            return f"[{self._project}] {text}"
        return text

    def notify(self, message: str) -> None:
        if self.url:
            try: requests.post(self.url, json={"text": self._prefix(message)}, timeout=10)
            except Exception as e:
                log.warning(f"Webhook notify failed: {e}")

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.notify(f"❓ [{short_id}] {question}"); return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        return None

    def clear_pending(self, issue_id: str) -> None:
        pass
