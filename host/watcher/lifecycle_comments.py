"""Post lifecycle summary comments to the issue tracker.

Posts brief comments at key session transitions so that issue threads
reflect agent activity without overwhelming the tracker.  Only the
watcher calls these functions -- the container's StaticTracker stays
read-only.

Each function is idempotent with respect to a given session+event:
callers must track which events have already been posted to avoid
duplicates (see ``posted_events`` sets in the calling modules).
"""

import logging
from pathlib import Path
from typing import Callable

from host.session_utils import read_state

log = logging.getLogger("watcher")

# Maximum characters of preview text to include in a comment.
_PREVIEW_LEN = 200


def _truncate(text: str, max_len: int = _PREVIEW_LEN) -> str:
    """Truncate text to max_len, appending '...' if shortened."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _safe_post(get_tracker: Callable, issue_id: str, body: str, event: str, sid: str) -> None:
    """Post a comment, logging failures without raising.

    Does NOT call tracker.sync() — the watcher's main loop syncs
    periodically via _maybe_sync_tracker(), avoiding extra lock
    acquisitions on every lifecycle comment.
    """
    try:
        tracker = get_tracker()
        tracker.add_comment(issue_id, body)
    except Exception as e:
        log.warning(f"[{sid}] Failed to post lifecycle comment ({event}): {e}")


def post_start(get_tracker: Callable, issue_id: str, sid: str,
               title: str = "") -> None:
    """Post a comment when a session is auto-started by the watcher."""
    working_on = f", working on: {_truncate(title)}" if title else ""
    body = (
        f"Session `{sid}` started{working_on}.\n\n"
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
    body = (
        f"Session `{sid}` blocked on question:\n\n"
        f"> {_truncate(question)}\n\n"
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
    body = (
        f"Session `{sid}` sent back for revision:\n\n"
        f"> {_truncate(reason)}\n\n"
        f"View progress: `nightshift logs {issue_id}` / "
        f"`nightshift history {issue_id}`"
    )
    _safe_post(get_tracker, issue_id, body, "revise", sid)


def read_checkpoint_count(session_dir: Path) -> int:
    """Read checkpoint count from state.json, returning 0 on failure."""
    try:
        state = read_state(session_dir)
        return len(state.get("checkpoints", []))
    except (OSError, KeyError, ValueError) as e:
        log.debug(f"Could not read checkpoint count from {session_dir}: {e}")
        return 0
