"""Reviewer verdict extraction and handling (approve/revise)."""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from host.constants import SHORT_ID_LEN, REVISE_PENDING_FILENAME
from host.session_utils import update_status as _update_status, clear_completed_at
from core.config import load_workflow
from core.protocols import NotificationLevel
from core.review import (
    parse_nightshift_command, parse_verdict, strip_nightshift_command,
    build_revise_prompt,
)
from host.watcher.lifecycle_comments import post_revise
from host.watcher.telegram_relay import TelegramRelay

log = logging.getLogger("watcher")

# Directory containing the host package (host/)
_HOST_DIR = Path(__file__).resolve().parent.parent


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class VerdictHandler:
    """Handles reviewer approve/revise verdicts and feedback collection."""

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

    def extract_reviewer_verdict(self, conv_log: Path, issue_id: str) -> Optional[str]:
        """Extract @nightshift approve/revise from reviewer's conversation log."""
        verdict = self._verdict_from_conversation(conv_log)
        if verdict:
            return verdict
        return self._verdict_from_tracker(issue_id)

    def _verdict_from_conversation(self, conv_log: Path) -> Optional[str]:
        """Check conversation log for verdict using flexible pattern matching."""
        if not conv_log.exists():
            return None
        for line in reversed(conv_log.read_text().strip().splitlines()):
            try:
                entry = json.loads(line)
                text = entry.get("content", "")
                verdict = parse_verdict(text)
                if verdict in ("approve", "revise", "reject"):
                    return verdict
            except (json.JSONDecodeError, KeyError) as e:
                log.debug(f"Failed to parse conversation log line: {e}")
                continue
        return None

    def _verdict_from_tracker(self, issue_id: str) -> Optional[str]:
        """Check tracker comments for verdict using flexible pattern matching."""
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
            for comment in reversed(comments[-5:] if len(comments) > 5 else comments):
                verdict = parse_verdict(comment.body)
                if verdict in ("approve", "revise", "reject"):
                    return verdict
        except Exception as e:
            log.warning(f"Tracker poll for reviewer verdict failed: {e}")
        return None

    def handle_reviewer_approve(self, coder_sid: str, coder_dir: Path, issue_id: str):
        """Reviewer approved -- transition coder to waiting:human-review."""
        if not coder_dir.exists():
            log.warning(f"[{coder_sid[:12]}] Coder session directory missing, skipping approve")
            return
        try:
            _update_status(coder_dir, "waiting:human-review")
            log.info(f"[{coder_sid}] Reviewer approved -> waiting:human-review")

            self.telegram.notify(
                f"\u2705 Automated review *approved* `{coder_sid}`.\n"
                f"Human review: `nightshift accept/reject/revise {issue_id}`",
                level=NotificationLevel.ACTIONS)

            self._post_approval_to_tracker(coder_sid, issue_id)
        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer approve: {e}")

    def _post_approval_to_tracker(self, coder_sid: str, issue_id: str):
        """Post approval comment to tracker."""
        try:
            tracker = self._get_tracker()
            tracker.add_comment(issue_id,
                "\U0001f916 **Automated review: APPROVED**\n\n"
                "Reviewer is satisfied with the changes. Awaiting human confirmation.\n\n"
                f"Review with: `nightshift accept/reject/revise {issue_id}`")
            tracker.sync()
        except Exception as e:
            log.warning(f"[{coder_sid}] Failed to post approval to tracker: {e}")

    def collect_reviewer_feedback(self, coder_sid: str, issue_id: str,
                                  review_dir: Path) -> list[str]:
        """Collect revision feedback from reviewer conversation and tracker."""
        parts = self._feedback_from_conversation(coder_sid, review_dir)
        parts.extend(self._feedback_from_tracker(coder_sid, issue_id))
        return parts or ["Reviewer requested revisions but did not provide specific feedback."]

    def _feedback_from_conversation(self, coder_sid: str, review_dir: Path) -> list[str]:
        """Extract feedback from reviewer conversation log."""
        parts = []
        conv_log = review_dir / "conversation.jsonl"
        if not conv_log.exists():
            return parts
        for line in conv_log.read_text().strip().splitlines():
            try:
                entry = json.loads(line)
                text = entry.get("content", "")
                if "@nightshift" in text.lower() and "revise" in text.lower():
                    cleaned = strip_nightshift_command(text)
                    if cleaned:
                        parts.append(cleaned)
            except (json.JSONDecodeError, KeyError) as e:
                log.debug(f"[{coder_sid}] Failed to parse reviewer feedback line: {e}")
                continue
        return parts

    def _feedback_from_tracker(self, coder_sid: str, issue_id: str) -> list[str]:
        """Extract feedback from tracker comments."""
        parts = []
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
            for comment in reversed(comments[-5:] if len(comments) > 5 else comments):
                cmd = parse_nightshift_command(comment.body)
                if cmd == "revise":
                    cleaned = strip_nightshift_command(comment.body)
                    if cleaned:
                        parts.append(cleaned)
                    break
        except Exception as e:
            log.warning(f"[{coder_sid}] Tracker poll for review feedback failed: {e}")
        return parts

    def handle_reviewer_revise(self, coder_sid: str, coder_dir: Path,
                               issue_id: str, review_dir: Path):
        """Reviewer requested revisions -- resume coder with feedback."""
        if not coder_dir.exists():
            log.warning(f"[{coder_sid[:12]}] Coder session directory missing, skipping revise")
            return
        try:
            parts = self.collect_reviewer_feedback(coder_sid, issue_id, review_dir)
            feedback = build_revise_prompt([], inline_feedback="\n".join(parts))
            (coder_dir / "resume-prompt.md").write_text(feedback)

            reason = "\n".join(parts)
            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue_id, "--resume",
            ]
            if not self._launch_background(cmd, coder_sid):
                log.warning(f"[{coder_sid}] Reviewer revise launch failed -- writing marker for retry")
                self._write_revise_pending_marker(coder_dir, issue_id, review_dir)
                return

            # Only update state after successful launch (SSM-7)
            clear_completed_at(coder_dir)
            _update_status(coder_dir, "working")
            self._recently_launched[coder_sid] = time.time()
            log.info(f"[{coder_sid}] Reviewer requested revisions -- resuming coder")
            self.telegram.notify(f"\U0001f504 Reviewer requested revisions for `{coder_sid}`. Coder resuming.",
                                level=NotificationLevel.ALL)
            post_revise(self._get_tracker, issue_id, coder_sid, reason)
        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer revise: {e}")

    def _write_revise_pending_marker(self, coder_dir: Path, issue_id: str, review_dir: Path):
        """Write marker file for SessionMonitor to retry the revise launch."""
        marker = coder_dir / REVISE_PENDING_FILENAME
        try:
            marker_data = {
                "issue_id": issue_id,
                "review_dir": str(review_dir),
            }
            marker.write_text(json.dumps(marker_data))
            log.info(f"[{coder_dir.name}] Wrote revise-pending.json for retry")
        except Exception as e:
            log.error(f"[{coder_dir.name}] Failed to write revise-pending marker: {e}")
