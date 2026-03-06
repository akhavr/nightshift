#!/usr/bin/env python3
"""Host watcher — pauses idle containers, collects answers via Telegram + file.

Tracker-agnostic: communicates with containers ONLY via files in the shared
session directory (waiting.json / answer.txt). Optionally polls Telegram
for replies. Never imports or calls any tracker.

    python host/watcher.py --sessions-dir .agent-worker/sessions
"""

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [watcher] %(message)s")
log = logging.getLogger("watcher")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class HostWatcher:
    """Monitors session dirs, pauses containers, polls Telegram, writes answers.

    Zero tracker coupling. The contract is:
    - Container writes /session/waiting.json when it needs an answer.
    - Watcher writes /session/answer.txt when it has one.
    - Container reads answer.txt and continues.

    Answer sources (checked in order):
    1. Telegram reply (if configured)
    2. Manual: user runs `cli.py answer <id> "text"` which writes answer.txt directly
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir

        # Telegram config (optional)
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.tg_enabled = HAS_REQUESTS and bool(self.tg_token and self.tg_chat)
        self._tg_offset = 0

        # Track paused sessions: session_id -> metadata
        self._paused: dict[str, dict] = {}

    def run(self):
        log.info(f"Watching {self.sessions_dir}")
        if self.tg_enabled:
            log.info("Telegram polling enabled")
        else:
            log.info("Telegram not configured — answers via CLI only")

        while True:
            self._scan_for_waiting()
            self._check_for_answers()
            time.sleep(2)

    def _scan_for_waiting(self):
        """Detect new waiting.json files → pause those containers."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            waiting_file = session_dir / "waiting.json"

            if waiting_file.exists() and sid not in self._paused:
                try:
                    data = json.loads(waiting_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                container = f"agent-worker-{sid}"

                # Brief delay to let container finish writing state
                time.sleep(1)

                if self._docker_pause(container):
                    self._paused[sid] = {
                        "question": data.get("question", ""),
                        "issue_id": data.get("issue_id", ""),
                        "container": container,
                        "dir": session_dir,
                        "paused_at": time.time(),
                        "tg_msg_id": None,
                    }
                    log.info(f"[{sid}] Paused. Question: {data.get('question', '')[:60]}")

                    # Forward question to Telegram if not already sent by container
                    if self.tg_enabled and data.get("question"):
                        msg_id = self._tg_send_question(
                            sid, data["question"], data.get("issue_id", "")[:12]
                        )
                        self._paused[sid]["tg_msg_id"] = msg_id
                else:
                    log.warning(f"[{sid}] Pause failed — container will poll internally")

    def _check_for_answers(self):
        """Check Telegram for replies, write answer.txt, unpause."""
        tg_replies = self._poll_telegram() if self.tg_enabled else {}

        for sid, info in list(self._paused.items()):
            answer_file = info["dir"] / "answer.txt"

            # Check if someone wrote answer.txt directly (via CLI)
            if answer_file.exists():
                log.info(f"[{sid}] answer.txt found (via CLI). Unpausing.")
                self._docker_unpause(info["container"])
                del self._paused[sid]
                continue

            # Check Telegram replies
            if sid in tg_replies:
                answer = tg_replies[sid]
                log.info(f"[{sid}] Telegram reply: {answer[:60]}")
                answer_file.write_text(answer)
                self._docker_unpause(info["container"])
                log.info(f"[{sid}] Unpaused.")
                del self._paused[sid]
                continue

            # Log periodic status
            elapsed = time.time() - info["paused_at"]
            if int(elapsed) % 300 == 0 and int(elapsed) > 0:
                log.info(f"[{sid}] Still waiting ({elapsed/60:.0f}m)")

    # --- Docker ---

    def _docker_pause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "pause", container], capture_output=True,
        ).returncode == 0

    def _docker_unpause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "unpause", container], capture_output=True,
        ).returncode == 0

    # --- Telegram (self-contained, no tracker imports) ---

    def _tg_send_question(self, sid: str, question: str, short_id: str) -> Optional[int]:
        """Send question to Telegram with force_reply. Returns message_id."""
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": (
                        f"❓ *Question*\n"
                        f"*Issue:* `{short_id}`\n"
                        f"*Q:* {question}\n\n"
                        f"_Reply to answer._"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "force_reply": True, "selective": True,
                        "input_field_placeholder": "Answer...",
                    },
                }, timeout=10,
            )
            d = resp.json()
            return d["result"]["message_id"] if d.get("ok") else None
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return None

    def _poll_telegram(self) -> dict[str, str]:
        """Fetch Telegram updates, match replies to paused sessions."""
        replies: dict[str, str] = {}
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                params={"offset": self._tg_offset, "timeout": 1}, timeout=5,
            )
            for u in resp.json().get("result", []):
                self._tg_offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                rt = msg.get("reply_to_message", {})

                if not text or not rt:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(self.tg_chat):
                    continue

                reply_msg_id = rt.get("message_id")

                # Match by Telegram message_id
                for sid, info in self._paused.items():
                    if info.get("tg_msg_id") == reply_msg_id:
                        replies[sid] = text
                        self._tg_ack(msg.get("message_id"), sid)
                        break

        except Exception as e:
            log.warning(f"Telegram poll: {e}")

        return replies

    def _tg_ack(self, reply_to: int, sid: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": f"✅ Answer received for `{sid}`. Unpausing.",
                    "parse_mode": "Markdown",
                    "reply_to_message_id": reply_to,
                }, timeout=10,
            )
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description="Host watcher — pause/unpause containers")
    p.add_argument("--sessions-dir", required=True, help=".agent-worker/sessions path")
    a = p.parse_args()
    HostWatcher(Path(a.sessions_dir)).run()

if __name__ == "__main__":
    main()
