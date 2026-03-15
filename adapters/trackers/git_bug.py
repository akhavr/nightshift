"""git-bug adapter."""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from core.protocols import IssueTracker, TrackerIssue, TrackerComment, SHORT_ID_LEN

log = logging.getLogger(__name__)

_LOCK_RETRIES = 3
_LOCK_RETRY_DELAY_S = 5
_CMD_TIMEOUT_S = 30
_POLL_INTERVAL_S = 0.1
_GRACEFUL_KILL_TIMEOUT_S = 5


def _graceful_kill(proc: subprocess.Popen, timeout: int = _GRACEFUL_KILL_TIMEOUT_S) -> None:
    """Terminate a subprocess, escalating to kill if it doesn't exit in time."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class GitBugTracker:
    def __init__(self, repo_dir: str | Path = "/workspace",
                 shutdown_event: threading.Event | None = None):
        self.cwd = str(repo_dir)
        self._shutdown = shutdown_event or threading.Event()
        self._current_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._has_remote: bool | None = None  # lazy-detected on first sync

    @staticmethod
    def _short(issue_id: str) -> str:
        """Truncate issue ID to SHORT_ID_LEN for git-bug CLI compatibility."""
        return issue_id[:SHORT_ID_LEN]

    def _run(self, *args: str, timeout: int = _CMD_TIMEOUT_S, ignore_rc: set[int] | None = None) -> str:
        for attempt in range(_LOCK_RETRIES):
            if self._shutdown.is_set():
                return ""
            try:
                stdout, stderr, returncode = self._run_interruptible(
                    ["git-bug", *args], timeout=timeout,
                )
                if returncode is None:
                    return ""  # shutdown interrupted
                if returncode == 0:
                    return stdout.strip()
                if returncode in (ignore_rc or set()):
                    return stdout.strip()
                if "already locked by the process pid" in stderr:
                    pid = self._extract_lock_pid(stderr)
                    if pid and not self._pid_alive(pid):
                        log.warning(f"git-bug locked by dead process {pid} — clearing lock")
                        self._clear_stale_lock()
                        continue  # retry immediately after clearing
                    if attempt < _LOCK_RETRIES - 1:
                        log.info(f"git-bug locked (pid {pid}), retrying in {_LOCK_RETRY_DELAY_S}s...")
                        if self._shutdown.wait(timeout=_LOCK_RETRY_DELAY_S):
                            return ""  # shutdown during retry sleep
                        continue
                log.warning(f"git-bug {' '.join(args)} failed (rc={returncode}): {stderr.strip()}")
                return stdout.strip()
            except subprocess.TimeoutExpired:
                log.warning(f"git-bug {args[0]} timed out after {timeout}s")
                return ""
            except OSError as e:
                log.warning(f"git-bug {args[0]} failed: {e}")
                return ""
        return ""

    def _run_interruptible(self, cmd: list[str], timeout: int
                           ) -> tuple[str, str, int | None]:
        """Run a subprocess with poll loop so signals can interrupt it.

        Returns (stdout, stderr, returncode). returncode is None if
        shutdown interrupted before the process finished.
        """
        with self._proc_lock:
            if self._shutdown.is_set():
                return ("", "", None)
            proc = subprocess.Popen(
                cmd, cwd=self.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self._current_proc = proc

        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if self._shutdown.is_set():
                    _graceful_kill(proc)
                    return ("", "", None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                # Short sleep so we check shutdown frequently
                time.sleep(min(_POLL_INTERVAL_S, remaining))

            stdout = proc.stdout.read() if proc.stdout else ""
            stderr = proc.stderr.read() if proc.stderr else ""
            return (stdout, stderr, proc.returncode)
        finally:
            with self._proc_lock:
                self._current_proc = None

    def terminate_current(self):
        """Terminate any in-flight child process. Called during shutdown."""
        with self._proc_lock:
            proc = self._current_proc
        if proc and proc.poll() is None:
            log.info("Terminating in-flight git-bug process")
            _graceful_kill(proc)

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
        raw = self._run("bug", "show", self._short(issue_id), "-f", "json")
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
        raw = self._run("bug", "show", self._short(issue_id), "-f", "json")
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
        self._run("bug", "comment", "new", self._short(issue_id), "-m", body)

    def set_status(self, issue_id: str, status: str) -> None:
        cmd = "close" if status == "closed" else "open"
        self._run("bug", "status", cmd, self._short(issue_id))

    def add_label(self, issue_id: str, label: str) -> None:
        self._run("bug", "label", "new", self._short(issue_id), label, ignore_rc={1})

    def remove_label(self, issue_id: str, label: str) -> None:
        self._run("bug", "label", "rm", self._short(issue_id), label, ignore_rc={1})

    _NO_REMOTE_MARKERS = ("remote not found", "unable to resolve URL for remote")

    def sync(self) -> None:
        if self._has_remote is False:
            return
        if self._has_remote is None:
            # Probe once — run pull and check stderr for missing remote
            stdout, stderr, rc = self._run_interruptible(
                ["git-bug", "pull"], timeout=_CMD_TIMEOUT_S)
            if rc and any(m in (stderr or "") for m in self._NO_REMOTE_MARKERS):
                log.info("git-bug has no remote configured — skipping sync")
                self._has_remote = False
                return
            self._has_remote = True
            # pull already ran; just do push
            self._run("push")
        else:
            self._run("pull")
            self._run("push")
