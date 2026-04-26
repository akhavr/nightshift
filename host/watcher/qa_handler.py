"""Q&A pause/unpause cycle: detects waiting containers, delivers answers."""

import json
import logging
import threading
from pathlib import Path

from host.constants import (
    PRE_PAUSE_DELAY_S, STILL_WAITING_LOG_INTERVAL_S, SHORT_ID_LEN, LOG_PREVIEW_LEN,
)
from host.watcher.lifecycle_comments import post_question
from host.watcher.telegram_relay import TelegramRelay
from host.session_utils import read_state

log = logging.getLogger("watcher")

# Valid states for receiving an answer
ANSWER_VALID_STATES = {"waiting:question"}


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class QAHandler:
    """Q&A pause/unpause cycle: detects waiting containers, delivers answers."""

    def __init__(self, sessions_dir: Path, telegram: TelegramRelay,
                 get_tracker=None, shutdown_event: threading.Event | None = None):
        self.sessions_dir = sessions_dir
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._shutdown = shutdown_event or threading.Event()
        self._paused: dict[str, dict] = {}
        self._posted_question: set[str] = set()

    def scan_for_waiting(self):
        """Detect new waiting.json files -> pause those containers."""
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
                except (json.JSONDecodeError, OSError) as e:
                    log.warning(f"[{sid}] Failed to read waiting.json: {e}")
                    continue

                container = f"nightshift-{sid}"

                # Brief delay to let container finish writing state
                # Use shutdown_event.wait() so Ctrl-C can interrupt
                if self._shutdown.wait(timeout=PRE_PAUSE_DELAY_S):
                    return

                if _pkg().docker_pause(container):
                    self._paused[sid] = {
                        "question": data.get("question", ""),
                        "issue_id": data.get("issue_id", ""),
                        "container": container,
                        "dir": session_dir,
                        "paused_at": _pkg().time.time(),
                        "tg_msg_id": None,
                    }
                    log.info(f"[{sid}] Paused. Question: {data.get('question', '')[:LOG_PREVIEW_LEN]}")

                    # Forward question to Telegram if not already sent by container
                    if self.telegram.enabled and data.get("question"):
                        msg_id = self.telegram.send_question(
                            sid, data["question"], data.get("issue_id", "")[:SHORT_ID_LEN]
                        )
                        self._paused[sid]["tg_msg_id"] = msg_id

                    # Post question comment to tracker (once per session)
                    issue_id = data.get("issue_id", "")
                    if self._get_tracker and issue_id and sid not in self._posted_question:
                        self._posted_question.add(sid)
                        post_question(self._get_tracker, issue_id, sid,
                                      data.get("question", ""))
                else:
                    log.warning(f"[{sid}] Pause failed -- container will poll internally")

    def _is_valid_answer_state(self, session_dir: Path) -> bool:
        """Check if session is in a valid state to receive an answer."""
        state_file = session_dir / "state.json"
        if not state_file.exists():
            return True  # No state file = permissive (legacy behavior)
        try:
            state = read_state(session_dir)
            status = state.get("status", "")
            return status in ANSWER_VALID_STATES
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to read state for answer validation: {e}")
            return True  # Permissive on read failure

    def check_for_answers(self, tg_replies: dict[str, str]):
        """Check for answers (Telegram + CLI), write answer.txt, unpause."""
        for sid, info in list(self._paused.items()):
            answer_file = info["dir"] / "answer.txt"

            # Check if someone wrote answer.txt directly (via CLI)
            if answer_file.exists():
                log.info(f"[{sid}] answer.txt found (via CLI). Unpausing.")
                _pkg().docker_unpause(info["container"])
                del self._paused[sid]
                self._posted_question.discard(sid)
                continue

            # Check Telegram replies
            if sid in tg_replies:
                # Validate state before delivering answer
                if not self._is_valid_answer_state(info["dir"]):
                    log.warning(f"[{sid}] Skipping answer delivery: invalid state")
                    continue

                answer = tg_replies[sid]
                log.info(f"[{sid}] Telegram reply: {answer[:LOG_PREVIEW_LEN]}")
                answer_file.write_text(answer)
                _pkg().docker_unpause(info["container"])
                log.info(f"[{sid}] Unpaused.")
                del self._paused[sid]
                self._posted_question.discard(sid)
                continue

            # Log periodic status
            elapsed = _pkg().time.time() - info["paused_at"]
            if int(elapsed) % STILL_WAITING_LOG_INTERVAL_S == 0 and int(elapsed) > 0:
                log.info(f"[{sid}] Still waiting ({elapsed/60:.0f}m)")
