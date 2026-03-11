#!/usr/bin/env python3
"""Host watcher — pauses idle containers, collects answers, monitors reviews.

Handles three concerns:
1. Q&A: pauses containers on waiting.json, collects answers via Telegram/CLI
2. Review: monitors waiting:review sessions for @nightshift commands in
   tracker comments or Telegram replies, triggers revise/accept/reject
3. Telegram relay: posts Telegram replies as tracker comments for audit trail

    python host/watcher.py --sessions-dir .nightshift/sessions
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from host.env import load_all_dotenv
from host.session_utils import (
    get_repo_root, read_state, write_state, update_status as _update_status,
    force_remove_dir, remove_worktree,
)
from host.docker_utils import (
    docker_pause, docker_unpause, docker_stop, docker_container_status,
)
from host.constants import (
    REVIEW_POLL_INTERVAL_S, MAIN_LOOP_SLEEP_S, PRE_PAUSE_DELAY_S,
    STILL_WAITING_LOG_INTERVAL_S, ORPHAN_GRACE_PERIOD_S,
    COMMAND_BACKOFF_BASE_S, COMMAND_BACKOFF_CAP_S, COMMAND_BACKOFF_CAP_CYCLES,
    TG_LONG_POLL_TIMEOUT_S, TG_HTTP_TIMEOUT_S, TG_POST_TIMEOUT_S,
    TG_MESSAGE_SOFT_LIMIT, TG_TRUNCATION_POINT,
)
from core.review import (
    parse_nightshift_command, strip_nightshift_command,
    collect_review_feedback, build_revise_prompt,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [watcher] %(message)s")
log = logging.getLogger("watcher")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

_ACTIVE_STATUSES = ("working", "starting", "waiting:answer")


class HostWatcher:
    """Monitors session dirs, pauses containers, polls Telegram, watches reviews.

    The contract:
    - Container writes /session/waiting.json when it needs an answer.
    - Watcher writes /session/answer.txt when it has one.
    - Container reads answer.txt and continues.
    - For reviews: watcher polls tracker comments for @nightshift commands.

    Answer sources (checked in order):
    1. Telegram reply (if configured)
    2. Manual: user runs `nightshift answer <id> "text"` which writes answer.txt directly
    """

    def __init__(self, sessions_dir: Path, repo_dir: Path, auto_start: bool = True):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.auto_start = auto_start
        self._auto_start_config = None  # Lazy-loaded from WORKFLOW.md

        # Telegram config (optional)
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.tg_enabled = HAS_REQUESTS and bool(self.tg_token and self.tg_chat)
        self._tg_offset = 0

        # Track paused sessions: session_id -> metadata
        self._paused: dict[str, dict] = {}

        # Review monitoring: track last-seen comment count per session
        self._review_comment_counts: dict[str, int] = {}
        self._last_review_poll = 0.0
        self._last_orphan_check = 0.0
        self._last_auto_start_poll = 0.0
        self._last_closed_check = 0.0
        self._known_issue_ids: set[str] = set()

        # Track recently launched sessions to avoid orphan false positives
        # Maps sid -> launch timestamp
        self._recently_launched: dict[str, float] = {}

        # Track failed commands to avoid retrying too fast
        # Maps sid -> (last_attempt_time, attempt_count)
        self._command_failures: dict[str, tuple[float, int]] = {}

        # Track reviewer <-> coder round counts: coder_sid -> rounds
        self._review_rounds: dict[str, int] = {}

        # Tracker (lazy-initialized on first review poll)
        self._tracker = None
        self._config = None

    def _get_tracker(self):
        """Lazy-init tracker from WORKFLOW.md."""
        if self._tracker is None:
            from core.config import load_workflow, create_tracker
            self._config = load_workflow(self.repo_dir / "WORKFLOW.md")
            self._tracker = create_tracker(self._config, repo_dir=str(self.repo_dir))
        return self._tracker

    def _get_auto_start_config(self):
        """Lazy-load auto_start config from WORKFLOW.md."""
        if self._auto_start_config is None:
            from core.config import load_workflow
            if self._config is None:
                self._config = load_workflow(self.repo_dir / "WORKFLOW.md")
            self._auto_start_config = self._config.auto_start
        return self._auto_start_config

    def run(self):
        log.info(f"Watching {self.sessions_dir}")
        if self.tg_enabled:
            log.info("Telegram polling enabled")
        else:
            log.info("Telegram not configured — answers via CLI only")
        if self.auto_start:
            asc = self._get_auto_start_config()
            if asc.enabled:
                log.info(f"Auto-start enabled — label={asc.label!r}, "
                         f"poll={asc.poll_interval_s}s, max_concurrent={asc.max_concurrent}")
            else:
                log.info("Auto-start: enabled via CLI but disabled in WORKFLOW.md config "
                         "(set auto_start.enabled: true)")
                self.auto_start = False
        else:
            log.info("Auto-start disabled")

        while True:
            # Single Telegram poll, routes to Q&A or review
            tg_answers, tg_reviews = self._poll_telegram_all() if self.tg_enabled else ({}, {})
            self._scan_for_waiting()
            self._check_for_answers(tg_answers)
            # Sync tracker once per review poll cycle (not per-method)
            self._maybe_sync_tracker()
            self._check_reviews(tg_reviews)
            self._check_for_auto_review()
            self._check_reviewer_done()
            self._check_orphaned_sessions()
            self._check_closed_issues()
            if self.auto_start:
                self._check_new_issues()
            time.sleep(MAIN_LOOP_SLEEP_S)

    def _maybe_sync_tracker(self):
        """Sync tracker at most once per review poll interval."""
        now = time.time()
        if now - self._last_review_poll < REVIEW_POLL_INTERVAL_S:
            return
        try:
            self._get_tracker().sync()
        except Exception as e:
            log.warning(f"Tracker sync failed: {e}")

    def _launch_background(self, cmd: list[str], sid: str):
        """Launch a subprocess in background, logging its output."""
        log_file = self.sessions_dir.parent / "watcher.log"
        try:
            f = open(log_file, "a")
            subprocess.Popen(cmd, cwd=str(self.repo_dir), stdout=f, stderr=f)
        except Exception as e:
            log.error(f"[{sid}] Failed to launch {cmd}: {e}")

    def _scan_for_waiting(self):
        """Detect new waiting.json files → pause those containers."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            waiting_file = session_dir / "waiting.json"

            if waiting_file.exists() and sid not in self._paused:
                try:
                    data = json.loads(waiting_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                container = f"nightshift-{sid}"

                # Brief delay to let container finish writing state
                time.sleep(PRE_PAUSE_DELAY_S)

                if docker_pause(container):
                    self._paused[sid] = {
                        "question": data.get("question", ""),
                        "issue_id": data.get("issue_id", ""),
                        "container": container,
                        "dir": session_dir,
                        "paused_at": time.time(),
                        "tg_msg_id": None,
                    }
                    log.info(f"[{sid}] Paused. Question: {data.get('question', '')[:60]}")

                    # Forward question to Telegram if not already sent by container
                    if self.tg_enabled and data.get("question"):
                        msg_id = self._tg_send_question(
                            sid, data["question"], data.get("issue_id", "")[:12]
                        )
                        self._paused[sid]["tg_msg_id"] = msg_id
                else:
                    log.warning(f"[{sid}] Pause failed — container will poll internally")

    def _check_for_answers(self, tg_replies: dict[str, str]):
        """Check for answers (Telegram + CLI), write answer.txt, unpause."""

        for sid, info in list(self._paused.items()):
            answer_file = info["dir"] / "answer.txt"

            # Check if someone wrote answer.txt directly (via CLI)
            if answer_file.exists():
                log.info(f"[{sid}] answer.txt found (via CLI). Unpausing.")
                docker_unpause(info["container"])
                del self._paused[sid]
                continue

            # Check Telegram replies
            if sid in tg_replies:
                answer = tg_replies[sid]
                log.info(f"[{sid}] Telegram reply: {answer[:60]}")
                answer_file.write_text(answer)
                docker_unpause(info["container"])
                log.info(f"[{sid}] Unpaused.")
                del self._paused[sid]
                continue

            # Log periodic status
            elapsed = time.time() - info["paused_at"]
            if int(elapsed) % STILL_WAITING_LOG_INTERVAL_S == 0 and int(elapsed) > 0:
                log.info(f"[{sid}] Still waiting ({elapsed/60:.0f}m)")

    # --- Automated review step ---

    def _check_for_auto_review(self):
        """Detect waiting:review coder sessions, launch reviewer if REVIEW.md exists."""
        if not self.sessions_dir.exists():
            return

        review_md = self.repo_dir / "REVIEW.md"
        if not review_md.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir() or session_dir.name.startswith("review-"):
                continue
            sid = session_dir.name
            if not (session_dir / "state.json").exists():
                continue
            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError):
                continue
            if state.get("status") != "waiting:review":
                continue
            issue_id = state.get("issue_id", "")
            if issue_id:
                self._maybe_launch_review(sid, session_dir, issue_id, review_md)

    def _maybe_launch_review(self, sid: str, session_dir: Path,
                              issue_id: str, review_md: Path):
        """Launch a review session for sid, or escalate if max rounds reached."""
        try:
            from core.config import load_workflow
            review_config = load_workflow(review_md)
            max_rounds = review_config.review.max_rounds
        except Exception:
            max_rounds = 3

        rounds = self._review_rounds.get(sid, 0)
        if rounds >= max_rounds:
            log.info(f"[{sid}] Max review rounds ({max_rounds}) reached — escalating")
            _update_status(session_dir, "waiting:human-review")
            self._tg_notify(
                f"⚠️ `{sid}` hit max review rounds ({max_rounds}). "
                f"Escalating to human review.\n"
                f"`nightshift accept/reject/revise {issue_id}`")
            return

        _update_status(session_dir, "reviewing")
        review_sid = f"review-{sid}"
        self._recently_launched[review_sid] = time.time()
        self._review_rounds[sid] = rounds + 1

        log.info(f"[{sid}] Launching automated review (round {rounds + 1}/{max_rounds})")
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "launch.py"),
            issue_id,
            "--workflow", str(review_md),
            "--step", "review",
            "--coder-session", sid,
        ]
        self._launch_background(cmd, review_sid)

    def _check_reviewer_done(self):
        """Check if reviewer sessions have finished, handle approve/revise verdict."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name

            # Only process review sessions
            if not sid.startswith("review-"):
                continue

            if not (session_dir / "state.json").exists():
                continue

            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError):
                continue

            if state.get("status") != "waiting:review":
                continue

            # Reviewer is done — check its tracker comments for verdict
            issue_id = state.get("issue_id", "")
            coder_sid = sid[len("review-"):]  # strip "review-" prefix
            coder_dir = self.sessions_dir / coder_sid

            if not coder_dir.exists():
                continue

            # Check reviewer conversation log for @nightshift command
            conv_log = session_dir / "conversation.jsonl"
            verdict = self._extract_reviewer_verdict(conv_log, issue_id)

            if not verdict:
                continue

            log.info(f"[{sid}] Reviewer verdict: {verdict}")

            if verdict == "approve":
                self._handle_reviewer_approve(coder_sid, coder_dir, issue_id)
            elif verdict == "revise":
                self._handle_reviewer_revise(coder_sid, coder_dir, issue_id, session_dir)

            # Clean up reviewer session
            self._cleanup_review_session(sid, session_dir)

    def _extract_reviewer_verdict(self, conv_log: Path, issue_id: str) -> Optional[str]:
        """Extract @nightshift approve/revise from reviewer's conversation log."""
        if not conv_log.exists():
            return None

        # Check conversation log for @nightshift commands
        for line in reversed(conv_log.read_text().strip().splitlines()):
            try:
                entry = json.loads(line)
                text = entry.get("content", "")
                cmd = parse_nightshift_command(text)
                if cmd in ("approve", "revise"):
                    return cmd
            except (json.JSONDecodeError, KeyError):
                continue

        # Also check tracker comments
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
            # Check recent comments for reviewer verdict
            for comment in reversed(comments[-5:] if len(comments) > 5 else comments):
                cmd = parse_nightshift_command(comment.body)
                if cmd in ("approve", "revise"):
                    return cmd
        except Exception as e:
            log.warning(f"Tracker poll for reviewer verdict failed: {e}")

        return None

    def _handle_reviewer_approve(self, coder_sid: str, coder_dir: Path, issue_id: str):
        """Reviewer approved — transition coder to waiting:human-review."""
        try:
            _update_status(coder_dir, "waiting:human-review")
            log.info(f"[{coder_sid}] Reviewer approved → waiting:human-review")

            self._tg_notify(
                f"✅ Automated review *approved* `{coder_sid}`.\n"
                f"Human review: `nightshift accept/reject/revise {issue_id}`")

            try:
                tracker = self._get_tracker()
                tracker.add_comment(issue_id,
                    "🤖 **Automated review: APPROVED**\n\n"
                    "Reviewer is satisfied with the changes. Awaiting human confirmation.\n\n"
                    f"Review with: `nightshift accept/reject/revise {issue_id}`")
                tracker.sync()
            except Exception as e:
                log.warning(f"[{coder_sid}] Failed to post approval to tracker: {e}")

        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer approve: {e}")

    def _collect_reviewer_feedback(self, coder_sid: str, issue_id: str,
                                    review_dir: Path) -> list[str]:
        """Collect revision feedback from reviewer conversation and tracker."""
        parts = []
        conv_log = review_dir / "conversation.jsonl"
        if conv_log.exists():
            for line in conv_log.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                    text = entry.get("content", "")
                    if "@nightshift" in text.lower() and "revise" in text.lower():
                        cleaned = strip_nightshift_command(text)
                        if cleaned:
                            parts.append(cleaned)
                except (json.JSONDecodeError, KeyError):
                    continue

        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
            for comment in reversed(comments[-5:] if len(comments) > 5 else comments):
                cmd = parse_nightshift_command(comment.body)
                if cmd == "revise":
                    cleaned = strip_nightshift_command(comment.body)
                    if cleaned:
                        parts.append(cleaned)
                    break
        except Exception as e:
            log.warning(f"[{coder_sid}] Tracker poll for review feedback failed: {e}")

        return parts or ["Reviewer requested revisions but did not provide specific feedback."]

    def _handle_reviewer_revise(self, coder_sid: str, coder_dir: Path,
                                issue_id: str, review_dir: Path):
        """Reviewer requested revisions — resume coder with feedback."""
        try:
            parts = self._collect_reviewer_feedback(coder_sid, issue_id, review_dir)
            feedback = build_revise_prompt([], inline_feedback="\n".join(parts))
            (coder_dir / "resume-prompt.md").write_text(feedback)

            _update_status(coder_dir, "working")
            self._recently_launched[coder_sid] = time.time()
            log.info(f"[{coder_sid}] Reviewer requested revisions — resuming coder")
            self._tg_notify(f"🔄 Reviewer requested revisions for `{coder_sid}`. Coder resuming.")

            cmd = [
                sys.executable,
                str(Path(__file__).parent / "launch.py"),
                issue_id, "--resume",
            ]
            self._launch_background(cmd, coder_sid)
        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer revise: {e}")

    def _cleanup_review_session(self, review_sid: str, review_dir: Path):
        """Clean up a reviewer session (worktree, branch, session dir)."""
        try:
            # Extract the coder sid to determine short_id
            coder_sid = review_sid[len("review-"):]

            from core.config import load_workflow
            review_md = self.repo_dir / "REVIEW.md"
            config = load_workflow(review_md) if review_md.exists() else load_workflow(self.repo_dir / "WORKFLOW.md")

            wt = self.repo_dir / config.workspace.root / f"review-{coder_sid}"
            branch = f"review/{coder_sid}"

            remove_worktree(self.repo_dir, wt, branch)

            shutil.rmtree(review_dir, ignore_errors=True)

            self._recently_launched.pop(review_sid, None)
            self._review_comment_counts.pop(review_sid, None)

            log.info(f"[{review_sid}] Cleaned up reviewer session")
        except Exception as e:
            log.error(f"[{review_sid}] Reviewer cleanup failed: {e}")

    # --- Review monitoring ---

    def _check_reviews(self, tg_review_replies: dict[str, tuple[str, str]]):
        """Poll tracker for @nightshift commands on waiting:review sessions."""
        now = time.time()
        if now - self._last_review_poll < REVIEW_POLL_INTERVAL_S:
            return
        self._last_review_poll = now

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
            except (json.JSONDecodeError, OSError):
                continue
            if state.get("status") not in ("waiting:review", "waiting:human-review"):
                self._review_comment_counts.pop(sid, None)
                continue
            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            if sid in tg_review_replies:
                tg_text, tg_author = tg_review_replies[sid]
                self._post_telegram_review_to_tracker(issue_id, tg_text, tg_author)

            self._poll_review_comments(sid, issue_id, session_dir)

    def _poll_review_comments(self, sid: str, issue_id: str, session_dir: Path):
        """Check tracker for new @nightshift commands on a review session."""
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
        except Exception as e:
            log.warning(f"[{sid}] Tracker poll failed: {e}")
            return

        last_count = self._review_comment_counts.get(sid, 0)
        if len(comments) <= last_count:
            return

        self._review_comment_counts[sid] = len(comments)

        if last_count == 0:
            if comments:
                cmd = parse_nightshift_command(comments[-1].body)
                if cmd:
                    log.info(f"[{sid}] Found pending @nightshift {cmd} from {comments[-1].author}")
                    self._handle_review_command(sid, issue_id, cmd, session_dir)
            return

        for comment in comments[last_count:]:
            cmd = parse_nightshift_command(comment.body)
            if cmd:
                log.info(f"[{sid}] Found @nightshift {cmd} from {comment.author}")
                self._handle_review_command(sid, issue_id, cmd, session_dir)
                break

    # --- Orphan detection ---

    def _check_orphaned_sessions(self):
        """Detect sessions with status 'working' but no running container — auto-resume."""
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
            self._maybe_resume_orphan(session_dir, sid, now)

    def _maybe_resume_orphan(self, session_dir: Path, sid: str, now: float):
        """Check a single session and auto-resume if orphaned."""
        try:
            state = read_state(session_dir)
        except (json.JSONDecodeError, OSError):
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
        if docker_container_status(container) in ("running", "paused"):
            return

        issue_id = state.get("issue_id", "")
        if not issue_id:
            return

        log.info(f"[{sid}] Orphaned session (container gone, status: {state['status']}). Auto-resuming.")
        self._recently_launched[sid] = time.time()

        cmd = [
            sys.executable,
            str(Path(__file__).parent / "launch.py"),
            issue_id, "--resume",
        ]
        self._launch_background(cmd, sid)

    # --- Closed issue cleanup ---

    def _check_closed_issues(self):
        """Detect sessions whose issues have been closed — clean up worktree + session."""
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
            except (json.JSONDecodeError, OSError):
                continue

            # Only clean up sessions that are idle (not actively working)
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

            # Stop running container if any
            container = f"nightshift-{sid}"
            docker_stop(container)

            log.info(f"[{sid}] Issue closed — cleaning up worktree and session")
            self._cleanup_session(sid, issue_id, session_dir)

    def _cleanup_session(self, sid: str, issue_id: str, session_dir: Path):
        """Remove worktree, branch, and session directory."""
        try:
            from core.config import load_workflow
            config = load_workflow(self.repo_dir / "WORKFLOW.md")
            wt = self.repo_dir / config.workspace.root / f"agent-{sid}"
            branch = f"agent/{sid}"

            remove_worktree(self.repo_dir, wt, branch)

            shutil.rmtree(session_dir)

            # Clean up tracking state
            self._review_comment_counts.pop(sid, None)
            self._recently_launched.pop(sid, None)

            log.info(f"[{sid}] Cleaned up worktree, branch, and session")
        except Exception as e:
            log.error(f"[{sid}] Cleanup failed: {e}")

    # --- Auto-start ---

    def _iter_session_states(self) -> list[tuple[Path, dict]]:
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

    def _count_active_sessions(self, states=None) -> int:
        """Count sessions that are currently working or starting."""
        if states is None:
            states = self._iter_session_states()
        return sum(
            1 for _, state in states
            if state.get("status") in _ACTIVE_STATUSES
        )

    def _check_new_issues(self):
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
        all_states = self._iter_session_states()
        existing_issue_ids: set[str] = {
            state.get("issue_id", "") for _, state in all_states
        }
        active_count = self._count_active_sessions(states=all_states)

        for issue in issues:
            if issue.id in existing_issue_ids or issue.id in self._known_issue_ids:
                continue

            if active_count >= asc.max_concurrent:
                log.info(f"Auto-start: at max concurrent ({asc.max_concurrent}), "
                         f"deferring {issue.identifier}")
                break

            self._known_issue_ids.add(issue.id)
            sid = issue.id[:12]
            self._recently_launched[sid] = time.time()
            active_count += 1
            log.info(f"Auto-start: launching {issue.identifier} — {issue.title[:60]}")
            self._tg_notify(f"🚀 Auto-starting `{issue.identifier}`: {issue.title[:60]}")

            cmd = [
                sys.executable,
                str(Path(__file__).parent / "launch.py"),
                issue.id,
            ]
            self._launch_background(cmd, sid)

    def _post_telegram_review_to_tracker(self, issue_id: str, text: str, author: str):
        """Post Telegram review reply as a tracker comment for audit trail."""
        try:
            tracker = self._get_tracker()
            comment_body = f"Review from {author} via Telegram:\n\n{text}"
            tracker.add_comment(issue_id, comment_body)
            log.info(f"Posted Telegram review to tracker for {issue_id[:12]}")
        except Exception as e:
            log.warning(f"Failed to post Telegram review to tracker: {e}")

    def _handle_review_command(self, sid: str, issue_id: str, cmd: str, session_dir: Path):
        """Execute a @nightshift command on a waiting:review session."""
        # Backoff on repeated failures: wait 2^attempts minutes (max 30 min)
        if sid in self._command_failures:
            last_time, attempts = self._command_failures[sid]
            backoff_s = min(COMMAND_BACKOFF_BASE_S * (2 ** attempts), COMMAND_BACKOFF_CAP_S)
            if time.time() - last_time < backoff_s:
                return  # still in cooldown

        if cmd == "revise":
            self._do_revise(sid, issue_id, session_dir)
        elif cmd == "accept":
            self._do_cli_command(sid, "accept", issue_id)
        elif cmd == "reject":
            self._do_cli_command(sid, "reject", issue_id)
        elif cmd == "approve":
            # Manual approve (same as reviewer approve — transition to human-review)
            self._handle_reviewer_approve(sid, session_dir, issue_id)

    def _do_revise(self, sid: str, issue_id: str, session_dir: Path):
        """Collect review feedback and relaunch agent."""
        try:
            tracker = self._get_tracker()
            # Skip sync — tracker was already synced in _check_reviews
            review_comments = collect_review_feedback(tracker, issue_id, sync=False)

            if not review_comments:
                log.warning(f"[{sid}] No review feedback found — skipping revise")
                return

            feedback = build_revise_prompt(review_comments)
            (session_dir / "resume-prompt.md").write_text(feedback)

            _update_status(session_dir, "working")

            # Reset comment count tracking and mark as recently launched
            self._review_comment_counts.pop(sid, None)
            self._recently_launched[sid] = time.time()

            log.info(f"[{sid}] Revising with {len(review_comments)} comment(s)")

            # Launch in background — don't block the watcher
            cmd = [
                sys.executable,
                str(Path(__file__).parent / "launch.py"),
                issue_id, "--resume",
            ]
            self._launch_background(cmd, sid)

        except Exception as e:
            log.error(f"[{sid}] Revise failed: {e}")

    def _do_cli_command(self, sid: str, command: str, issue_id: str):
        """Run a CLI command (accept/reject) as a subprocess."""
        try:
            log.info(f"[{sid}] Running nightshift {command}")
            self._review_comment_counts.pop(sid, None)
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "cli.py"), command, issue_id],
                cwd=str(self.repo_dir), capture_output=True, text=True,
            )
            if result.returncode != 0:
                error_msg = (result.stderr.strip() + "\n" + result.stdout.strip()).strip()
                _, attempts = self._command_failures.get(sid, (0, 0))
                attempts += 1
                self._command_failures[sid] = (time.time(), attempts)
                backoff_m = min(2 ** attempts, COMMAND_BACKOFF_CAP_CYCLES)
                log.error(f"[{sid}] nightshift {command} failed (attempt {attempts}, "
                          f"retry in {backoff_m}m): {error_msg}")
                self._tg_notify(f"⚠️ `nightshift {command}` failed for `{sid}` "
                                f"(attempt {attempts}, retry in {backoff_m}m):\n\n{error_msg}")
            else:
                log.info(f"[{sid}] nightshift {command} completed")
                self._tg_notify(f"✅ `nightshift {command}` completed for `{sid}`")
                self._command_failures.pop(sid, None)
                if result.stdout.strip():
                    log.info(f"[{sid}] {result.stdout.strip()}")
        except Exception as e:
            log.error(f"[{sid}] {command} failed: {e}")

    def _poll_telegram_all(self) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        """Single Telegram poll — routes messages to Q&A answers or review commands."""
        qa: dict[str, str] = {}
        reviews: dict[str, tuple[str, str]] = {}
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                params={"offset": self._tg_offset, "timeout": TG_LONG_POLL_TIMEOUT_S},
                timeout=TG_HTTP_TIMEOUT_S,
            )
            for u in resp.json().get("result", []):
                self._tg_offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(self.tg_chat):
                    continue
                self._route_tg_message(msg, text, qa, reviews)
        except Exception as e:
            log.debug(f"Telegram poll: {e}")
        return qa, reviews

    def _route_tg_message(self, msg: dict, text: str,
                           qa: dict[str, str],
                           reviews: dict[str, tuple[str, str]]):
        """Route a single Telegram message to Q&A or review."""
        rt = msg.get("reply_to_message", {})
        reply_msg_id = rt.get("message_id") if rt else None
        author = msg.get("from", {}).get("first_name", "Unknown")
        msg_id = msg.get("message_id")

        if reply_msg_id:
            # Check if reply is to a paused Q&A question
            for sid, info in self._paused.items():
                if info.get("tg_msg_id") == reply_msg_id:
                    qa[sid] = text
                    self._tg_ack(msg_id, sid)
                    return
            # Otherwise check for @nightshift review command
            cmd = parse_nightshift_command(text)
            if cmd:
                matched_sid = self._match_session_from_text(rt.get("text", ""))
                if matched_sid:
                    reviews[matched_sid] = (text, author)
                    self._tg_ack(msg_id, matched_sid)
        else:
            cmd = parse_nightshift_command(text)
            if cmd:
                matched_sid = self._match_session_from_text(text)
                if matched_sid:
                    reviews[matched_sid] = (text, author)
                    self._tg_ack(msg_id, matched_sid)

    def _match_session_from_text(self, text: str) -> Optional[str]:
        """Find a session ID mentioned in text."""
        if not self.sessions_dir.exists():
            return None
        for session_dir in self.sessions_dir.iterdir():
            if session_dir.is_dir() and session_dir.name in text:
                return session_dir.name
        return None

    # --- Telegram (self-contained) ---

    @property
    def _project_name(self) -> str:
        return self.repo_dir.name

    def _tg_notify(self, text: str):
        """Send a plain notification to Telegram (no reply expected)."""
        if not self.tg_enabled:
            return
        text = f"[{self._project_name}] {text}"
        if len(text) > TG_MESSAGE_SOFT_LIMIT:
            text = text[:TG_TRUNCATION_POINT] + "\n\n… (truncated, see watcher.log)"
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": text,
                    "parse_mode": "Markdown",
                }, timeout=TG_POST_TIMEOUT_S,
            )
        except Exception as e:
            log.warning(f"Telegram notify failed: {e}")

    def _tg_send_question(self, sid: str, question: str, short_id: str) -> Optional[int]:
        """Send question to Telegram with force_reply. Returns message_id."""
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": (
                        f"[{self._project_name}] ❓ *Question*\n"
                        f"*Issue:* `{short_id}`\n"
                        f"*Q:* {question}\n\n"
                        f"_Reply to answer._"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "force_reply": True, "selective": True,
                        "input_field_placeholder": "Answer...",
                    },
                }, timeout=TG_POST_TIMEOUT_S,
            )
            d = resp.json()
            return d["result"]["message_id"] if d.get("ok") else None
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return None

    def _tg_ack(self, reply_to: int, sid: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": f"[{self._project_name}] ✅ Received for `{sid}`.",
                    "parse_mode": "Markdown",
                    "reply_to_message_id": reply_to,
                }, timeout=TG_POST_TIMEOUT_S,
            )
        except Exception as e:
            log.warning(f"Telegram ack failed: {e}")


def main():
    p = argparse.ArgumentParser(description="Host watcher — pause/unpause, review monitor")
    p.add_argument("--sessions-dir", required=True, help=".nightshift/sessions path")
    p.add_argument("--no-auto-start", action="store_true",
                   help="Disable automatic starting of new issues")
    p.add_argument("--log-file", default=None,
                   help="Log to file instead of stderr")
    a = p.parse_args()

    # Reconfigure logging to file if requested
    if a.log_file:
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [watcher] %(message)s",
            filename=a.log_file,
        )

    # Load .env from repo root (does not override existing env vars)
    try:
        repo = get_repo_root()
        load_all_dotenv(repo / ".env")
    except subprocess.CalledProcessError:
        repo = Path.cwd()

    HostWatcher(Path(a.sessions_dir), repo, auto_start=not a.no_auto_start).run()

if __name__ == "__main__":
    main()
