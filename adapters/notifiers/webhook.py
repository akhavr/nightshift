"""Generic webhook notifier."""

import logging
import os
from typing import Optional
import requests
from core.protocols import Notifier
from adapters.notifiers._utils import project_prefix

HTTP_REQUEST_TIMEOUT_S = 10  # Default timeout for outgoing HTTP calls

log = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL", "")

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def notify(self, message: str) -> None:
        if self.url:
            try: requests.post(self.url, json={"text": project_prefix(message)}, timeout=HTTP_REQUEST_TIMEOUT_S)
            except Exception as e:
                log.warning(f"Webhook notify failed: {e}")

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.notify(f"❓ [{short_id}] {question}"); return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        return None

    def clear_pending(self, issue_id: str) -> None:
        pass
