"""Live issue sync — bidirectional file-based sync between host and container.

Reads (host -> container):
  Re-dumps issue.json to session dirs so the container sees new comments.
  Throttled to ISSUE_REDUMP_INTERVAL_S per session to reduce git-bug lock
  contention (each redump acquires the lock twice: get_issue + get_comments).

Writes (container -> host):
  Processes tracker-outbox.jsonl entries, applying them via the real tracker.
"""

import json
import logging
import re
import time
from pathlib import Path

from core.constants import TRACKER_OUTBOX_FILENAME, TRACKER_OUTBOX_PROCESSING
from host.constants import ISSUE_REDUMP_INTERVAL_S
from host.session_utils import read_state
from host.issue_dump import redump_issue

log = logging.getLogger("watcher")

# Tracks last redump time per session ID to enforce the throttle.
_last_redump: dict[str, float] = {}

VALID_OPS = {"add_comment", "set_status", "add_label", "remove_label"}
_LEGACY_OP_ALIASES = {
    "comment": "add_comment",
    "label_add": "add_label",
    "label_rm": "remove_label",
}
ISSUE_ID_PATTERN = re.compile(r"^[a-f0-9]{8,64}$")

_WORKING_STATUSES = ("working", "starting", "waiting:answer")


def _validate_outbox_entry(entry: dict) -> None:
    """Validate a single outbox entry before execution.

    The host still accepts legacy op names emitted by the current StaticTracker
    implementation, but it rejects malformed payloads before tracker calls.
    """
    if not isinstance(entry, dict):
        raise ValueError("Outbox entry must be a JSON object")

    missing = [field for field in ("op", "issue_id") if field not in entry]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    op = entry.get("op")
    if not isinstance(op, str):
        raise ValueError(f"Unknown op: {op}")
    if op not in VALID_OPS and op not in _LEGACY_OP_ALIASES:
        raise ValueError(f"Unknown op: {op}")

    issue_id = entry.get("issue_id")
    if not isinstance(issue_id, str) or not ISSUE_ID_PATTERN.fullmatch(issue_id):
        raise ValueError(f"Invalid issue_id: {issue_id}")


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
        try:
            _validate_outbox_entry(entry)
        except ValueError as e:
            log.warning(f"[{session_dir.name}] Invalid outbox entry on line {line_no}: {e}; entry={entry}")
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
        if op in {"comment", "add_comment"}:
            tracker.add_comment(issue_id, entry.get("text", ""))
        elif op == "set_status":
            tracker.set_status(issue_id, entry.get("status", ""))
        elif op in {"label_add", "add_label"}:
            tracker.add_label(issue_id, entry.get("label", ""))
        elif op in {"label_rm", "remove_label"}:
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

        # Re-dump issue.json only for active sessions, throttled
        if status in _WORKING_STATUSES:
            sid = session_dir.name
            now = time.time()
            if now - _last_redump.get(sid, 0) < ISSUE_REDUMP_INTERVAL_S:
                continue
            try:
                redump_issue(tracker, issue_id, session_dir)
                _last_redump[sid] = now
            except Exception as e:
                log.warning(f"[{session_dir.name}] Re-dump failed: {e}")
