"""Telegram communication: polling, sending notifications, Q&A questions, acks."""

import logging
import threading
from pathlib import Path
from typing import Optional

from host.constants import (
    TG_LONG_POLL_TIMEOUT_S, TG_HTTP_TIMEOUT_S, TG_POST_TIMEOUT_S,
    TG_MESSAGE_SOFT_LIMIT, TG_TRUNCATION_POINT,
)
from core.protocols import NotificationLevel, should_notify
from core.review import parse_nightshift_command
from adapters.notifiers._utils import redact_url

log = logging.getLogger("watcher")


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class TelegramRelay:
    """Telegram communication: polling, sending notifications, Q&A questions, acks."""

    def __init__(self, token: str, chat_id: str, project_name: str, sessions_dir: Path,
                 level: str = "all"):
        self.token = token
        self.chat_id = chat_id
        self.project_name = project_name
        self.sessions_dir = sessions_dir
        # Look up HAS_REQUESTS from the package so test patches on
        # host.watcher.HAS_REQUESTS take effect.
        self.enabled = _pkg().HAS_REQUESTS and bool(token and chat_id)
        self._offset = 0
        self._level = NotificationLevel[level.upper()]
        self._shutdown = threading.Event()

    def set_level(self, level: str):
        """Update the notification level (e.g. after config reload)."""
        self._level = NotificationLevel[level.upper()]

    def poll_all(self, paused: dict) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        """Single Telegram poll -- routes messages to Q&A answers or review commands."""
        qa: dict[str, str] = {}
        reviews: dict[str, tuple[str, str]] = {}
        if self._shutdown.is_set():
            return qa, reviews
        try:
            resp = _pkg().requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self._offset, "timeout": TG_LONG_POLL_TIMEOUT_S},
                timeout=TG_HTTP_TIMEOUT_S,
            )
            for u in resp.json().get("result", []):
                self._offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue
                self.route_message(msg, text, qa, reviews, paused)
        except Exception as e:
            log.debug(f"Telegram poll: {redact_url(e)}")
        return qa, reviews

    def route_message(self, msg: dict, text: str,
                      qa: dict[str, str],
                      reviews: dict[str, tuple[str, str]],
                      paused: dict):
        """Route a single Telegram message to Q&A or review."""
        rt = msg.get("reply_to_message", {})
        reply_msg_id = rt.get("message_id") if rt else None
        author = msg.get("from", {}).get("first_name", "Unknown")
        msg_id = msg.get("message_id")

        if reply_msg_id:
            # Check if reply is to a paused Q&A question
            for sid, info in paused.items():
                if info.get("tg_msg_id") == reply_msg_id:
                    qa[sid] = text
                    self.ack(msg_id, sid)
                    return
            # Otherwise check for @nightshift review command
            cmd = parse_nightshift_command(text)
            if cmd:
                matched_sid = self.match_session(rt.get("text", ""))
                if matched_sid:
                    reviews[matched_sid] = (text, author)
                    self.ack(msg_id, matched_sid)
        else:
            cmd = parse_nightshift_command(text)
            if cmd:
                matched_sid = self.match_session(text)
                if matched_sid:
                    reviews[matched_sid] = (text, author)
                    self.ack(msg_id, matched_sid)

    def match_session(self, text: str) -> Optional[str]:
        """Find a session ID mentioned in text."""
        if not self.sessions_dir.exists():
            return None
        for session_dir in self.sessions_dir.iterdir():
            if session_dir.is_dir() and session_dir.name in text:
                return session_dir.name
        return None

    def notify(self, text: str, *, level: NotificationLevel = NotificationLevel.ALL):
        """Send a plain notification to Telegram (no reply expected)."""
        if not self.enabled:
            return
        if not should_notify(self._level, level):
            return
        text = f"[{self.project_name}] {text}"
        if len(text) > TG_MESSAGE_SOFT_LIMIT:
            text = text[:TG_TRUNCATION_POINT] + "\n\n... (truncated, see watcher.log)"
        try:
            _pkg().requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                }, timeout=TG_POST_TIMEOUT_S,
            )
        except Exception as e:
            log.warning(f"Telegram notify failed: {redact_url(e)}")

    def send_question(self, sid: str, question: str, short_id: str) -> Optional[int]:
        """Send question to Telegram with force_reply. Returns message_id."""
        try:
            resp = _pkg().requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": (
                        f"[{self.project_name}] \u2753 *Question*\n"
                        f"*Issue:* `{short_id}`\n"
                        f"*Q:* {question}\n\n"
                        f"_Reply to answer._"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "force_reply": True, "selective": True,
                        "input_field_placeholder": "Answer...",
                    },
                }, timeout=TG_POST_TIMEOUT_S,
            )
            d = resp.json()
            return d["result"]["message_id"] if d.get("ok") else None
        except Exception as e:
            log.warning(f"Telegram send failed: {redact_url(e)}")
            return None

    def ack(self, reply_to: int, sid: str):
        """Send acknowledgement reply on Telegram."""
        try:
            _pkg().requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": f"[{self.project_name}] \u2705 Received for `{sid}`.",
                    "parse_mode": "Markdown",
                    "reply_to_message_id": reply_to,
                }, timeout=TG_POST_TIMEOUT_S,
            )
        except Exception as e:
            log.warning(f"Telegram ack failed: {redact_url(e)}")
