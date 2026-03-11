"""git-bug adapter."""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from core.protocols import IssueTracker, TrackerIssue, TrackerComment, SHORT_ID_LEN

log = logging.getLogger(__name__)

_LOCK_RETRIES = 3
_LOCK_RETRY_DELAY_S = 5
_CMD_TIMEOUT_S = 30


class GitBugTracker:
    def __init__(self, repo_dir: str | Path = "/workspace"):
        self.cwd = str(repo_dir)

    def _run(self, *args: str, timeout: int = _CMD_TIMEOUT_S, ignore_rc: set[int] | None = None) -> str:
        for attempt in range(_LOCK_RETRIES):
            try:
                r = subprocess.run(
                    ["git-bug", *args], cwd=self.cwd,
                    capture_output=True, text=True, timeout=timeout,
                )
                if r.returncode == 0:
                    return r.stdout.strip()
                if r.returncode in (ignore_rc or set()):
                    return r.stdout.strip()
                if "already locked by the process pid" in r.stderr:
                    # Check if the locking process is still alive
                    pid = self._extract_lock_pid(r.stderr)
                    if pid and not self._pid_alive(pid):
                        log.warning(f"git-bug locked by dead process {pid} — clearing lock")
                        self._clear_stale_lock()
                        continue  # retry immediately after clearing
                    if attempt < _LOCK_RETRIES - 1:
                        log.info(f"git-bug locked (pid {pid}), retrying in {_LOCK_RETRY_DELAY_S}s...")
                        time.sleep(_LOCK_RETRY_DELAY_S)
                        continue
                log.warning(f"git-bug {' '.join(args)} failed (rc={r.returncode}): {r.stderr.strip()}")
                return r.stdout.strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                log.warning(f"git-bug {args[0]} failed: {e}")
                return ""
        return ""

    @staticmethod
    def _extract_lock_pid(stderr: str) -> int | None:
        """Extract PID from 'already locked by the process pid NNNN'."""
        m = re.search(r"process pid (\d+)", stderr)
        return int(m.group(1)) if m else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we can't signal it

    def _clear_stale_lock(self):
        """Remove stale git-bug lock files."""
        repo_git = Path(self.cwd) / ".git"
        # git-bug uses Go's lockfile package — look for lock files
        for pattern in ["git-bug-cache.lock", "*.lock"]:
            for lock in repo_git.glob(f"**/{pattern}"):
                if "git-bug" in str(lock) or "bug" in str(lock):
                    log.info(f"Removing stale lock: {lock}")
                    lock.unlink(missing_ok=True)

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        raw = self._run("bug", "show", issue_id, "-f", "json")
        if not raw: return None
        try:
            d = json.loads(raw)
            comments = d.get("comments", [])
            return TrackerIssue(
                id=issue_id, identifier=issue_id[:SHORT_ID_LEN],
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
        self._run("bug", "label", "new", issue_id, label, ignore_rc={1})

    def remove_label(self, issue_id: str, label: str) -> None:
        self._run("bug", "label", "rm", issue_id, label, ignore_rc={1})

    def sync(self) -> None:
        self._run("pull")
        self._run("push")
