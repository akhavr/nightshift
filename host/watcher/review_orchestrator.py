"""Review orchestration: auto-review launch, verdict handling, review commands."""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from host.constants import (
    REVIEW_POLL_INTERVAL_S, SHORT_ID_LEN,
    COMMAND_BACKOFF_BASE_S, COMMAND_BACKOFF_CAP_S, COMMAND_BACKOFF_CAP_CYCLES,
    DEFAULT_MAX_REVIEW_ROUNDS,
)
from host.session_utils import read_state, update_status as _update_status
from core.config import load_workflow
from core.review import (
    parse_nightshift_command, strip_nightshift_command,
    collect_review_feedback, build_revise_prompt,
)
from host.watcher.telegram_relay import TelegramRelay

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
                 launch_background):
        self.sessions_dir = sessions_dir
        self.repo_dir = repo_dir
        self.telegram = telegram
        self._get_tracker = get_tracker
        self._recently_launched = recently_launched
        self._launch_background = launch_background
        self._comment_counts: dict[str, int] = {}
        self._last_poll = 0.0
        self._rounds: dict[str, int] = {}
        self._command_failures: dict[str, tuple[float, int]] = {}

    def check_for_auto_review(self):
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
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for auto-review: {e}")
                continue
            if state.get("status") != "waiting:review":
                continue
            issue_id = state.get("issue_id", "")
            if issue_id:
                self.maybe_launch_review(sid, session_dir, issue_id, review_md)

    def maybe_launch_review(self, sid: str, session_dir: Path,
                            issue_id: str, review_md: Path):
        """Launch a review session for sid, or escalate if max rounds reached."""
        try:
            review_config = load_workflow(review_md)
            max_rounds = review_config.review.max_rounds
        except Exception as e:
            log.warning(f"[{sid}] Failed to load REVIEW.md config, using default max rounds: {e}")
            max_rounds = DEFAULT_MAX_REVIEW_ROUNDS

        rounds = self._rounds.get(sid, 0)
        if rounds >= max_rounds:
            log.info(f"[{sid}] Max review rounds ({max_rounds}) reached -- escalating")
            _update_status(session_dir, "waiting:human-review")
            self.telegram.notify(
                f"\u26a0\ufe0f `{sid}` hit max review rounds ({max_rounds}). "
                f"Escalating to human review.\n"
                f"`nightshift accept/reject/revise {issue_id}`")
            return

        _update_status(session_dir, "reviewing")
        review_sid = f"review-{sid}"
        self._recently_launched[review_sid] = time.time()
        self._rounds[sid] = rounds + 1

        log.info(f"[{sid}] Launching automated review (round {rounds + 1}/{max_rounds})")
        cmd = [
            sys.executable,
            str(_HOST_DIR / "launch.py"),
            issue_id,
            "--workflow", str(review_md),
            "--step", "review",
            "--coder-session", sid,
        ]
        self._launch_background(cmd, review_sid)

    def check_reviewer_done(self):
        """Check if reviewer sessions have finished, handle approve/revise verdict."""
        if not self.sessions_dir.exists():
            return

        for session_dir in self.sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if not sid.startswith("review-"):
                continue
            if not (session_dir / "state.json").exists():
                continue
            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read reviewer state: {e}")
                continue
            if state.get("status") != "waiting:review":
                continue

            issue_id = state.get("issue_id", "")
            coder_sid = sid[len("review-"):]
            coder_dir = self.sessions_dir / coder_sid
            if not coder_dir.exists():
                continue

            conv_log = session_dir / "conversation.jsonl"
            verdict = self.extract_reviewer_verdict(conv_log, issue_id)
            if not verdict:
                continue

            log.info(f"[{sid}] Reviewer verdict: {verdict}")
            if verdict == "approve":
                self.handle_reviewer_approve(coder_sid, coder_dir, issue_id)
            elif verdict == "revise":
                self.handle_reviewer_revise(coder_sid, coder_dir, issue_id, session_dir)

            self.cleanup_review_session(sid, session_dir)

    def extract_reviewer_verdict(self, conv_log: Path, issue_id: str) -> Optional[str]:
        """Extract @nightshift approve/revise from reviewer's conversation log."""
        if not conv_log.exists():
            return None

        for line in reversed(conv_log.read_text().strip().splitlines()):
            try:
                entry = json.loads(line)
                text = entry.get("content", "")
                cmd = parse_nightshift_command(text)
                if cmd in ("approve", "revise"):
                    return cmd
            except (json.JSONDecodeError, KeyError) as e:
                log.debug(f"Failed to parse conversation log line: {e}")
                continue

        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
            for comment in reversed(comments[-5:] if len(comments) > 5 else comments):
                cmd = parse_nightshift_command(comment.body)
                if cmd in ("approve", "revise"):
                    return cmd
        except Exception as e:
            log.warning(f"Tracker poll for reviewer verdict failed: {e}")

        return None

    def handle_reviewer_approve(self, coder_sid: str, coder_dir: Path, issue_id: str):
        """Reviewer approved -- transition coder to waiting:human-review."""
        try:
            _update_status(coder_dir, "waiting:human-review")
            log.info(f"[{coder_sid}] Reviewer approved -> waiting:human-review")

            self.telegram.notify(
                f"\u2705 Automated review *approved* `{coder_sid}`.\n"
                f"Human review: `nightshift accept/reject/revise {issue_id}`")

            try:
                tracker = self._get_tracker()
                tracker.add_comment(issue_id,
                    "\U0001f916 **Automated review: APPROVED**\n\n"
                    "Reviewer is satisfied with the changes. Awaiting human confirmation.\n\n"
                    f"Review with: `nightshift accept/reject/revise {issue_id}`")
                tracker.sync()
            except Exception as e:
                log.warning(f"[{coder_sid}] Failed to post approval to tracker: {e}")

        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer approve: {e}")

    def collect_reviewer_feedback(self, coder_sid: str, issue_id: str,
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
                except (json.JSONDecodeError, KeyError) as e:
                    log.debug(f"[{coder_sid}] Failed to parse reviewer feedback line: {e}")
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

    def handle_reviewer_revise(self, coder_sid: str, coder_dir: Path,
                               issue_id: str, review_dir: Path):
        """Reviewer requested revisions -- resume coder with feedback."""
        try:
            parts = self.collect_reviewer_feedback(coder_sid, issue_id, review_dir)
            feedback = build_revise_prompt([], inline_feedback="\n".join(parts))
            (coder_dir / "resume-prompt.md").write_text(feedback)

            _update_status(coder_dir, "working")
            self._recently_launched[coder_sid] = time.time()
            log.info(f"[{coder_sid}] Reviewer requested revisions -- resuming coder")
            self.telegram.notify(f"\U0001f504 Reviewer requested revisions for `{coder_sid}`. Coder resuming.")

            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue_id, "--resume",
            ]
            self._launch_background(cmd, coder_sid)
        except Exception as e:
            log.error(f"[{coder_sid}] Failed to handle reviewer revise: {e}")

    def cleanup_review_session(self, review_sid: str, review_dir: Path):
        """Clean up a reviewer session (worktree, branch, session dir)."""
        try:
            coder_sid = review_sid[len("review-"):]

            review_md = self.repo_dir / "REVIEW.md"
            config = load_workflow(review_md) if review_md.exists() else load_workflow(self.repo_dir / "WORKFLOW.md")

            wt = self.repo_dir / config.workspace.root / f"review-{coder_sid}"
            branch = f"review/{coder_sid}"

            _pkg().remove_worktree(self.repo_dir, wt, branch)

            _pkg().shutil.rmtree(review_dir, ignore_errors=True)

            self._recently_launched.pop(review_sid, None)
            self._comment_counts.pop(review_sid, None)

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
            try:
                state = read_state(session_dir)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[{sid}] Failed to read state for review check: {e}")
                continue
            if state.get("status") not in ("waiting:review", "waiting:human-review"):
                self._comment_counts.pop(sid, None)
                continue
            issue_id = state.get("issue_id", "")
            if not issue_id:
                continue

            if sid in tg_review_replies:
                tg_text, tg_author = tg_review_replies[sid]
                self.post_telegram_review_to_tracker(issue_id, tg_text, tg_author)

            self.poll_review_comments(sid, issue_id, session_dir)

    def poll_review_comments(self, sid: str, issue_id: str, session_dir: Path):
        """Check tracker for new @nightshift commands on a review session."""
        try:
            tracker = self._get_tracker()
            comments = tracker.get_comments(issue_id)
        except Exception as e:
            log.warning(f"[{sid}] Tracker poll failed: {e}")
            return

        last_count = self._comment_counts.get(sid, 0)
        if len(comments) <= last_count:
            return

        self._comment_counts[sid] = len(comments)

        if last_count == 0:
            if comments:
                cmd = parse_nightshift_command(comments[-1].body)
                if cmd:
                    log.info(f"[{sid}] Found pending @nightshift {cmd} from {comments[-1].author}")
                    self.handle_review_command(sid, issue_id, cmd, session_dir)
            return

        for comment in comments[last_count:]:
            cmd = parse_nightshift_command(comment.body)
            if cmd:
                log.info(f"[{sid}] Found @nightshift {cmd} from {comment.author}")
                self.handle_review_command(sid, issue_id, cmd, session_dir)
                break

    def post_telegram_review_to_tracker(self, issue_id: str, text: str, author: str):
        """Post Telegram review reply as a tracker comment for audit trail."""
        try:
            tracker = self._get_tracker()
            comment_body = f"Review from {author} via Telegram:\n\n{text}"
            tracker.add_comment(issue_id, comment_body)
            log.info(f"Posted Telegram review to tracker for {issue_id[:SHORT_ID_LEN]}")
        except Exception as e:
            log.warning(f"Failed to post Telegram review to tracker: {e}")

    def handle_review_command(self, sid: str, issue_id: str, cmd: str, session_dir: Path):
        """Execute a @nightshift command on a waiting:review session."""
        if sid in self._command_failures:
            last_time, attempts = self._command_failures[sid]
            backoff_s = min(COMMAND_BACKOFF_BASE_S * (2 ** attempts), COMMAND_BACKOFF_CAP_S)
            if time.time() - last_time < backoff_s:
                return  # still in cooldown

        if cmd == "revise":
            self.do_revise(sid, issue_id, session_dir)
        elif cmd == "accept":
            self.do_cli_command(sid, "accept", issue_id)
        elif cmd == "reject":
            self.do_cli_command(sid, "reject", issue_id)
        elif cmd == "approve":
            self.handle_reviewer_approve(sid, session_dir, issue_id)

    def do_revise(self, sid: str, issue_id: str, session_dir: Path):
        """Collect review feedback and relaunch agent."""
        try:
            tracker = self._get_tracker()
            review_comments = collect_review_feedback(tracker, issue_id, sync=False)

            if not review_comments:
                log.warning(f"[{sid}] No review feedback found -- skipping revise")
                return

            feedback = build_revise_prompt(review_comments)
            (session_dir / "resume-prompt.md").write_text(feedback)

            _update_status(session_dir, "working")

            self._comment_counts.pop(sid, None)
            self._recently_launched[sid] = time.time()

            log.info(f"[{sid}] Revising with {len(review_comments)} comment(s)")

            cmd = [
                sys.executable,
                str(_HOST_DIR / "launch.py"),
                issue_id, "--resume",
            ]
            self._launch_background(cmd, sid)

        except Exception as e:
            log.error(f"[{sid}] Revise failed: {e}")

    def do_cli_command(self, sid: str, command: str, issue_id: str):
        """Run a CLI command (accept/reject) as a subprocess."""
        try:
            log.info(f"[{sid}] Running nightshift {command}")
            self._comment_counts.pop(sid, None)
            result = _pkg().subprocess.run(
                [sys.executable, str(_HOST_DIR / "cli.py"), command, issue_id],
                cwd=str(self.repo_dir), capture_output=True, text=True,
            )
            if result.returncode != 0:
                self._handle_cli_failure(sid, command, result)
            else:
                self._handle_cli_success(sid, command, result)
        except Exception as e:
            log.error(f"[{sid}] {command} failed: {e}")

    def _handle_cli_failure(self, sid: str, command: str, result):
        """Handle a failed CLI command with backoff tracking."""
        error_msg = (result.stderr.strip() + "\n" + result.stdout.strip()).strip()
        _, attempts = self._command_failures.get(sid, (0, 0))
        attempts += 1
        self._command_failures[sid] = (time.time(), attempts)
        backoff_m = min(2 ** attempts, COMMAND_BACKOFF_CAP_CYCLES)
        log.error(f"[{sid}] nightshift {command} failed (attempt {attempts}, "
                  f"retry in {backoff_m}m): {error_msg}")
        self.telegram.notify(f"\u26a0\ufe0f `nightshift {command}` failed for `{sid}` "
                             f"(attempt {attempts}, retry in {backoff_m}m):\n\n{error_msg}")

    def _handle_cli_success(self, sid: str, command: str, result):
        """Handle a successful CLI command."""
        log.info(f"[{sid}] nightshift {command} completed")
        self.telegram.notify(f"\u2705 `nightshift {command}` completed for `{sid}`")
        self._command_failures.pop(sid, None)
        if result.stdout.strip():
            log.info(f"[{sid}] {result.stdout.strip()}")
