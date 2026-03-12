"""Orphan detection, closed issue cleanup, auto-start."""

import json
import logging
import sys
import time
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, ORPHAN_GRACE_PERIOD_S, SHORT_ID_LEN, LOG_PREVIEW_LEN,
)
from host.session_utils import read_state
from core.config import load_workflow
from host.watcher.telegram_relay import TelegramRelay

log = logging.getLogger("watcher")

_ACTIVE_STATUSES = ("working", "starting", "waiting:answer")

# Directory containing the host package (host/)
_HOST_DIR = Path(__file__).resolve().parent.parent


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class SessionMonitor:
    """Orphan detection, closed issue cleanup, auto-start."""

    def __init__(self, sessions_dir: Path, repo_dir: Path, auto_start: bool,
                 telegram: TelegramRelay, get_tracker, get_auto_start_config,
                 recently_launched: dict, launch_background):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._get_auto_start_config = get_auto_start_config
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._last_orphan_check = 0.0
        self._last_closed_check = 0.0
        self._last_auto_start_poll = 0.0
        self._known_issue_ids: set[str] = set()

    def check_orphaned_sessions(self):
        """Detect sessions with status 'working' but no running container -- auto-resume."""
        now = time.time()
        if now - self._last_orphan_check < REVIEW_POLL_INTERVAL_S:
            return
        self._last_orphan_check = now

        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue
            self.maybe_resume_orphan(session_dir, sid, now)

    def maybe_resume_orphan(self, session_dir: Path, sid: str, now: float):
        """Check a single session and auto-resume if orphaned."""
        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read state for orphan check: {e}")
            return

        if state.get("status") not in ("working", "starting"):
            return

        # Skip if recently launched (give it time to start)
        if sid in self._recently_launched:
            if now - self._recently_launched[sid] < ORPHAN_GRACE_PERIOD_S:
                return
            del self._recently_launched[sid]

        # Skip if container is still running
        container = f"nightshift-{sid}"
        if _pkg().docker_container_status(container) in ("running", "paused"):
            return

        issue_id = state.get("issue_id", "")
        if not issue_id:
            return

        log.info(f"[{sid}] Orphaned session (container gone, status: {state['status']}). Auto-resuming.")
        self._recently_launched[sid] = time.time()

        cmd = [
            sys.executable,
            str(_HOST_DIR / "launch.py"),
            issue_id, "--resume",
        ]
        self._launch_background(cmd, sid)

    def check_closed_issues(self):
        """Detect sessions whose issues have been closed -- clean up worktree + session."""
        now = time.time()
        if now - self._last_closed_check < REVIEW_POLL_INTERVAL_S:
            return
        self._last_closed_check = now

        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue

            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for closed-issue check: {e}")
                continue

            if state.get("status") in ("working", "starting"):
                continue

            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            try:
                tracker = self._get_tracker()
                issue = tracker.get_issue(issue_id)
            except Exception as e:
                log.warning(f"[{sid}] Failed to check issue status: {e}")
                continue

            if not issue or issue.status not in ("closed",):
                continue

            container = f"nightshift-{sid}"
            _pkg().docker_stop(container)

            log.info(f"[{sid}] Issue closed -- cleaning up worktree and session")
            self.cleanup_session(sid, issue_id, session_dir)

    def cleanup_session(self, sid: str, issue_id: str, session_dir: Path):
        """Remove worktree, branch, and session directory."""
        try:
            config = load_workflow(self.repo_dir / "WORKFLOW.md")
            wt = self.repo_dir / config.workspace.root / f"agent-{sid}"
            branch = f"agent/{sid}"

            _pkg().remove_worktree(self.repo_dir, wt, branch)

            _pkg().shutil.rmtree(session_dir)

            self._recently_launched.pop(sid, None)

            log.info(f"[{sid}] Cleaned up worktree, branch, and session")
        except Exception as e:
            log.error(f"[{sid}] Cleanup failed: {e}")

    def iter_session_states(self) -> list[tuple[Path, dict]]:
        """Read all session state.json files, returning (session_dir, state_dict) pairs."""
        results = []
        if not self.sessions_dir.exists():
            return results
        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            if not (session_dir / "state.json").exists():
                continue
            try:
                state = read_state(session_dir)
                results.append((session_dir, state))
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"Auto-start: failed to read state for {session_dir.name}: {e}")
        return results

    def count_active_sessions(self, states=None) -> int:
        """Count sessions that are currently working or starting."""
        if states is None:
            states = self.iter_session_states()
        return sum(
            1 for _, state in states
            if state.get("status") in _ACTIVE_STATUSES
        )

    def check_new_issues(self):
        """Poll tracker for open issues matching the auto-start label and start sessions."""
        asc = self._get_auto_start_config()
        now = time.time()
        if now - self._last_auto_start_poll < asc.poll_interval_s:
            return
        self._last_auto_start_poll = now

        try:
            tracker = self._get_tracker()
            issues = tracker.list_issues(status="open")
        except Exception as e:
            log.warning(f"Auto-start: tracker poll failed: {e}")
            return

        # Filter by label
        label = asc.label
        if label:
            issues = [i for i in issues if label in i.labels]

        # Build set of existing issue IDs and count active sessions in one pass
        all_states = self.iter_session_states()
        existing_issue_ids: set[str] = {
            state.get("issue_id", "") for _, state in all_states
        }
        active_count = self.count_active_sessions(states=all_states)

        for issue in issues:
            if issue.id in existing_issue_ids or issue.id in self._known_issue_ids:
                continue

            if active_count >= asc.max_concurrent:
                log.info(f"Auto-start: at max concurrent ({asc.max_concurrent}), "
                         f"deferring {issue.identifier}")
                break

            self._known_issue_ids.add(issue.id)
            sid = issue.id[:SHORT_ID_LEN]
            self._recently_launched[sid] = time.time()
            active_count += 1
            log.info(f"Auto-start: launching {issue.identifier} -- {issue.title[:LOG_PREVIEW_LEN]}")
            self.telegram.notify(f"\U0001f680 Auto-starting `{issue.identifier}`: {issue.title[:LOG_PREVIEW_LEN]}")

            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue.id,
            ]
            self._launch_background(cmd, sid)
