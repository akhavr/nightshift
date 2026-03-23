"""Orphan detection, closed issue cleanup, auto-start."""

import json
import logging
import sys
import time
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, ORPHAN_GRACE_PERIOD_S, SHORT_ID_LEN,
    MAX_ORPHAN_RESUMES, AUTH_RETRY_INTERVAL_S, MAX_AUTH_RETRIES,
    REVIEW_SESSION_PREFIX, LAUNCH_GRACE_PERIOD_S,
)
from core.protocols import NotificationLevel
from core.constants import TITLE_TRUNCATE_LEN
from host.session_utils import read_state, write_state, update_status
from core.config import load_workflow
from host.watcher.lifecycle_comments import post_start, post_resume, read_checkpoint_count
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
                 recently_launched: dict, launch_background,
                 workflow_path: Path | None = None):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self.workflow_path = workflow_path or (repo_dir / "WORKFLOW.md")
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._get_auto_start_config = get_auto_start_config
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._last_orphan_check = 0.0
        self._last_closed_check = 0.0
        self._last_auto_start_poll = 0.0
        self._last_auth_retry_check = 0.0
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

        status = state.get("status")
        is_review_session = sid.startswith(REVIEW_SESSION_PREFIX)

        # Review sessions in waiting:review with no container are orphaned
        # (the review container crashed). Coder sessions in waiting:review
        # are expected to have no container — the coder is paused while
        # the review handles them.
        if status == "waiting:review" and is_review_session:
            pass  # fall through to container check
        elif status not in ("working", "starting"):
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

        orphan_resumes = state.get("orphan_resumes", 0)
        if orphan_resumes >= MAX_ORPHAN_RESUMES:
            if is_review_session:
                self._handle_review_orphan_limit(sid, session_dir, state, issue_id)
            else:
                self._handle_coder_orphan_limit(sid, session_dir, state, issue_id)
            return

        state["orphan_resumes"] = orphan_resumes + 1
        write_state(session_dir, state)

        log.info(f"[{sid}] Orphaned session (container gone, status: {state['status']}, "
                 f"orphan_resume {orphan_resumes + 1}/{MAX_ORPHAN_RESUMES}). Auto-resuming.")
        self._recently_launched[sid] = time.time()

        session_dir = self.sessions_dir / sid
        self._resume_session(sid, issue_id, reason="orphaned (container gone)")

    def _handle_coder_orphan_limit(self, sid: str, session_dir: Path,
                                    state: dict, issue_id: str):
        """Suspend a coder session as too-complex after hitting the orphan limit."""
        log.error(f"[{sid}] Hit max orphan resumes ({MAX_ORPHAN_RESUMES}). "
                  f"Task may be too complex — stopping.")
        state["status"] = "suspended:too-complex"
        write_state(session_dir, state)
        try:
            tracker = self._get_tracker()
            tracker.add_comment(issue_id,
                f"🛑 Auto-resume limit reached ({MAX_ORPHAN_RESUMES} orphan restarts). "
                f"The agent keeps crashing at the same point — the task is likely "
                f"too complex for a single issue. Please split it into smaller sub-tasks "
                f"and re-file.")
        except Exception as e:
            log.warning(f"[{sid}] Failed to post too-complex comment: {e}")
        self.telegram.notify(
            f"🛑 `{sid}` hit {MAX_ORPHAN_RESUMES} orphan restarts. "
            f"Task too complex — needs splitting. Session suspended.",
            level=NotificationLevel.ACTIONS)

    def _handle_review_orphan_limit(self, sid: str, session_dir: Path,
                                     state: dict, issue_id: str):
        """Handle a review session that hit the orphan limit: fail and fall back to human review."""
        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        log.error(f"[{sid}] Review session hit max orphan resumes ({MAX_ORPHAN_RESUMES}). "
                  f"Falling back to human review for coder session {coder_sid}.")
        state["status"] = "suspended:review-failed"
        write_state(session_dir, state)

        # Transition coder session to waiting:human-review
        coder_dir = self.sessions_dir / coder_sid
        if coder_dir.exists() and (coder_dir / "state.json").exists():
            try:
                update_status(coder_dir, "waiting:human-review")
            except Exception as e:
                log.warning(f"[{coder_sid}] Failed to transition coder to human-review: {e}")

        try:
            tracker = self._get_tracker()
            tracker.add_comment(issue_id,
                f"⚠️ Auto-review failed after {MAX_ORPHAN_RESUMES} attempts — "
                f"falling back to human review.")
        except Exception as e:
            log.warning(f"[{sid}] Failed to post review-failed comment: {e}")
        self.telegram.notify(
            f"⚠️ `{sid}` auto-review failed after {MAX_ORPHAN_RESUMES} attempts — "
            f"falling back to human review.\n"
            f"`nightshift accept/reject/revise {issue_id}`",
            level=NotificationLevel.ACTIONS)

    def _resume_session(self, sid: str, issue_id: str, reason: str):
        """Post lifecycle comment and launch a resume for the given session."""
        session_dir = self.sessions_dir / sid
        checkpoint_count = read_checkpoint_count(session_dir)
        post_resume(self._get_tracker, issue_id, sid,
                    reason=reason, checkpoint_count=checkpoint_count)

        is_review = sid.startswith(REVIEW_SESSION_PREFIX)
        cmd = [
            sys.executable,
            str(_HOST_DIR / "launch.py"),
            issue_id, "--resume",
        ]
        workflow = self.workflow_path
        if is_review:
            review_md = self.repo_dir / "REVIEW.md"
            cmd += ["--step", "review"]
            if review_md.exists():
                workflow = review_md
        cmd += ["--workflow", str(workflow)]
        self._launch_background(cmd, sid)

    def check_auth_failures(self):
        """Retry sessions suspended due to auth failure on a slow interval."""
        now = time.time()
        if now - self._last_auth_retry_check < AUTH_RETRY_INTERVAL_S:
            return
        self._last_auth_retry_check = now

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
                log.warning(f"[{sid}] Failed to read state for auth-retry check: {e}")
                continue

            if state.get("status") != "suspended:auth-failure":
                continue

            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            auth_retries = state.get("auth_retries", 0)
            if auth_retries >= MAX_AUTH_RETRIES:
                log.warning(f"[{sid}] Auth retry limit reached ({MAX_AUTH_RETRIES}). "
                            f"Token still invalid — giving up.")
                state["status"] = "suspended:auth-failure-permanent"
                write_state(session_dir, state)
                self.telegram.notify(
                    f"🔑 `{sid}` hit {MAX_AUTH_RETRIES} auth retries — token still invalid. "
                    f"Giving up. Fix credentials and `nightshift resume` manually.",
                    level=NotificationLevel.ACTIONS)
                continue

            log.info(f"[{sid}] Auth-failure session — retrying (token may have been refreshed, "
                     f"attempt {auth_retries + 1}/{MAX_AUTH_RETRIES})")
            state["auth_retries"] = auth_retries + 1
            state["status"] = "working"
            write_state(session_dir, state)
            self._recently_launched[sid] = time.time()

            self._resume_session(sid, issue_id, reason="auth-failure retry")

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

            # Stop the container before cleanup to avoid pulling
            # the session dir out from under a running container.
            container = f"nightshift-{sid}"
            _pkg().docker_stop(container)

            # Verify container is actually gone; if still running,
            # defer cleanup to the next poll cycle.
            status = _pkg().docker_container_status(container)
            if status in ("running", "paused"):
                log.warning(f"[{sid}] Container still {status} after stop -- deferring cleanup")
                continue

            log.info(f"[{sid}] Issue closed -- cleaning up worktree and session")
            self.cleanup_session(sid, issue_id, session_dir)

    def cleanup_session(self, sid: str, issue_id: str, session_dir: Path):
        """Remove worktree, branch, and session directory."""
        try:
            config = load_workflow(self.workflow_path)
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
        """Count sessions that are currently working or starting.

        Also counts recently launched sessions (within LAUNCH_GRACE_PERIOD_S)
        that don't yet have a state.json, closing the TOCTOU race between
        subprocess spawn and state file creation.
        """
        if states is None:
            states = self.iter_session_states()
        count = sum(
            1 for _, state in states
            if state.get("status") in _ACTIVE_STATUSES
        )
        # Add recently launched sessions that have no state.json yet
        now = time.time()
        known_sids = {sd.name for sd, _ in states}
        for sid, launch_time in self._recently_launched.items():
            if sid in known_sids:
                continue
            if now - launch_time < LAUNCH_GRACE_PERIOD_S:
                count += 1
        return count

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
            log.info(f"Auto-start: launching {issue.identifier} -- {issue.title[:TITLE_TRUNCATE_LEN]}")
            self.telegram.notify(f"\U0001f680 Auto-starting `{issue.identifier}`: {issue.title[:TITLE_TRUNCATE_LEN]}",
                                level=NotificationLevel.ALL)

            post_start(self._get_tracker, issue.id, sid, title=issue.title)

            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue.id,
                "--workflow", str(self.workflow_path),
            ]
            self._launch_background(cmd, sid)
