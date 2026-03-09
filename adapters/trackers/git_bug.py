"""git-bug adapter."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from core.protocols import IssueTracker, TrackerIssue, TrackerComment

log = logging.getLogger(__name__)


class GitBugTracker:
    def __init__(self, repo_dir: str | Path = "/workspace"):
        self.cwd = str(repo_dir)

    def _run(self, *args: str, timeout: int = 30) -> str:
        try:
            r = subprocess.run(
                ["git-bug", *args], cwd=self.cwd,
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode != 0:
                log.warning(f"git-bug {' '.join(args)} failed (rc={r.returncode}): {r.stderr.strip()}")
            return r.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning(f"git-bug {args[0]} failed: {e}")
            return ""

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        raw = self._run("bug", "show", issue_id, "-f", "json")
        if not raw: return None
        try:
            d = json.loads(raw)
            comments = d.get("comments", [])
            return TrackerIssue(
                id=issue_id, identifier=issue_id[:12],
                title=d.get("title", "Unknown"),
                body=comments[0].get("message", "") if comments else "",
                status=d.get("status", "unknown"),
                labels=[l.lower() for l in (d.get("labels") or [])],
                created_at=d.get("created_at"),
            )
        except json.JSONDecodeError:
            return None

    def list_issues(self, status=None) -> list[TrackerIssue]:
        args = ["bug", "-f", "json"]
        if isinstance(status, str):
            args.extend(["--status", status])
        raw = self._run(*args)
        if not raw: return []
        try:
            return [i for item in json.loads(raw)
                    if (i := self.get_issue(item.get("id", ""))) is not None]
        except json.JSONDecodeError:
            return []

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        raw = self._run("bug", "show", issue_id, "-f", "json")
        if not raw: return []
        try:
            return [
                TrackerComment(
                    author=c.get("author", {}).get("name", "unknown"),
                    body=c.get("message", ""), created_at=c.get("timestamp"),
                )
                for c in json.loads(raw).get("comments", [])
            ]
        except json.JSONDecodeError:
            return []

    def add_comment(self, issue_id: str, body: str) -> None:
        self._run("bug", "comment", "new", issue_id, "-m", body)

    def set_status(self, issue_id: str, status: str) -> None:
        cmd = "close" if status == "closed" else "open"
        self._run("bug", "status", cmd, issue_id)

    def add_label(self, issue_id: str, label: str) -> None:
        self._run("bug", "label", "new", issue_id, label)

    def remove_label(self, issue_id: str, label: str) -> None:
        self._run("bug", "label", "rm", issue_id, label)

    def sync(self) -> None:
        self._run("pull")
        self._run("push")
