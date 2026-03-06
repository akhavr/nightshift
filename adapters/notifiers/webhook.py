"""Generic webhook notifier."""

import os
from typing import Optional
import requests
from core.protocols import Notifier


class WebhookNotifier:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL", "")

    def notify(self, message: str) -> None:
        if self.url:
            try: requests.post(self.url, json={"text": message}, timeout=10)
            except Exception: pass

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.notify(f"❓ [{short_id}] {question}"); return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        return None

    def clear_pending(self, issue_id: str) -> None:
        pass
