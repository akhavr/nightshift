"""Static tracker — reads pre-dumped issue data from JSON files.

Used inside containers where the real tracker (git-bug, GitHub, etc.)
is not available. The host dumps issue.json and issues.json to the
session directory before launching the container.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from core.protocols import IssueTracker, TrackerIssue, TrackerComment, SHORT_ID_LEN

log = logging.getLogger(__name__)


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


class StaticTracker:
    """Read-only tracker backed by JSON files in the session directory.

    Expected files:
      - /session/issue.json   — the current issue
      - /session/issues.json  — all open issues (for related-issue search)
    """

    def __init__(self, session_dir: str | Path = "/session"):
        self.session_dir = Path(session_dir)
        self._issue: Optional[TrackerIssue] = None
        self._issues: list[TrackerIssue] = []
        self._load()

    def _load(self):
        issue_path = self.session_dir / "issue.json"
        if issue_path.exists():
            self._issue = _issue_from_dict(json.loads(issue_path.read_text()))

        issues_path = self.session_dir / "issues.json"
        if issues_path.exists():
            self._issues = [_issue_from_dict(d) for d in json.loads(issues_path.read_text())]

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
        return []  # Comments not available in static mode

    def add_comment(self, issue_id: str, body: str) -> None:
        log.info(f"[static] Comment on {issue_id[:SHORT_ID_LEN]}: {body[:80]}")

    def set_status(self, issue_id: str, status: str) -> None:
        log.info(f"[static] Status {issue_id[:SHORT_ID_LEN]} -> {status}")

    def add_label(self, issue_id: str, label: str) -> None:
        log.info(f"[static] Label +{label} on {issue_id[:SHORT_ID_LEN]}")

    def remove_label(self, issue_id: str, label: str) -> None:
        log.info(f"[static] Label -{label} on {issue_id[:SHORT_ID_LEN]}")

    def sync(self) -> None:
        pass  # Nothing to sync
