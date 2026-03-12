"""Answer collection from multiple sources (file, notifier, tracker).

Extracted from SessionRunner to separate the answer-polling concern.
"""

import logging
import time

from core.protocols import IssueTracker, Notifier
from core.state import StateManager

log = logging.getLogger(__name__)

ANSWER_POLL_S = 1
TRACKER_SYNC_INTERVAL = 30
ANSWER_PREVIEW_LEN = 200

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑")


def collect_answer(state_mgr: StateManager, notifier: Notifier,
                   tracker: IssueTracker, issue_id: str) -> str:
    """Poll all answer sources until one provides an answer.

    Sources checked in priority order:
    1. answer.txt (written by host watcher or CLI)
    2. Notifier (Telegram force_reply)
    3. Tracker comments (new non-bot comments)
    """
    last_count = len(tracker.get_comments(issue_id))
    sync_counter = 0
    while True:
        if a := state_mgr.check_answer():
            notifier.clear_pending(issue_id)
            return a
        if a := notifier.check_answer(issue_id):
            return a
        sync_counter += 1
        if sync_counter >= TRACKER_SYNC_INTERVAL:
            sync_counter = 0
            last_count = _check_tracker_comments(
                tracker, notifier, issue_id, last_count)
            if isinstance(last_count, str):
                return last_count  # got an answer
        time.sleep(ANSWER_POLL_S)  # container may be paused here


def _check_tracker_comments(tracker: IssueTracker, notifier: Notifier,
                            issue_id: str, last_count: int) -> int | str:
    """Check tracker for new non-bot comments. Returns answer str or updated count."""
    tracker.sync()
    comments = tracker.get_comments(issue_id)
    if len(comments) > last_count:
        latest = comments[-1].body
        if not any(latest.startswith(p) for p in BOT_PREFIXES):
            notifier.clear_pending(issue_id)
            return latest  # answer found
        return len(comments)
    return last_count
