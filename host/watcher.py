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
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from host.env import load_all_dotenv
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

# How often to poll tracker for review commands (seconds)
REVIEW_POLL_INTERVAL_S = 30


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

    def run(self):
        log.info(f"Watching {self.sessions_dir}")
        if self.tg_enabled:
            log.info("Telegram polling enabled")
        else:
            log.info("Telegram not configured — answers via CLI only")
        if self.auto_start:
            log.info("Auto-start enabled — polling tracker for new issues")
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
            self._check_orphaned_sessions()
            self._check_closed_issues()
            if self.auto_start:
                self._check_new_issues()
            time.sleep(2)

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
                time.sleep(1)

                if self._docker_pause(container):
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
                self._docker_unpause(info["container"])
                del self._paused[sid]
                continue

            # Check Telegram replies
            if sid in tg_replies:
                answer = tg_replies[sid]
                log.info(f"[{sid}] Telegram reply: {answer[:60]}")
                answer_file.write_text(answer)
                self._docker_unpause(info["container"])
                log.info(f"[{sid}] Unpaused.")
                del self._paused[sid]
                continue

            # Log periodic status
            elapsed = time.time() - info["paused_at"]
            if int(elapsed) % 300 == 0 and int(elapsed) > 0:
                log.info(f"[{sid}] Still waiting ({elapsed/60:.0f}m)")

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
            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                state = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            if state.get("status") != "waiting:review":
                # Clean up tracking for non-review sessions
                self._review_comment_counts.pop(sid, None)
                continue

            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            # Post Telegram review reply as tracker comment first
            if sid in tg_review_replies:
                tg_text, tg_author = tg_review_replies[sid]
                self._post_telegram_review_to_tracker(issue_id, tg_text, tg_author)

            # Poll tracker for new comments with @nightshift command
            try:
                tracker = self._get_tracker()
                comments = tracker.get_comments(issue_id)
            except Exception as e:
                log.warning(f"[{sid}] Tracker poll failed: {e}")
                continue

            last_count = self._review_comment_counts.get(sid, 0)
            if len(comments) <= last_count:
                continue

            self._review_comment_counts[sid] = len(comments)

            # On first scan, check the last comment for a pending command
            # (handles commands posted before watcher started)
            if last_count == 0:
                if comments:
                    cmd = parse_nightshift_command(comments[-1].body)
                    if cmd:
                        log.info(f"[{sid}] Found pending @nightshift {cmd} from {comments[-1].author}")
                        self._handle_review_command(sid, issue_id, cmd, session_dir)
                continue

            # Check new comments for @nightshift commands
            new_comments = comments[last_count:]
            for comment in new_comments:
                cmd = parse_nightshift_command(comment.body)
                if cmd:
                    log.info(f"[{sid}] Found @nightshift {cmd} from {comment.author}")
                    self._handle_review_command(sid, issue_id, cmd, session_dir)
                    break  # one command per poll cycle

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
            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                state = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            if state.get("status") not in ("working", "starting"):
                continue

            # Skip if recently launched (give it time to start)
            if sid in self._recently_launched:
                if now - self._recently_launched[sid] < 120:  # 2 min grace
                    continue
                else:
                    del self._recently_launched[sid]

            # Skip if container is still running
            container = f"nightshift-{sid}"
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip() in ("running", "paused"):
                continue

            # Container is gone but status is working — orphaned
            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            log.info(f"[{sid}] Orphaned session (container gone, status: {state['status']}). Auto-resuming.")
            self._recently_launched[sid] = time.time()

            # Launch resume in background
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
            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                state = json.loads(state_file.read_text())
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
            subprocess.run(["docker", "stop", container], capture_output=True)

            log.info(f"[{sid}] Issue closed — cleaning up worktree and session")
            self._cleanup_session(sid, issue_id, session_dir)

    def _cleanup_session(self, sid: str, issue_id: str, session_dir: Path):
        """Remove worktree, branch, and session directory."""
        try:
            from core.config import load_workflow
            config = load_workflow(self.repo_dir / "WORKFLOW.md")
            wt = self.repo_dir / config.workspace.root / f"agent-{sid}"
            branch = f"agent/{sid}"

            # Remove worktree
            if wt.exists():
                result = subprocess.run(
                    ["git", "worktree", "remove", str(wt), "--force"],
                    capture_output=True, cwd=str(self.repo_dir),
                )
                if result.returncode != 0:
                    import shutil
                    try:
                        shutil.rmtree(wt)
                    except PermissionError:
                        subprocess.run(["docker", "run", "--rm",
                                        "-v", f"{wt}:/cleanup:rw",
                                        "ubuntu:24.04", "rm", "-rf", "/cleanup"],
                                       capture_output=True)
                        try:
                            shutil.rmtree(wt)
                        except FileNotFoundError:
                            pass

            subprocess.run(["git", "worktree", "prune"],
                           capture_output=True, cwd=str(self.repo_dir))
            subprocess.run(["git", "branch", "-D", branch],
                           capture_output=True, cwd=str(self.repo_dir))

            # Remove session dir
            import shutil
            shutil.rmtree(session_dir)

            # Clean up tracking state
            self._review_comment_counts.pop(sid, None)
            self._recently_launched.pop(sid, None)

            log.info(f"[{sid}] Cleaned up worktree, branch, and session")
        except Exception as e:
            log.error(f"[{sid}] Cleanup failed: {e}")

    # --- Auto-start ---

    def _check_new_issues(self):
        """Poll tracker for open issues and start sessions for new ones."""
        now = time.time()
        if now - self._last_auto_start_poll < REVIEW_POLL_INTERVAL_S:
            return
        self._last_auto_start_poll = now

        try:
            tracker = self._get_tracker()
            issues = tracker.list_issues(status="open")
        except Exception as e:
            log.warning(f"Auto-start: tracker poll failed: {e}")
            return

        # Build set of issue IDs that already have sessions
        existing_sids = set()
        if self.sessions_dir.exists():
            for session_dir in self.sessions_dir.iterdir():
                if session_dir.is_dir() and (session_dir / "state.json").exists():
                    try:
                        state = json.loads((session_dir / "state.json").read_text())
                        existing_sids.add(state.get("issue_id", ""))
                    except (json.JSONDecodeError, OSError):
                        pass

        for issue in issues:
            if issue.id in existing_sids or issue.id in self._known_issue_ids:
                continue

            self._known_issue_ids.add(issue.id)
            sid = issue.id[:12]
            self._recently_launched[sid] = time.time()
            log.info(f"Auto-start: new issue {issue.identifier} — {issue.title[:60]}")

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
            backoff_s = min(60 * (2 ** attempts), 1800)
            if time.time() - last_time < backoff_s:
                return  # still in cooldown

        if cmd == "revise":
            self._do_revise(sid, issue_id, session_dir)
        elif cmd == "accept":
            self._do_cli_command(sid, "accept", issue_id)
        elif cmd == "reject":
            self._do_cli_command(sid, "reject", issue_id)

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

            state = json.loads((session_dir / "state.json").read_text())
            state["status"] = "working"
            (session_dir / "state.json").write_text(json.dumps(state, indent=2))

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
                error_msg = result.stderr.strip()
                _, attempts = self._command_failures.get(sid, (0, 0))
                attempts += 1
                self._command_failures[sid] = (time.time(), attempts)
                backoff_m = min(2 ** attempts, 30)
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
        """Single Telegram poll — routes messages to Q&A answers or review commands.

        Returns (qa_replies, review_replies) where:
        - qa_replies: {session_id: answer_text} for paused Q&A sessions
        - review_replies: {session_id: (text, author)} for waiting:review sessions
        """
        qa: dict[str, str] = {}
        reviews: dict[str, tuple[str, str]] = {}
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                params={"offset": self._tg_offset, "timeout": 1}, timeout=5,
            )
            for u in resp.json().get("result", []):
                self._tg_offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                rt = msg.get("reply_to_message", {})

                if not text:
                    continue
                if str(msg.get("chat", {}).get("id")) != str(self.tg_chat):
                    continue

                reply_msg_id = rt.get("message_id") if rt else None
                author = msg.get("from", {}).get("first_name", "Unknown")

                # Route 1: reply to a paused Q&A session
                if reply_msg_id:
                    for sid, info in self._paused.items():
                        if info.get("tg_msg_id") == reply_msg_id:
                            qa[sid] = text
                            self._tg_ack(msg.get("message_id"), sid)
                            break
                    else:
                        # Route 2: reply with @nightshift command (review)
                        cmd = parse_nightshift_command(text)
                        if cmd:
                            rt_text = rt.get("text", "")
                            matched_sid = self._match_session_from_text(rt_text)
                            if matched_sid:
                                reviews[matched_sid] = (text, author)
                                self._tg_ack(msg.get("message_id"), matched_sid)
                else:
                    # Non-reply message with @nightshift command
                    cmd = parse_nightshift_command(text)
                    if cmd:
                        # Try to match from message text itself
                        matched_sid = self._match_session_from_text(text)
                        if matched_sid:
                            reviews[matched_sid] = (text, author)
                            self._tg_ack(msg.get("message_id"), matched_sid)

        except Exception as e:
            log.debug(f"Telegram poll: {e}")

        return qa, reviews

    def _match_session_from_text(self, text: str) -> Optional[str]:
        """Find a session ID mentioned in text."""
        if not self.sessions_dir.exists():
            return None
        for session_dir in self.sessions_dir.iterdir():
            if session_dir.is_dir() and session_dir.name in text:
                return session_dir.name
        return None

    # --- Docker ---

    def _docker_pause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "pause", container], capture_output=True,
        ).returncode == 0

    def _docker_unpause(self, container: str) -> bool:
        return subprocess.run(
            ["docker", "unpause", container], capture_output=True,
        ).returncode == 0

    # --- Telegram (self-contained) ---

    def _tg_notify(self, text: str):
        """Send a plain notification to Telegram (no reply expected)."""
        if not self.tg_enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat,
                    "text": text,
                    "parse_mode": "Markdown",
                }, timeout=10,
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
                        f"❓ *Question*\n"
                        f"*Issue:* `{short_id}`\n"
                        f"*Q:* {question}\n\n"
                        f"_Reply to answer._"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "force_reply": True, "selective": True,
                        "input_field_placeholder": "Answer...",
                    },
                }, timeout=10,
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
                    "text": f"✅ Received for `{sid}`.",
                    "parse_mode": "Markdown",
                    "reply_to_message_id": reply_to,
                }, timeout=10,
            )
        except Exception:
            pass


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
        repo = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        load_all_dotenv(repo / ".env")
    except subprocess.CalledProcessError:
        repo = Path.cwd()

    HostWatcher(Path(a.sessions_dir), repo, auto_start=not a.no_auto_start).run()

if __name__ == "__main__":
    main()
