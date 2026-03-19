"""Live issue sync — bidirectional file-based sync between host and container.

Reads (host -> container):
  Re-dumps issue.json to session dirs so the container sees new comments.

Writes (container -> host):
  Processes tracker-outbox.jsonl entries, applying them via the real tracker.
"""

import json
import logging
from pathlib import Path

from core.constants import TRACKER_OUTBOX_FILENAME, TRACKER_OUTBOX_PROCESSING
from host.session_utils import read_state
from host.issue_dump import redump_issue

log = logging.getLogger("watcher")

_WORKING_STATUSES = ("working", "starting", "waiting:answer")


def process_outbox(session_dir: Path, tracker) -> int:
    """Read and apply outbox entries from a single session dir.

    Uses atomic rename to avoid losing entries the container appends
    concurrently: rename outbox -> .processing, process, then delete.
    If a .processing file already exists (crash recovery), process it first.

    Returns the number of operations processed.
    """
    processing_path = session_dir / TRACKER_OUTBOX_PROCESSING
    outbox_path = session_dir / TRACKER_OUTBOX_FILENAME

    # Crash recovery: process leftover .processing file from a previous cycle
    total = _process_file(session_dir, processing_path, tracker)

    # Atomically claim the outbox by renaming it
    if outbox_path.exists():
        try:
            outbox_path.rename(processing_path)
        except OSError as e:
            log.warning(f"[{session_dir.name}] Failed to rename outbox for processing: {e}")
            return total
        total += _process_file(session_dir, processing_path, tracker)

    return total


def _process_file(session_dir: Path, path: Path, tracker) -> int:
    """Process all entries in a single outbox file, then delete it."""
    if not path.exists():
        return 0

    try:
        raw = path.read_text()
    except OSError as e:
        log.warning(f"[{session_dir.name}] Failed to read {path.name}: {e}")
        return 0

    if not raw.strip():
        try:
            path.unlink()
        except OSError as e:
            log.warning(f"[{session_dir.name}] Failed to delete empty {path.name}: {e}")
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

    # Delete the processed file
    try:
        path.unlink()
    except OSError as e:
        log.warning(f"[{session_dir.name}] Failed to delete {path.name}: {e}")

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
