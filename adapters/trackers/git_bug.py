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
from core.constants import LOCK_RETRY_ATTEMPTS, LOCK_RETRY_BASE_DELAY_S

log = logging.getLogger(__name__)

_CMD_TIMEOUT_S = 30
_POLL_INTERVAL_S = 0.1
_GRACEFUL_KILL_TIMEOUT_S = 5
_TREE_CMD_TIMEOUT_S = 10

_CLOCK_DIR = "git-bug/clocks"
_CLOCK_FILES = {"bugs-edit": "edit-clock", "bugs-create": "create-clock"}


def detect_corrupt_clocks(repo_dir: str | Path) -> list[str]:
    """Return list of empty (corrupt) lamport clock filenames under .git/git-bug/clocks/."""
    clock_dir = Path(repo_dir) / ".git" / _CLOCK_DIR
    corrupt = []
    for filename in _CLOCK_FILES:
        path = clock_dir / filename
        if path.exists() and path.stat().st_size == 0:
            corrupt.append(filename)
    return corrupt


def _scan_max_clocks(repo_dir: str | Path) -> dict[str, int]:
    """Scan refs/bugs/ tip trees for max edit-clock and create-clock values.

    Returns dict mapping clock prefix ("edit-clock", "create-clock") to max value found.
    """
    maxvals: dict[str, int] = {}
    try:
        refs_out = subprocess.run(
            ["git", "for-each-ref", "--format=%(objectname)", "refs/bugs/"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
        )
        if refs_out.returncode != 0:
            return maxvals
        for ref_hash in refs_out.stdout.strip().splitlines():
            tree_out = subprocess.run(
                ["git", "ls-tree", ref_hash],
                cwd=str(repo_dir), capture_output=True, text=True, timeout=_TREE_CMD_TIMEOUT_S,
            )
            if tree_out.returncode != 0:
                continue
            for line in tree_out.stdout.splitlines():
                for prefix in _CLOCK_FILES.values():
                    m = re.search(rf"{prefix}-(\d+)", line)
                    if m:
                        val = int(m.group(1))
                        if val > maxvals.get(prefix, 0):
                            maxvals[prefix] = val
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning(f"Failed to scan bug refs for clock repair: {e}")
    return maxvals


def repair_lamport_clocks(repo_dir: str | Path) -> list[str]:
    """Detect and repair empty git-bug lamport clock files.

    Returns list of filenames that were repaired.
    """
    corrupt = detect_corrupt_clocks(repo_dir)
    if not corrupt:
        return []

    log.warning(f"Corrupt git-bug clock files detected: {corrupt}")
    maxvals = _scan_max_clocks(repo_dir)

    clock_dir = Path(repo_dir) / ".git" / _CLOCK_DIR
    repaired = []
    for filename, prefix in _CLOCK_FILES.items():
        if filename not in corrupt:
            continue
        val = maxvals.get(prefix, 0)
        path = clock_dir / filename
        path.write_text(str(val))
        log.warning(f"Repaired {filename}: wrote value {val}")
        repaired.append(filename)
    return repaired


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
        """Run a git-bug command with lock retry.

        All git-bug operations — including those from cli.py (accept, reject,
        start, etc.) — route through this method via GitBugTracker's public API,
        so lock retry is applied uniformly.  There are no direct git-bug
        subprocess calls outside this adapter.
        """
        for attempt in range(LOCK_RETRY_ATTEMPTS):
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
                    if attempt < LOCK_RETRY_ATTEMPTS - 1:
                        delay = LOCK_RETRY_BASE_DELAY_S * (2 ** attempt)
                        log.info(f"git-bug locked (pid {pid}), retrying in {delay}s...")
                        if self._shutdown.wait(timeout=delay):
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
        # git-bug uses Go's lockfile package — look for lock files under git-bug dirs
        for lock in repo_git.glob("**/git-bug*/**/*.lock"):
            log.info(f"Removing stale lock: {lock}")
            lock.unlink(missing_ok=True)
        # Also check for lock files directly named git-bug-*.lock
        for lock in repo_git.glob("**/git-bug*.lock"):
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

    def run_raw(self, *args: str) -> str:
        """Pass arguments directly to git-bug CLI with lock retry."""
        return self._run(*args)

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
