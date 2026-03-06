"""Composite notifier — broadcasts to all, Q&A through primary."""

from typing import Optional
from core.protocols import Notifier


class CompositeNotifier:
    """Wraps multiple notifiers. First with round-trip Q&A is primary."""

    def __init__(self, notifiers: list):
        self.notifiers = notifiers
        # Primary = first notifier that can do Q&A (Telegram, not webhook)
        # Webhooks return False from send_question, indicating no round-trip
        self._primary = notifiers[0] if notifiers else None

    def notify(self, message: str) -> None:
        for n in self.notifiers:
            n.notify(message)

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        # Send through primary (Telegram etc.) for Q&A
        sent = False
        if self._primary:
            sent = self._primary.send_question(issue_id, question, short_id)
        # Also broadcast to all others (one-way)
        for n in self.notifiers:
            if n is not self._primary:
                n.send_question(issue_id, question, short_id)
        return sent

    def check_answer(self, issue_id: str) -> Optional[str]:
        if self._primary:
            return self._primary.check_answer(issue_id)
        return None

    def clear_pending(self, issue_id: str) -> None:
        if self._primary:
            self._primary.clear_pending(issue_id)

    def start(self):
        for n in self.notifiers:
            if hasattr(n, "start"):
                n.start()

    def stop(self):
        for n in self.notifiers:
            if hasattr(n, "stop"):
                n.stop()
