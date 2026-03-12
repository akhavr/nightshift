"""Telegram notifier with force_reply Q&A."""

import json
import logging
import os
import threading
import time
from typing import Optional

import requests
from core.protocols import Notifier, IssueTracker, SHORT_ID_LEN
from adapters.notifiers._utils import project_prefix

HTTP_REQUEST_TIMEOUT_S = 10   # Default timeout for outgoing HTTP calls
TG_LONG_POLL_TIMEOUT_S = 2   # Telegram getUpdates long-poll timeout
TG_POLL_HTTP_TIMEOUT_S = 7   # HTTP timeout for getUpdates (> long-poll)
TG_ERROR_BACKOFF_S = 5        # Sleep on poll error before retry

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, tracker: IssueTracker,
                 token: str | None = None, chat_id: str | None = None):
        self.tracker = tracker
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._offset = 0
        self._running = False

    def start(self):
        if not self.enabled: return
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self._running = False

    def notify(self, message: str) -> None:
        if not self.enabled: return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id,
                      "text": project_prefix(f"🤖 {message}"),
                      "parse_mode": "Markdown"}, timeout=HTTP_REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning(f"Telegram notify failed: {e}")

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        if not self.enabled: return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": project_prefix(
                        f"❓ *Question*\n*Issue:* `{short_id or issue_id[:SHORT_ID_LEN]}`\n"
                        f"*Q:* {question}\n\n_Reply to answer._"),
                    "parse_mode": "Markdown",
                    "reply_markup": {"force_reply": True, "selective": True,
                                     "input_field_placeholder": "Answer..."},
                }, timeout=HTTP_REQUEST_TIMEOUT_S)
            d = resp.json()
            if not d.get("ok"): return False
            with self._lock:
                self._pending[issue_id] = {
                    "msg_id": d["result"]["message_id"],
                    "answer": None, "event": threading.Event(),
                }
            return True
        except requests.RequestException as e:
            log.warning(f"Telegram send_question failed: {e}")
            return False

    def check_answer(self, issue_id: str) -> Optional[str]:
        with self._lock:
            p = self._pending.get(issue_id)
            if p and p["event"].is_set():
                self._pending.pop(issue_id, None)
                return p["answer"]
        return None

    def clear_pending(self, issue_id: str) -> None:
        with self._lock: self._pending.pop(issue_id, None)

    def _poll(self):
        while self._running:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"offset": self._offset, "timeout": TG_LONG_POLL_TIMEOUT_S,
                            "allowed_updates": json.dumps(["message"])},
                    timeout=TG_POLL_HTTP_TIMEOUT_S)
                for u in resp.json().get("result", []):
                    self._offset = u["update_id"] + 1
                    self._handle(u)
            except Exception as e:
                log.warning(f"Telegram: {e}"); time.sleep(TG_ERROR_BACKOFF_S)

    def _handle(self, u: dict):
        msg = u.get("message", {}); text = msg.get("text", "").strip()
        rt = msg.get("reply_to_message", {})
        if not text or not rt: return
        if str(msg.get("chat",{}).get("id")) != str(self.chat_id): return
        rid = rt.get("message_id")
        with self._lock:
            for iid, p in self._pending.items():
                if p["msg_id"] == rid:
                    self.tracker.add_comment(iid, f"👤 [via Telegram]: {text}")
                    p["answer"] = text; p["event"].set()
                    return
