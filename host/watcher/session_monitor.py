"""Orphan detection, closed issue cleanup, auto-start."""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, ORPHAN_GRACE_PERIOD_S, SHORT_ID_LEN,
    MAX_ORPHAN_RESUMES, AUTH_RETRY_INTERVAL_S, MAX_AUTH_RETRIES,
    PROVIDER_OUTAGE_RETRY_INTERVAL_S, MAX_PROVIDER_OUTAGE_RETRIES,
    REVIEW_SESSION_PREFIX, LAUNCH_GRACE_PERIOD_S,
    ZOMBIE_CHECK_INTERVAL_S, ZOMBIE_TIMEOUT_MULTIPLIER, DEFAULT_STALL_TIMEOUT_S,
    SESSION_SIZE_CHECK_INTERVAL_S, SIZE_WARNING_THRESHOLD_MB,
    SIZE_CRITICAL_THRESHOLD_MB, BLOCKED_LABEL_PREFIX, REVISE_PENDING_FILENAME,
    RUNAWAY_RESUME_WARNING_THRESHOLD,
)
from core.protocols import NotificationLevel
from core.constants import TITLE_TRUNCATE_LEN
from core.state import StateManager
from host.session_utils import (
    read_state, update_status, _issue_id_prefix_match,
    archive_session,
    increment_orphan_resumes, update_state_fields,
)
from core.config import load_workflow
from host.watcher.lifecycle_comments import post_start, post_resume, read_checkpoint_count
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.issue_sync import process_outbox

log = logging.getLogger("watcher")

_ACTIVE_STATUSES = ("working", "starting", "running", "waiting:answer")
_ACTIVE_ORPHAN_STATUSES = ("working", "starting", "running")

# Directory containing the host package (host/)
_HOST_DIR = Path(__file__).resolve().parent.parent
REVIEW_CLEANUP_GRACE_PERIOD_S = 60


def is_blocked(labels: list[str]) -> bool:
    """Check if issue has any blocked:<id> labels."""
    return any(l.startswith(BLOCKED_LABEL_PREFIX) for l in labels)


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


