"""Static tracker — reads pre-dumped issue data from JSON files.

Used inside containers where the real tracker (git-bug, GitHub, etc.)
is not available. The host dumps issue.json and issues.json to the
session directory before launching the container.

Supports bidirectional sync:
- Reads: reload() re-reads issue.json when mtime changes, returns new comments.
- Writes: mutations are appended to tracker-outbox.jsonl for host processing.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from core.constants import TRACKER_OUTBOX_FILENAME
from core.protocols import IssueTracker, TrackerIssue, TrackerComment, SHORT_ID_LEN

log = logging.getLogger(__name__)

LOG_BODY_PREVIEW_LEN = 80


def _issue_from_dict(d: dict) -> TrackerIssue:
    return TrackerIssue(
        id=d["id"],
        identifier=d.get("identifier", d["id"][:SHORT_ID_LEN]),
        title=d.get("title", ""),
        body=d.get("body", ""),
        status=d.get("status", "open"),
        labels=d.get("labels", []),
        url=d.get("url"),
        priority=d.get("priority"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _comments_from_dict(d: dict) -> list[TrackerComment]:
    """Extract comments from an issue dict if present."""
    raw = d.get("comments", [])
    return [
        TrackerComment(
            author=c.get("author", ""),
            body=c.get("body", ""),
            created_at=c.get("created_at"),
        )
        for c in raw
    ]


class StaticTracker:
    """Tracker backed by JSON files in the session directory.

    Reads: issue.json and issues.json (re-read on mtime change via reload()).
    Writes: mutations appended to tracker-outbox.jsonl for host processing.
    """

    def __init__(self, session_dir: str | Path = "/session"):
        self.session_dir = Path(session_dir)
        self._issue: Optional[TrackerIssue] = None
        self._issues: list[TrackerIssue] = []
        self._comments: list[TrackerComment] = []
        self._issue_mtime: float = 0.0
        self._outbox_path = self.session_dir / TRACKER_OUTBOX_FILENAME
        self._load()

    def _load(self):
        issue_path = self.session_dir / "issue.json"
        if issue_path.exists():
            data = json.loads(issue_path.read_text())
            self._issue = _issue_from_dict(data)
            self._comments = _comments_from_dict(data)
            try:
                self._issue_mtime = os.path.getmtime(issue_path)
            except OSError as e:
                log.warning(f"Could not stat issue.json: {e}")

        issues_path = self.session_dir / "issues.json"
        if issues_path.exists():
            self._issues = [_issue_from_dict(d) for d in json.loads(issues_path.read_text())]

    def reload(self) -> list[TrackerComment]:
        """Re-read issue.json if mtime changed. Returns list of NEW comments since last load."""
        issue_path = self.session_dir / "issue.json"
        if not issue_path.exists():
            return []

        try:
            current_mtime = os.path.getmtime(issue_path)
        except OSError as e:
            log.warning(f"Could not stat issue.json for reload: {e}")
            return []

        if current_mtime <= self._issue_mtime:
            return []

        old_comment_count = len(self._comments)
        try:
            data = json.loads(issue_path.read_text())
            self._issue = _issue_from_dict(data)
            self._comments = _comments_from_dict(data)
            self._issue_mtime = current_mtime
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to reload issue.json: {e}")
            return []

        new_comments = self._comments[old_comment_count:]
        if new_comments:
            log.info(f"Reloaded issue.json: {len(new_comments)} new comment(s)")
        return new_comments

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        if self._issue and (self._issue.id == issue_id or self._issue.identifier == issue_id
                           or self._issue.id.startswith(issue_id)):
            return self._issue
        # Search in all issues
        for i in self._issues:
            if i.id == issue_id or i.identifier == issue_id or i.id.startswith(issue_id):
                return i
        return None

    def list_issues(self, status=None) -> list[TrackerIssue]:
        if status is None:
            return self._issues
        if isinstance(status, str):
            status = [status]
        return [i for i in self._issues if i.status in status]

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        return list(self._comments)

    def add_comment(self, issue_id: str, body: str) -> None:
        log.info(f"[static] Comment on {issue_id[:SHORT_ID_LEN]}: {body[:LOG_BODY_PREVIEW_LEN]}")
        self._append_outbox({"op": "comment", "issue_id": issue_id, "text": body})

    def set_status(self, issue_id: str, status: str) -> None:
        log.info(f"[static] Status {issue_id[:SHORT_ID_LEN]} -> {status}")
        self._append_outbox({"op": "set_status", "issue_id": issue_id, "status": status})

    def add_label(self, issue_id: str, label: str) -> None:
        log.info(f"[static] Label +{label} on {issue_id[:SHORT_ID_LEN]}")
        self._append_outbox({"op": "label_add", "issue_id": issue_id, "label": label})

    def remove_label(self, issue_id: str, label: str) -> None:
        log.info(f"[static] Label -{label} on {issue_id[:SHORT_ID_LEN]}")
        self._append_outbox({"op": "label_rm", "issue_id": issue_id, "label": label})

    def run_raw(self, *args: str) -> str:
        raise NotImplementedError("StaticTracker does not support raw CLI passthrough")

    def sync(self) -> None:
        pass  # Nothing to sync

    def _append_outbox(self, entry: dict) -> None:
        """Append a JSON-lines entry to the outbox file for host processing."""
        try:
            with open(self._outbox_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            log.warning(f"Failed to write to outbox: {e}")
