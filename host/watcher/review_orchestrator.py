"""Review orchestration: auto-review launch, reviewer session lifecycle.

Delegates verdict handling to VerdictHandler and command execution to CommandExecutor.
"""

import json
import logging
import sys
import time
from pathlib import Path

from host.constants import (
    REVIEW_POLL_INTERVAL_S, SHORT_ID_LEN, DEFAULT_MAX_REVIEW_ROUNDS,
    REVIEW_SESSION_PREFIX,
)
from core.protocols import NotificationLevel
from host.session_utils import read_state, update_status as _update_status, archive_session
from core.config import load_workflow
from core.post_run import check_empty_session
from core.review import parse_nightshift_command
from host.watcher.lifecycle_comments import post_done, read_checkpoint_count
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.verdict_handler import VerdictHandler
from host.watcher.command_executor import CommandExecutor

log = logging.getLogger("watcher")

# Directory containing the host package (host/)
_HOST_DIR = Path(__file__).resolve().parent.parent


def _pkg():
    """Lazy import of host.watcher package for test-patchable names."""
    import host.watcher as _w
    return _w


class ReviewOrchestrator:
    """Review orchestration: auto-review launch, verdict handling, review commands."""

    def __init__(self, sessions_dir: Path, repo_dir: Path,
                 telegram: TelegramRelay,
                 get_tracker, recently_launched: dict,
                 launch_background,
                 workflow_path: Path | None = None):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.workflow_path = workflow_path or (repo_dir / "WORKFLOW.md")
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._last_poll = 0.0
        self._rounds: dict[str, int] = {}
        self._posted_done: set[str] = set()

        self.verdicts = VerdictHandler(
            sessions_dir, repo_dir, telegram,
            get_tracker, recently_launched,
            lambda cmd, sid: self._launch_background(cmd, sid),
        )
        self.commands = CommandExecutor(
            sessions_dir, repo_dir, telegram,
            get_tracker, recently_launched,
            lambda cmd, sid: self._launch_background(cmd, sid),
        )

    # -- Delegated state for backward compat with tests/callers --

    @property
    def _comment_counts(self):
        return self.commands._comment_counts

    @property
    def _command_failures(self):
        return self.commands._command_failures

    # -- Delegated accessors for backward compat with tests/callers --

    def extract_reviewer_verdict(self, conv_log, issue_id):
        return self.verdicts.extract_reviewer_verdict(conv_log, issue_id)

    def handle_reviewer_approve(self, coder_sid, coder_dir, issue_id):
        return self.verdicts.handle_reviewer_approve(coder_sid, coder_dir, issue_id)

    def handle_reviewer_revise(self, coder_sid, coder_dir, issue_id, review_dir):
        return self.verdicts.handle_reviewer_revise(coder_sid, coder_dir, issue_id, review_dir)

    def collect_reviewer_feedback(self, coder_sid, issue_id, review_dir):
        return self.verdicts.collect_reviewer_feedback(coder_sid, issue_id, review_dir)

    def do_revise(self, sid, issue_id, session_dir):
        return self.commands.do_revise(sid, issue_id, session_dir)

    def do_cli_command(self, sid, command, issue_id):
        return self.commands.do_cli_command(sid, command, issue_id)

    def post_telegram_review_to_tracker(self, issue_id, text, author):
        return self.commands.post_telegram_review_to_tracker(issue_id, text, author)

    def handle_review_command(self, sid, issue_id, cmd, session_dir):
        return self._dispatch_review_command(sid, issue_id, cmd, session_dir)

    def poll_review_comments(self, sid, issue_id, session_dir):
        return self._poll_review_comments(sid, issue_id, session_dir)

    # -- Core orchestration methods --

    def check_for_auto_review(self):
        """Detect waiting:review coder sessions, launch reviewer if REVIEW.md exists."""
        if not self.sessions_dir.exists():
            return

        review_md = self.repo_dir / "REVIEW.md"
        waiting_sessions = []

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir() or session_dir.name.startswith(REVIEW_SESSION_PREFIX):
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue
            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for auto-review: {e}")
                continue
            if state.get("status") != "waiting:review":
                continue
            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            # Post done comment once per session (regardless of REVIEW.md)
            if sid not in self._posted_done:
                self._posted_done.add(sid)
                cp_count = read_checkpoint_count(session_dir)
                post_done(self._get_tracker, issue_id, sid, cp_count)

            waiting_sessions.append((sid, session_dir, issue_id))

        if not review_md.exists():
            return

        for sid, session_dir, issue_id in waiting_sessions:
            self.maybe_launch_review(sid, session_dir, issue_id, review_md)

    def maybe_launch_review(self, sid: str, session_dir: Path,
                            issue_id: str, review_md: Path):
        """Launch a review session for sid, or escalate if max rounds reached."""
        if not session_dir.exists():
            log.warning(f"[{sid[:12]}] Session directory missing, skipping review launch")
            return

        try:
            review_config = load_workflow(review_md)
            max_rounds = review_config.review.max_rounds
        except Exception as e:
            log.warning(f"[{sid}] Failed to load REVIEW.md config, using default max rounds: {e}")
            max_rounds = DEFAULT_MAX_REVIEW_ROUNDS
            base_branch = "master"
        else:
            base_branch = review_config.workspace.base_branch

        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read state before review launch: {e}")
            return

        branch = state.get("branch", "")
        if branch and check_empty_session(self.repo_dir, branch, base_branch):
            log.warning(f"[{sid}] Empty session detected — skipping review launch")
            if session_dir.exists():
                _update_status(session_dir, "waiting:human-review")
            try:
                tracker = self._get_tracker()
                tracker.add_comment(
                    issue_id,
                    "⚠️ Empty session: no commits detected. "
                    "Did you forget to commit your changes?",
                )
            except Exception as e:
                log.warning(f"[{sid}] Failed to post empty-session comment: {e}")
            self.telegram.notify(
                f"⚠️ `{sid}` empty session detected — skipping review.\n"
                f"`nightshift accept/reject/revise {issue_id}`",
                level=NotificationLevel.ACTIONS,
            )
            return

        rounds = self._rounds.get(sid, 0)
        if rounds >= max_rounds:
            self._escalate_to_human(sid, session_dir, issue_id, max_rounds)
            return

        review_sid = f"{REVIEW_SESSION_PREFIX}{sid}"

        # Clean up stale review session if it exists with completed_at set
        # This handles the race condition where the watcher restarts after
        # a review container exits but before cleanup_review_session() is called.
        # If a verdict was processed, don't launch a new review.
        if self._maybe_cleanup_stale_review(review_sid):
            return

        _update_status(session_dir, "reviewing")

        log.info(f"[{sid}] Launching automated review (round {rounds + 1}/{max_rounds})")
        cmd = [
            sys.executable,
            str(_HOST_DIR / "launch.py"),
            issue_id,
            "--workflow", str(review_md),
            "--step", "review",
            "--coder-session", sid,
        ]
        try:
            if self._launch_background(cmd, review_sid):
                self._recently_launched[review_sid] = time.time()
                self._rounds[sid] = rounds + 1
            else:
                log.warning(f"[{sid}] Review launch failed -- reverting to waiting:review")
                _update_status(session_dir, "waiting:review")
        except Exception as e:
            log.error(f"[{sid}] Review launch error: {e} -- reverting to waiting:review")
            _update_status(session_dir, "waiting:review")

    def _escalate_to_human(self, sid: str, session_dir: Path,
                           issue_id: str, max_rounds: int):
        """Escalate to human review when max rounds reached."""
        log.info(f"[{sid}] Max review rounds ({max_rounds}) reached -- escalating")
        if session_dir.exists():
            _update_status(session_dir, "waiting:human-review")
        self.telegram.notify(
            f"\u26a0\ufe0f `{sid}` hit max review rounds ({max_rounds}). "
            f"Escalating to human review.\n"
            f"`nightshift accept/reject/revise {issue_id}`",
            level=NotificationLevel.ACTIONS)

    def _maybe_cleanup_stale_review(self, review_sid: str) -> bool:
        """Clean up a stale review session if it exists with completed_at set.

        This handles the race condition where the watcher restarts after a review
        container exits (setting completed_at) but before cleanup_review_session()
        is called. Without this cleanup, launching a new review fails with
        'session already exists'.

        IMPORTANT: Before cleanup, this method extracts and processes the verdict
        from the review session to ensure the coder session state is updated.
        Otherwise, the coder remains in waiting:review status, triggering an
        infinite relaunch cycle.

        Returns True if a verdict was processed (caller should not launch review).
        """
        review_dir = self.sessions_dir / review_sid
        if not review_dir.exists() or not (review_dir / "state.json").exists():
            return False

        try:
            state = read_state(review_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{review_sid}] Failed to read state for stale check: {e}")
            return False

        # Only clean up if completed_at is set (review finished normally)
        if not state.get("completed_at"):
            return False

        log.info(f"[{review_sid}] Cleaning up stale review session before relaunch")

        # Extract coder session info
        coder_sid = review_sid[len(REVIEW_SESSION_PREFIX):]
        coder_dir = self.sessions_dir / coder_sid
        issue_id = state.get("issue_id", "")

        # Process verdict BEFORE cleanup to prevent infinite relaunch loop
        verdict_processed = False
        if coder_dir.exists() and issue_id:
            conv_log = review_dir / "conversation.jsonl"
            verdict = self.verdicts.extract_reviewer_verdict(conv_log, issue_id)
            if verdict:
                log.info(f"[{review_sid}] Processing stale verdict: {verdict}")
                if verdict == "approve":
                    self.verdicts.handle_reviewer_approve(coder_sid, coder_dir, issue_id)
                    verdict_processed = True
                elif verdict == "revise":
                    self._posted_done.discard(coder_sid)
                    self.verdicts.handle_reviewer_revise(
                        coder_sid, coder_dir, issue_id, review_dir)
                    verdict_processed = True

        self.cleanup_review_session(review_sid, review_dir)
        return verdict_processed

    def check_reviewer_done(self):
        """Check if reviewer sessions have finished, handle verdict."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not sid.startswith(REVIEW_SESSION_PREFIX):
                continue
            if not (session_dir / "state.json").exists():
                continue
            self._process_reviewer_session(sid, session_dir)

    def _process_reviewer_session(self, sid: str, session_dir: Path):
        """Check a single reviewer session for verdict or no-verdict fallback."""
        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read reviewer state: {e}")
            return

        status = state.get("status", "")

        # Review hit max-turns without emitting a verdict — fall back to human
        if status == "suspended:review-no-verdict":
            self._handle_review_no_verdict(sid, session_dir, state)
            return

        if status != "waiting:review":
            return

        issue_id = state.get("issue_id", "")
        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        coder_dir = self.sessions_dir / coder_sid
        if not coder_dir.exists():
            return

        conv_log = session_dir / "conversation.jsonl"
        verdict = self.verdicts.extract_reviewer_verdict(conv_log, issue_id)
        if not verdict:
            return

        log.info(f"[{sid}] Reviewer verdict: {verdict}")
        if verdict == "approve":
            self.verdicts.handle_reviewer_approve(coder_sid, coder_dir, issue_id)
        elif verdict == "revise":
            self._posted_done.discard(coder_sid)
            self.verdicts.handle_reviewer_revise(coder_sid, coder_dir, issue_id, session_dir)

        self.cleanup_review_session(sid, session_dir)

    def _handle_review_no_verdict(self, sid: str, session_dir: Path, state: dict):
        """Review session hit max-turns without a verdict — escalate to human review."""
        issue_id = state.get("issue_id", "")
        coder_sid = sid[len(REVIEW_SESSION_PREFIX):]
        coder_dir = self.sessions_dir / coder_sid

        log.warning(f"[{sid}] Review hit max-turns with no verdict — "
                    f"falling back to human review for {coder_sid}")

        if coder_dir.exists() and (coder_dir / "state.json").exists():
            try:
                _update_status(coder_dir, "waiting:human-review")
            except Exception as e:
                log.warning(f"[{coder_sid}] Failed to transition coder to human-review: {e}")

        try:
            tracker = self._get_tracker()
            tracker.add_comment(issue_id,
                "⚠️ Auto-review hit max-turns without a verdict — "
                "falling back to human review.")
        except Exception as e:
            log.warning(f"[{sid}] Failed to post review-no-verdict comment: {e}")

        self.telegram.notify(
            f"⚠️ `{sid}` review hit max-turns without verdict — "
            f"falling back to human review.\n"
            f"`nightshift accept/reject/revise {issue_id}`",
            level=NotificationLevel.ACTIONS)

        self.cleanup_review_session(sid, session_dir)

    def cleanup_review_session(self, review_sid: str, review_dir: Path):
        """Clean up a reviewer session (worktree, branch, session dir)."""
        try:
            coder_sid = review_sid[len(REVIEW_SESSION_PREFIX):]

            review_md = self.repo_dir / "REVIEW.md"
            config = load_workflow(review_md) if review_md.exists() else load_workflow(self.workflow_path)

            wt = self.repo_dir / config.workspace.root / f"{REVIEW_SESSION_PREFIX}{coder_sid}"
            branch = f"review/{coder_sid}"

            archive_session(review_dir, self.repo_dir)
            _pkg().remove_worktree(self.repo_dir, wt, branch)
            _pkg().shutil.rmtree(review_dir, ignore_errors=True)

            self._recently_launched.pop(review_sid, None)
            self.commands._comment_counts.pop(review_sid, None)

            log.info(f"[{review_sid}] Cleaned up reviewer session")
        except Exception as e:
            log.error(f"[{review_sid}] Reviewer cleanup failed: {e}")

    def check_reviews(self, tg_review_replies: dict[str, tuple[str, str]]):
        """Poll tracker for @nightshift commands on waiting:review sessions."""
        now = time.time()
        if now - self._last_poll < REVIEW_POLL_INTERVAL_S:
            return
        self._last_poll = now

        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue
            self._check_session_reviews(sid, session_dir, tg_review_replies)

    def _check_session_reviews(self, sid: str, session_dir: Path,
                                tg_review_replies: dict[str, tuple[str, str]]):
        """Check a single session for review commands."""
        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[{sid}] Failed to read state for review check: {e}")
            return
        if state.get("status") not in ("waiting:review", "waiting:human-review"):
            self.commands._comment_counts.pop(sid, None)
            return
        issue_id = state.get("issue_id", "")
        if not issue_id:
            return

        if sid in tg_review_replies:
            tg_text, tg_author = tg_review_replies[sid]
            self.commands.post_telegram_review_to_tracker(issue_id, tg_text, tg_author)

        self._poll_review_comments(sid, issue_id, session_dir)

    def _poll_review_comments(self, sid: str, issue_id: str, session_dir: Path):
        """Check tracker for new @nightshift commands on a review session."""
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
        except Exception as e:
            log.warning(f"[{sid}] Tracker poll failed: {e}")
            return

        last_count = self.commands._comment_counts.get(sid, 0)
        if len(comments) <= last_count:
            return

        self.commands._comment_counts[sid] = len(comments)

        if last_count == 0:
            self._handle_first_poll(sid, issue_id, comments, session_dir)
            return

        for comment in comments[last_count:]:
            cmd = parse_nightshift_command(comment.body)
            if cmd:
                log.info(f"[{sid}] Found @nightshift {cmd} from {comment.author}")
                self.handle_review_command(sid, issue_id, cmd, session_dir)
                break

    def _handle_first_poll(self, sid: str, issue_id: str,
                           comments: list, session_dir: Path):
        """Handle the first poll for a session (check latest comment only)."""
        if comments:
            cmd = parse_nightshift_command(comments[-1].body)
            if cmd:
                log.info(f"[{sid}] Found pending @nightshift {cmd} from {comments[-1].author}")
                self.handle_review_command(sid, issue_id, cmd, session_dir)

    def _dispatch_review_command(self, sid: str, issue_id: str,
                                  cmd: str, session_dir: Path):
        """Execute a @nightshift command on a waiting:review session."""
        if self.commands.is_in_cooldown(sid):
            return

        if cmd == "revise":
            self._posted_done.discard(sid)
            self.do_revise(sid, issue_id, session_dir)
        elif cmd == "accept":
            self.do_cli_command(sid, "accept", issue_id)
        elif cmd == "reject":
            self.do_cli_command(sid, "reject", issue_id)
        elif cmd == "approve":
            self.handle_reviewer_approve(sid, session_dir, issue_id)