def _parse_completed_at(timestamp: str) -> float | None:
    """Parse a completed_at timestamp into a unix epoch seconds value."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def cleanup_completed_review_session(review_dir: Path, coder_dir: Path | None = None,
                                     *, repo_dir: Path | None = None,
                                     workflow_path: Path | None = None,
                                     grace_period_s: int = REVIEW_CLEANUP_GRACE_PERIOD_S) -> bool:
    """Archive and remove a completed review session once the coder has moved on.

    Returns True when the review session was archived and cleaned up.
    """
    if not review_dir.exists() or not (review_dir / "state.json").exists():
        return False

    review_sid = review_dir.name
    if not review_sid.startswith(REVIEW_SESSION_PREFIX):
        return False

    try:
        review_state = read_state(review_dir)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[{review_sid}] Failed to read review state for cleanup: {e}")
        return False

    completed_at = _parse_completed_at(review_state.get("completed_at", ""))
    if completed_at is None:
        return False
    if time.time() - completed_at < grace_period_s:
        return False

    coder_sid = review_sid[len(REVIEW_SESSION_PREFIX):]
    if coder_dir is None:
        coder_dir = review_dir.parent / coder_sid
    if not coder_dir.exists() or not (coder_dir / "state.json").exists():
        return False

    try:
        coder_state = read_state(coder_dir)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[{coder_sid}] Failed to read coder state for review cleanup: {e}")
        return False

    # Block cleanup when coder is waiting:review OR reviewing.
    # "reviewing" means the review is in progress but verdict not yet processed.
    # If we archive the review while coder is still "reviewing", the coder gets
    # stuck forever because there's no review session to extract the verdict from.
    if coder_state.get("status") in ("waiting:review", "reviewing"):
        return False

    if repo_dir is None:
        try:
            repo_dir = review_dir.parents[2]
        except IndexError:
            return False

    if workflow_path is None:
        workflow_path = repo_dir / "WORKFLOW.md"

    review_md = repo_dir / "REVIEW.md"
    try:
        config = load_workflow(review_md) if review_md.exists() else load_workflow(workflow_path)
        worktree = repo_dir / config.workspace.root / f"{REVIEW_SESSION_PREFIX}{coder_sid}"
        archive_session(review_dir, repo_dir)
        _pkg().remove_worktree(repo_dir, worktree, f"review/{coder_sid}")
        _pkg().shutil.rmtree(review_dir, ignore_errors=True)
        log.info(f"[{review_sid}] Archived and cleaned up completed review session")
        return True
    except Exception as e:
        log.error(f"[{review_sid}] Failed to clean up completed review session: {e}")
        return False


class SessionMonitor:
    """Orphan detection, closed issue cleanup, auto-start."""

    def __init__(self, sessions_dir: Path, repo_dir: Path, auto_start: bool,
                 telegram: TelegramRelay, get_tracker, get_auto_start_config,
                 recently_launched: dict, launch_background,
                 workflow_path: Path | None = None,
                 review_orchestrator=None):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self.workflow_path = workflow_path or (repo_dir / "WORKFLOW.md")
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._get_auto_start_config = get_auto_start_config
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._review_orchestrator = review_orchestrator
        self._last_orphan_check = 0.0
        self._last_closed_check = 0.0
        self._last_auto_start_poll = 0.0
        self._last_auth_retry_check = 0.0
        self._last_provider_outage_check = 0.0
        self._last_zombie_check = 0.0
        self._last_session_size_check = 0.0
        self._alerted_zombies: set[str] = set()  # Avoid duplicate alerts
        self._alerted_large_sessions: set[str] = set()  # Avoid duplicate alerts
        self._alerted_runaway_sessions: set[str] = set()  # Avoid duplicate alerts

    def cleanup_stale_review_sessions(self):
        """Clean up review sessions with completed_at set but not yet cleaned up.

        This handles the race condition where the watcher restarts after a review
        container exits (setting completed_at) but before cleanup_review_session()
        is called. Without this cleanup, the watcher loops trying to launch a new
        review and fails with 'session already exists'.

        IMPORTANT: Before cleanup, this method processes the verdict from the
        review session to ensure the coder session state is updated. Otherwise,
        the coder remains in waiting:review status, triggering an infinite
        relaunch cycle.

        Called on watcher startup.
        """
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            # Only clean up review sessions
            if not sid.startswith(REVIEW_SESSION_PREFIX):
                continue
            if not (session_dir / "state.json").exists():
                continue

            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for stale review cleanup: {e}")
                continue

            # Only clean up if completed_at is set (review finished normally)
            if not state.get("completed_at"):
                continue

            log.info(f"[{sid}] Cleaning up stale review session (completed_at set)")

            # Process verdict BEFORE cleanup to prevent infinite relaunch loop
            coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
            coder_dir = self.sessions_dir / coder_sid
            if coder_dir.exists() and self._review_orchestrator:
                # _try_recover_review_verdict handles verdict extraction, processing, and cleanup
                if self._try_recover_review_verdict(coder_sid, coder_dir, sid):
                    continue  # Verdict processed and session cleaned up

            # No verdict found - do NOT cleanup. Leave the review session intact
            # so check_reviewer_done() can retry verdict extraction later, or so
            # human intervention can occur. Cleaning up without a verdict leaves
            # the coder stuck in "reviewing" forever.
            log.warning(f"[{sid}] Not archiving: no verdict recovered from stale review")

    def cleanup_stale_blocked_labels(self):
        """Remove blocked:<id> labels where the blocking issue is already closed.

        Called on watcher startup to handle stale labels from watcher downtime.
        """
        try:
            tracker = self._get_tracker()
            all_issues = tracker.list_issues()  # Both open and closed
        except Exception as e:
            log.warning(f"Stale blocked cleanup: tracker poll failed: {e}")
            return

        # Build set of closed issue ID prefixes
        closed_prefixes: set[str] = {
            i.id[:SHORT_ID_LEN] for i in all_issues if i.status == "closed"
        }

        # Scan open issues for stale blocked labels
        for issue in all_issues:
            if issue.status != "open":
                continue

            for label in issue.labels:
                if not label.startswith(BLOCKED_LABEL_PREFIX):
                    continue

                blocker_id = label[len(BLOCKED_LABEL_PREFIX):]
                if blocker_id in closed_prefixes:
                    try:
                        tracker.remove_label(issue.id, label)
                        log.info(f"Unblocked {issue.identifier}: removed stale {label}")
                    except Exception as e:
                        log.warning(f"Failed to remove stale label {label} from "
                                    f"{issue.identifier}: {e}")

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

    def check_runaway_sessions(self):
        """Warn when active sessions are approaching the orphan-resume limit."""
        if not self.sessions_dir.exists():
            return

        for session_dir, state in self._iter_runaway_session_states():
            sid = session_dir.name
            if state.get("status") not in _ACTIVE_ORPHAN_STATUSES:
                self._alerted_runaway_sessions.discard(sid)
                continue
            self._check_session_for_runaway(sid, state)

    def _iter_runaway_session_states(self) -> list[tuple[Path, dict]]:
        """Read raw state.json files for runaway-resume detection."""
        results: list[tuple[Path, dict]] = []
        if not self.sessions_dir.exists():
            return results

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            state_path = session_dir / "state.json"
            if not state_path.exists():
                continue

            try:
                state = read_state(session_dir, max_orphan_resumes=None)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{session_dir.name}] Failed to read state for runaway check: {e}")
                continue

            results.append((session_dir, state))

        return results

    def _check_session_for_runaway(self, sid: str, state: dict):
        """Check a single session for runaway orphan-resume behavior."""
        orphan_resumes = state.get("orphan_resumes", 0)
        if orphan_resumes < RUNAWAY_RESUME_WARNING_THRESHOLD:
            self._alerted_runaway_sessions.discard(sid)
            return

        if sid in self._alerted_runaway_sessions:
            return
        self._alerted_runaway_sessions.add(sid)

        log.warning(
            f"[{sid}] Runaway resume pattern detected: orphan_resumes={orphan_resumes} "
            f"(threshold: {RUNAWAY_RESUME_WARNING_THRESHOLD})"
        )
        self.telegram.notify(
            f"⚠️ `{sid}` has resumed {orphan_resumes} times without a live container. "
            f"This may be a runaway loop; check signal method and resume behavior.",
            level=NotificationLevel.ACTIONS,
        )

    def maybe_resume_orphan(self, session_dir: Path, sid: str, now: float):
        """Check a single session and auto-resume if orphaned."""
        try:
            state_mgr = StateManager(session_dir)
            session_state = state_mgr.load_state()
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read state for orphan check: {e}")
            return

        status = state_mgr.status
        is_review_session = sid.startswith(REVIEW_SESSION_PREFIX)

        # Any session with completed_at already finished normally. The
        # container may have exited before the watcher observed the final
        # status transition, so it must not be treated as an orphan.
        if session_state.completed_at:
            return

        # Review sessions in waiting:review with no container are orphaned
        # (the review container crashed).
        # Coder sessions in waiting:review are expected to have no
        # container — the coder is paused while the review handles them.
        #
        # Coder sessions stuck in "reviewing" with no review container are
        # also recovered: the review launch likely failed, so revert to
        # waiting:review for retry.
        review_waiting_for_container = status == "waiting:review" and is_review_session

        if status == "reviewing" and not is_review_session:
            # Coder session stuck in "reviewing" — check if the review
            # container is actually running.  If not, try to recover the
            # verdict from the completed review session before reverting.
            review_sid = f"{REVIEW_SESSION_PREFIX}{sid}"
            if review_sid in self._recently_launched:
                if now - self._recently_launched[review_sid] < ORPHAN_GRACE_PERIOD_S:
                    return  # review container still starting
            review_container = f"nightshift-{review_sid}"
            if _pkg().docker_container_status(review_container) in ("running", "paused"):
                return  # review container is alive

            # Check if the review session completed with a verdict that
            # was never processed (e.g. watcher restarted before processing).
            if self._try_recover_review_verdict(sid, session_dir, review_sid):
                return  # verdict recovered and applied

            # Check for revise-pending marker (failed revise launch after review)
            issue_id = session_state.issue_id
            if issue_id and self._retry_revise_if_pending(session_dir, sid, issue_id):
                return

            log.warning(f"[{sid}] Stuck in 'reviewing' with no review container — "
                        f"reverting to 'waiting:review'")
            update_status(session_dir, "waiting:review")
            return
        elif status not in _ACTIVE_ORPHAN_STATUSES and not review_waiting_for_container:
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

        issue_id = session_state.issue_id
        if not issue_id:
            return

        # Check for revise-pending marker (failed revise launch) — retry before
        # checking done signals, otherwise the session gets stuck in done:pending-review
        if self._retry_revise_if_pending(session_dir, sid, issue_id):
            return

        # Check if session actually completed (@@DONE@@ in conversation or
        # signal/done file exists). If so, transition to done:pending-review
        # instead of resuming — the container crashed after completing but
        # before persisting completed_at.
        if self._check_done_signals(session_dir, sid):
            return

        # Verify the agent branch still exists before attempting resume
        if not self._verify_branch_exists(session_state):
            log.warning(f"[{sid}] Branch missing — suspending session")
            update_state_fields(session_dir, status="suspended:branch-missing")
            self.telegram.notify(
                f"🔀 `{sid}` branch missing — session suspended. "
                f"Recreate the branch and `nightshift resume`.",
                level=NotificationLevel.ACTIONS)
            return

        orphan_resumes = session_state.orphan_resumes
        if orphan_resumes >= MAX_ORPHAN_RESUMES:
            if is_review_session:
                self._handle_review_orphan_limit(sid, session_dir, issue_id)
            else:
                self._handle_coder_orphan_limit(sid, session_dir, issue_id)
            return

        new_count = increment_orphan_resumes(session_dir)

        log.info(f"[{sid}] Orphaned session (container gone, status: {status}, "
                 f"orphan_resume {new_count}/{MAX_ORPHAN_RESUMES}). Auto-resuming.")
        self._recently_launched[sid] = time.time()

        session_dir = self.sessions_dir / sid
        self._resume_session(sid, issue_id, reason="orphaned (container gone)")

    def _check_done_signals(self, session_dir: Path, sid: str) -> bool:
        """Check if session completed via @@DONE@@ marker or signal file.

        When the container exits after outputting @@DONE@@ but before persisting
        completed_at to state.json, the orphan detector would incorrectly resume
        the session. This method checks for completion signals and transitions
        to done:pending-review instead.

        Returns True if a done signal was found and the session was transitioned.
        """
        # Check conversation.jsonl for @@DONE@@ marker
        conv_file = session_dir / "conversation.jsonl"
        if conv_file.exists():
            try:
                content = conv_file.read_text()
                if "@@DONE@@" in content:
                    log.info(f"[{sid}] Orphan has @@DONE@@ in conversation — "
                             f"transitioning to done:pending-review")
                    update_status(session_dir, "done:pending-review")
                    return True
            except OSError as e:
                log.warning(f"[{sid}] Failed to read conversation.jsonl: {e}")

        # Check for signal/done file (alternative signal mechanism)
        signal_done = session_dir / "signal" / "done"
        if signal_done.exists():
            log.info(f"[{sid}] Orphan has signal/done file — "
                     f"transitioning to done:pending-review")
            update_status(session_dir, "done:pending-review")
            return True

        return False

    def _retry_revise_if_pending(self, session_dir: Path, sid: str, issue_id: str) -> bool:
        """Check for revise-pending marker and retry the revise launch.

        When a revise launch fails (e.g., due to cp -a failure under concurrent
        git operations), a marker file is written so we can retry later. This
        prevents the session from getting stuck in done:pending-review.

        Returns True if a retry was attempted (regardless of success), False
        if no marker exists.
        """
        marker = session_dir / REVISE_PENDING_FILENAME
        if not marker.exists():
            return False

        try:
            marker_data = json.loads(marker.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read revise-pending marker: {e}")
            marker.unlink(missing_ok=True)
            return False

        review_dir = Path(marker_data.get("review_dir", ""))
        log.info(f"[{sid}] Found revise-pending marker (from review {review_dir.name}) — retrying launch")

        cmd = [
            sys.executable,
            str(_HOST_DIR / "launch.py"),
            issue_id, "--resume",
        ]
        if self._launch_background(cmd, sid):
            log.info(f"[{sid}] Revise retry launch succeeded — removing marker")
            marker.unlink(missing_ok=True)
            self._recently_launched[sid] = time.time()
            update_status(session_dir, "working")
        else:
            log.warning(f"[{sid}] Revise retry launch failed — keeping marker for next attempt")

        return True

    def _try_recover_review_verdict(self, coder_sid: str, coder_dir: Path,
                                    review_sid: str) -> bool:
        """Try to recover a verdict from a completed review session.

        When the watcher restarts after a review container has exited but
        before the verdict was processed, the review outbox and conversation
        log may still contain the verdict.  Process the outbox first (so
        tracker comments are applied), then extract and apply the verdict.

        Returns True if a verdict was found and applied, False otherwise.
        """
        review_dir = self.sessions_dir / review_sid
        if not review_dir.exists() or not (review_dir / "state.json").exists():
            return False

        try:
            review_state = read_state(review_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{review_sid}] Failed to read review state for verdict recovery: {e}")
            return False

        # Only recover from reviews that actually completed
        if not review_state.get("completed_at"):
            return False

        if self._review_orchestrator is None:
            return False

        # Process any pending outbox entries first so verdicts posted
        # via the tracker are visible for extraction.
        try:
            tracker = self._get_tracker()
            process_outbox(review_dir, tracker)
        except Exception as e:
            log.warning(f"[{review_sid}] Failed to process review outbox during recovery: {e}")

        # Extract verdict from conversation log or tracker
        issue_id = review_state.get("issue_id", "")
        conv_log = review_dir / "conversation.jsonl"
        verdict = self._review_orchestrator.extract_reviewer_verdict(conv_log, issue_id)
        if not verdict:
            log.info(f"[{coder_sid}] Review {review_sid} completed but no verdict found")
            return False

        log.info(f"[{coder_sid}] Recovered review verdict '{verdict}' from {review_sid}")
        if verdict == "approve":
            self._review_orchestrator.handle_reviewer_approve(coder_sid, coder_dir, issue_id)
        elif verdict == "revise":
            self._review_orchestrator.handle_reviewer_revise(
                coder_sid, coder_dir, issue_id, review_dir)

        # Clean up the review session
        self._review_orchestrator.cleanup_review_session(review_sid, review_dir)
        return True

    def _handle_coder_orphan_limit(self, sid: str, session_dir: Path,
                                    issue_id: str):
        """Suspend a coder session as too-complex after hitting the orphan limit."""
        log.error(f"[{sid}] Hit max orphan resumes ({MAX_ORPHAN_RESUMES}). "
                  f"Task may be too complex — stopping.")
        update_state_fields(session_dir, status="suspended:too-complex")
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
                                     issue_id: str):
        """Handle a review session that hit the orphan limit: fail and fall back to human review."""
        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        log.error(f"[{sid}] Review session hit max orphan resumes ({MAX_ORPHAN_RESUMES}). "
                  f"Falling back to human review for coder session {coder_sid}.")
        update_state_fields(session_dir, status="suspended:review-failed")

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

    def _verify_branch_exists(self, session_state) -> bool:
        """Verify the agent branch exists before resuming.

        Args:
            session_state: SessionState dataclass or dict with branch/issue_id fields.

        Returns True if the branch exists, False if missing.
        """
        branch = getattr(session_state, "branch", "") or (
            session_state.get("branch", "") if isinstance(session_state, dict) else ""
        )
        if not branch:
            return False
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True, cwd=self.repo_dir
        )
        if result.returncode != 0:
            issue_id = getattr(session_state, "issue_id", "") or (
                session_state.get("issue_id", "?") if isinstance(session_state, dict) else "?"
            )
            log.error("Branch %s missing for session (issue %s)", branch, issue_id)
            return False
        return True

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
                state_mgr = StateManager(session_dir)
                session_state = state_mgr.load_state()
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for auth-retry check: {e}")
                continue

            if state_mgr.status != "suspended:auth-failure":
                continue

            issue_id = session_state.issue_id
            if not issue_id:
                continue

            auth_retries = session_state.auth_retries
            if auth_retries >= MAX_AUTH_RETRIES:
                log.warning(f"[{sid}] Auth retry limit reached ({MAX_AUTH_RETRIES}). "
                            f"Token still invalid — giving up.")
                update_state_fields(session_dir, status="suspended:auth-failure-permanent")
                self.telegram.notify(
                    f"🔑 `{sid}` hit {MAX_AUTH_RETRIES} auth retries — token still invalid. "
                    f"Giving up. Fix credentials and `nightshift resume` manually.",
                    level=NotificationLevel.ACTIONS)
                continue

            log.info(f"[{sid}] Auth-failure session — retrying (token may have been refreshed, "
                     f"attempt {auth_retries + 1}/{MAX_AUTH_RETRIES})")
            update_state_fields(session_dir, auth_retries=auth_retries + 1, status="working")
            self._recently_launched[sid] = time.time()

            self._resume_session(sid, issue_id, reason="auth-failure retry")

    def check_provider_outages(self):
        """Retry sessions suspended due to provider overload on a slow interval."""
        now = time.time()
        if now - self._last_provider_outage_check < PROVIDER_OUTAGE_RETRY_INTERVAL_S:
            return
        self._last_provider_outage_check = now

        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue

            try:
                state_mgr = StateManager(session_dir)
                session_state = state_mgr.load_state()
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for provider-outage check: {e}")
                continue

            if state_mgr.status != "suspended:provider-overload":
                continue

            issue_id = session_state.issue_id
            if not issue_id:
                continue

            overload_retries = session_state.overload_resumes
            if overload_retries >= MAX_PROVIDER_OUTAGE_RETRIES:
                log.warning(f"[{sid}] Provider outage retry limit reached ({MAX_PROVIDER_OUTAGE_RETRIES}). "
                            f"Provider still overloaded — giving up.")
                update_state_fields(session_dir, status="suspended:provider-overload-permanent")
                self.telegram.notify(
                    f"⏳ `{sid}` hit {MAX_PROVIDER_OUTAGE_RETRIES} provider outage retries — "
                    f"provider still overloaded. Giving up. `nightshift resume` manually when available.",
                    level=NotificationLevel.ACTIONS)
                continue

            log.info(f"[{sid}] Provider-overload session — retrying (provider may be available, "
                     f"attempt {overload_retries + 1}/{MAX_PROVIDER_OUTAGE_RETRIES})")
            update_state_fields(session_dir, overload_resumes=overload_retries + 1, status="working")
            self._recently_launched[sid] = time.time()

            self._resume_session(sid, issue_id, reason="provider-outage retry")

    def check_zombie_containers(self):
        """Detect containers that are running but stuck (no events for extended time).

        A zombie container is one where:
        - Session status is 'working' or 'starting'
        - Docker container is running
        - No events for longer than stall_timeout_s * ZOMBIE_TIMEOUT_MULTIPLIER

        This differs from orphan detection: orphans have no container running,
        zombies have a running container that's stuck (infinite loop, deadlock).
        """
        now = time.time()
        if now - self._last_zombie_check < ZOMBIE_CHECK_INTERVAL_S:
            return
        self._last_zombie_check = now

        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue
            self._check_session_for_zombie(session_dir, sid, now)

    def _check_session_for_zombie(self, session_dir: Path, sid: str, now: float):
        """Check a single session for zombie container behavior."""
        try:
            state_mgr = StateManager(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read state for zombie check: {e}")
            return

        status = state_mgr.status
        if status not in _ACTIVE_ORPHAN_STATUSES:
            # Only check sessions that should be actively producing events
            return

        # Check if container is actually running
        container = f"nightshift-{sid}"
        if _pkg().docker_container_status(container) not in ("running",):
            # Container not running — orphan detector handles this
            return

        # Check last event time via raw-output.log mtime
        last_event_time = self._get_last_event_time(session_dir)
        if last_event_time is None:
            # No raw-output.log yet — session just started
            return

        stall_timeout = DEFAULT_STALL_TIMEOUT_S
        try:
            config = load_workflow(self.workflow_path)
            stall_timeout = config.agent.stall_timeout_s
        except Exception as e:
            log.debug(f"[{sid}] Could not load workflow config for stall timeout: {e}")

        zombie_threshold = stall_timeout * ZOMBIE_TIMEOUT_MULTIPLIER
        elapsed = now - last_event_time

        if elapsed > zombie_threshold:
            # Avoid duplicate alerts for the same session
            if sid in self._alerted_zombies:
                return
            self._alerted_zombies.add(sid)

            log.warning(f"[{sid}] Container may be stuck: no events for {elapsed:.0f}s "
                        f"(threshold: {zombie_threshold:.0f}s)")
            self.telegram.notify(
                f"⚠️ `{sid}` container may be stuck — no events for {elapsed:.0f}s. "
                f"Consider checking logs or restarting.",
                level=NotificationLevel.ACTIONS)
        else:
            # Container is active — clear from alerted set if it was there
            self._alerted_zombies.discard(sid)

    def check_session_sizes(self):
        """Warn when session directories grow too large."""
        now = time.time()
        if now - self._last_session_size_check < SESSION_SIZE_CHECK_INTERVAL_S:
            return
        self._last_session_size_check = now

        if not self.sessions_dir.exists():
            return

        warning_threshold_bytes = SIZE_WARNING_THRESHOLD_MB * 1024 * 1024
        critical_threshold_bytes = SIZE_CRITICAL_THRESHOLD_MB * 1024 * 1024

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue

            total_bytes = 0
            try:
                for root, _, files in os.walk(session_dir):
                    for filename in files:
                        file_path = os.path.join(root, filename)
                        try:
                            total_bytes += os.path.getsize(file_path)
                        except OSError as e:
                            log.debug(f"[{sid}] Failed to stat {file_path}: {e}")
            except OSError as e:
                log.warning(f"[{sid}] Failed to scan session directory size: {e}")
                continue

            if total_bytes <= warning_threshold_bytes:
                self._alerted_large_sessions.discard(sid)
                continue

            total_mb = total_bytes / (1024 * 1024)
            if total_bytes > critical_threshold_bytes:
                if sid in self._alerted_large_sessions:
                    continue
                self._alerted_large_sessions.add(sid)
                log.warning(f"[{sid}] Session size is {total_mb:.1f} MB "
                            f"(critical threshold: {SIZE_CRITICAL_THRESHOLD_MB} MB)")
                self.telegram.notify(
                    f"⚠️ `{sid}` session size is {total_mb:.1f} MB. "
                    f"Critical threshold: {SIZE_CRITICAL_THRESHOLD_MB} MB. "
                    f"Consider pruning logs or archived output.",
                    level=NotificationLevel.ACTIONS)
                continue

            self._alerted_large_sessions.discard(sid)
            log.warning(f"[{sid}] Session size is {total_mb:.1f} MB "
                        f"(warning threshold: {SIZE_WARNING_THRESHOLD_MB} MB)")

    def _get_last_event_time(self, session_dir: Path) -> float | None:
        """Get the timestamp of the last event from raw-output.log mtime.

        Returns None if the file doesn't exist.
        """
        raw_log = session_dir / "raw-output.log"
        if not raw_log.exists():
            return None
        try:
            return raw_log.stat().st_mtime
        except OSError:
            return None

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
                state_mgr = StateManager(session_dir)
                session_state = state_mgr.load_state()
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for closed-issue check: {e}")
                continue

            issue_id = session_state.issue_id
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
            if _issue_id_prefix_match(issue.id, existing_issue_ids):
                continue

            # Skip issues blocked by dependencies
            if is_blocked(issue.labels):
                log.debug(f"Auto-start: skipping {issue.identifier} (blocked by dependency)")
                continue

            if active_count >= asc.max_concurrent:
                log.info(f"Auto-start: at max concurrent ({asc.max_concurrent}), "
                         f"deferring {issue.identifier}")
                break

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
