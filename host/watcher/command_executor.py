"""Execution of @nightshift CLI commands (accept/reject/revise) from reviews."""

import logging
import sys
import time
from pathlib import Path

from host.constants import (
    COMMAND_BACKOFF_BASE_S, COMMAND_BACKOFF_CAP_S, COMMAND_BACKOFF_CAP_CYCLES,
    SHORT_ID_LEN,
)
from host.session_utils import update_status as _update_status
from core.protocols import NotificationLevel
from core.review import collect_review_feedback, build_revise_prompt
from host.watcher.telegram_relay import TelegramRelay

log = logging.getLogger("watcher")

# Directory containing the host package (host/)
_HOST_DIR = Path(__file__).resolve().parent.parent


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class CommandExecutor:
    """Executes @nightshift CLI commands with backoff on failure."""

    def __init__(self, sessions_dir: Path, repo_dir: Path,
                 telegram: TelegramRelay,
                 get_tracker, recently_launched: dict,
                 launch_background):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._command_failures: dict[str, tuple[float, int]] = {}
        self._comment_counts: dict[str, int] = {}

    def is_in_cooldown(self, sid: str) -> bool:
        """Check if a session is still in backoff cooldown."""
        if sid not in self._command_failures:
            return False
        last_time, attempts = self._command_failures[sid]
        backoff_s = min(COMMAND_BACKOFF_BASE_S * (2 ** attempts), COMMAND_BACKOFF_CAP_S)
        return time.time() - last_time < backoff_s

    def do_revise(self, sid: str, issue_id: str, session_dir: Path):
        """Collect review feedback and relaunch agent."""
        try:
            tracker = self._get_tracker()
            review_comments = collect_review_feedback(tracker, issue_id, sync=False)

            if not review_comments:
                log.warning(f"[{sid}] No review feedback found -- skipping revise")
                return

            feedback = build_revise_prompt(review_comments)
            (session_dir / "resume-prompt.md").write_text(feedback)

            _update_status(session_dir, "working")

            self._comment_counts.pop(sid, None)
            self._recently_launched[sid] = time.time()

            log.info(f"[{sid}] Revising with {len(review_comments)} comment(s)")

            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue_id, "--resume",
            ]
            self._launch_background(cmd, sid)

        except Exception as e:
            log.error(f"[{sid}] Revise failed: {e}")

    def do_cli_command(self, sid: str, command: str, issue_id: str):
        """Run a CLI command (accept/reject) as a subprocess."""
        try:
            log.info(f"[{sid}] Running nightshift {command}")
            self._comment_counts.pop(sid, None)
            result = _pkg().subprocess.run(
                [sys.executable, str(_HOST_DIR / "cli.py"), command, issue_id],
                cwd=str(self.repo_dir), capture_output=True, text=True,
            )
            if result.returncode != 0:
                self._handle_cli_failure(sid, command, result)
            else:
                self._handle_cli_success(sid, command, result)
        except Exception as e:
            log.error(f"[{sid}] {command} failed: {e}")

    def _handle_cli_failure(self, sid: str, command: str, result):
        """Handle a failed CLI command with backoff tracking."""
        error_msg = (result.stderr.strip() + "\n" + result.stdout.strip()).strip()
        _, attempts = self._command_failures.get(sid, (0, 0))
        attempts += 1
        self._command_failures[sid] = (time.time(), attempts)
        backoff_m = min(2 ** attempts, COMMAND_BACKOFF_CAP_CYCLES)
        log.error(f"[{sid}] nightshift {command} failed (attempt {attempts}, "
                  f"retry in {backoff_m}m): {error_msg}")
        self.telegram.notify(f"\u26a0\ufe0f `nightshift {command}` failed for `{sid}` "
                             f"(attempt {attempts}, retry in {backoff_m}m):\n\n{error_msg}",
                             level=NotificationLevel.ALL)

    def _handle_cli_success(self, sid: str, command: str, result):
        """Handle a successful CLI command."""
        log.info(f"[{sid}] nightshift {command} completed")
        self.telegram.notify(f"\u2705 `nightshift {command}` completed for `{sid}`",
                             level=NotificationLevel.ALL)
        self._command_failures.pop(sid, None)
        if result.stdout.strip():
            log.info(f"[{sid}] {result.stdout.strip()}")

    def post_telegram_review_to_tracker(self, issue_id: str, text: str, author: str):
        """Post Telegram review reply as a tracker comment for audit trail."""
        try:
            tracker = self._get_tracker()
            comment_body = f"Review from {author} via Telegram:\n\n{text}"
            tracker.add_comment(issue_id, comment_body)
            log.info(f"Posted Telegram review to tracker for {issue_id[:SHORT_ID_LEN]}")
        except Exception as e:
            log.warning(f"Failed to post Telegram review to tracker: {e}")
