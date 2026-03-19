"""Live issue sync — bidirectional file-based sync between host and container.

Reads (host -> container):
  Re-dumps issue.json to session dirs so the container sees new comments.

Writes (container -> host):
  Processes tracker-outbox.jsonl entries, applying them via the real tracker.
"""

import json
import logging
from pathlib import Path

from core.constants import TRACKER_OUTBOX_FILENAME
from host.session_utils import read_state
from host.issue_dump import redump_issue

log = logging.getLogger("watcher")

_WORKING_STATUSES = ("working", "starting", "waiting:answer")


def process_outbox(session_dir: Path, tracker) -> int:
    """Read and apply outbox entries from a single session dir.

    Returns the number of operations processed.
    """
    outbox_path = session_dir / TRACKER_OUTBOX_FILENAME
    if not outbox_path.exists():
        return 0

    try:
        raw = outbox_path.read_text()
    except OSError as e:
        log.warning(f"[{session_dir.name}] Failed to read outbox: {e}")
        return 0

    if not raw.strip():
        return 0

    processed = 0
    for line_no, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning(f"[{session_dir.name}] Bad outbox line {line_no}: {e}")
            continue
        if _apply_outbox_entry(session_dir.name, tracker, entry):
            processed += 1

    # Truncate the outbox after processing
    try:
        outbox_path.write_text("")
    except OSError as e:
        log.warning(f"[{session_dir.name}] Failed to truncate outbox: {e}")

    if processed:
        log.info(f"[{session_dir.name}] Processed {processed} outbox entries")
    return processed


def _apply_outbox_entry(sid: str, tracker, entry: dict) -> bool:
    """Apply a single outbox entry to the real tracker. Returns True on success."""
    op = entry.get("op")
    issue_id = entry.get("issue_id", "")
    try:
        if op == "comment":
            tracker.add_comment(issue_id, entry.get("text", ""))
        elif op == "set_status":
            tracker.set_status(issue_id, entry.get("status", ""))
        elif op == "label_add":
            tracker.add_label(issue_id, entry.get("label", ""))
        elif op == "label_rm":
            tracker.remove_label(issue_id, entry.get("label", ""))
        else:
            log.warning(f"[{sid}] Unknown outbox op: {op}")
            return False
    except Exception as e:
        log.warning(f"[{sid}] Failed to apply outbox op {op}: {e}")
        return False
    return True


def sync_sessions(sessions_dir: Path, tracker) -> None:
    """Process outbox and re-dump issue.json for all active sessions.

    Called once per watcher loop iteration.
    """
    if not sessions_dir.exists():
        return

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        state_path = session_dir / "state.json"
        if not state_path.exists():
            continue

        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{session_dir.name}] Failed to read state for issue sync: {e}")
            continue

        status = state.get("status", "")
        issue_id = state.get("issue_id", "")
        if not issue_id:
            continue

        # Process outbox for any session that has one (even non-working)
        process_outbox(session_dir, tracker)

        # Re-dump issue.json only for active sessions
        if status in _WORKING_STATUSES:
            try:
                redump_issue(tracker, issue_id, session_dir)
            except Exception as e:
                log.warning(f"[{session_dir.name}] Re-dump failed: {e}")
