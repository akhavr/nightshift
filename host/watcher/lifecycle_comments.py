"""Post lifecycle summary comments to the issue tracker.

Posts brief comments at key session transitions so that issue threads
reflect agent activity without overwhelming the tracker.  Only the
watcher calls these functions -- the container's StaticTracker stays
read-only.

Each function is idempotent with respect to a given session+event:
callers must track which events have already been posted to avoid
duplicates (see ``posted_events`` sets in the calling modules).
"""

import json
import logging
from pathlib import Path
from typing import Callable

from host.constants import SHORT_ID_LEN

log = logging.getLogger("watcher")

# Maximum characters of question text to include in a comment.
_QUESTION_PREVIEW_LEN = 200


def _safe_post(get_tracker: Callable, issue_id: str, body: str, event: str, sid: str) -> None:
    """Post a comment and sync, logging failures without raising."""
    try:
        tracker = get_tracker()
        tracker.add_comment(issue_id, body)
        tracker.sync()
    except Exception as e:
        log.warning(f"[{sid}] Failed to post lifecycle comment ({event}): {e}")


def post_start(get_tracker: Callable, issue_id: str, sid: str) -> None:
    """Post a comment when a session is auto-started by the watcher."""
    body = (
        f"Session `{sid}` started.\n\n"
        f"View progress: `nightshift logs {issue_id}` / "
        f"`nightshift history {issue_id}`"
    )
    _safe_post(get_tracker, issue_id, body, "start", sid)


def post_resume(get_tracker: Callable, issue_id: str, sid: str, reason: str,
                checkpoint_count: int) -> None:
    """Post a comment when a session is auto-resumed (orphan recovery)."""
    body = (
        f"Session `{sid}` resumed — reason: {reason}. "
        f"Checkpoint count: {checkpoint_count}.\n\n"
        f"View progress: `nightshift logs {issue_id}` / "
        f"`nightshift history {issue_id}`"
    )
    _safe_post(get_tracker, issue_id, body, "resume", sid)


def post_question(get_tracker: Callable, issue_id: str, sid: str,
                  question: str) -> None:
    """Post a comment when the agent is blocked on a question."""
    truncated = question[:_QUESTION_PREVIEW_LEN]
    if len(question) > _QUESTION_PREVIEW_LEN:
        truncated += "..."
    body = (
        f"Session `{sid}` blocked on question:\n\n"
        f"> {truncated}\n\n"
        f"Answer via: `nightshift answer {issue_id} \"<answer>\"`"
    )
    _safe_post(get_tracker, issue_id, body, "question", sid)


def post_done(get_tracker: Callable, issue_id: str, sid: str,
              checkpoint_count: int) -> None:
    """Post a comment when the session completes (enters waiting:review)."""
    body = (
        f"Session `{sid}` complete. Checkpoints: {checkpoint_count}.\n\n"
        f"View logs: `nightshift logs {issue_id}` / "
        f"`nightshift history {issue_id}`"
    )
    _safe_post(get_tracker, issue_id, body, "done", sid)


def post_revise(get_tracker: Callable, issue_id: str, sid: str,
                reason: str) -> None:
    """Post a comment when a session is sent back for revision."""
    truncated = reason[:_QUESTION_PREVIEW_LEN]
    if len(reason) > _QUESTION_PREVIEW_LEN:
        truncated += "..."
    body = (
        f"Session `{sid}` sent back for revision:\n\n"
        f"> {truncated}\n\n"
        f"View progress: `nightshift logs {issue_id}` / "
        f"`nightshift history {issue_id}`"
    )
    _safe_post(get_tracker, issue_id, body, "revise", sid)


def read_checkpoint_count(session_dir: Path) -> int:
    """Read checkpoint count from state.json, returning 0 on failure."""
    try:
        state = json.loads((session_dir / "state.json").read_text())
        return len(state.get("checkpoints", []))
    except (json.JSONDecodeError, OSError, KeyError) as e:
        log.debug(f"Could not read checkpoint count from {session_dir}: {e}")
        return 0
