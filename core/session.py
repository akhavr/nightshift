"""Session runner — agent-agnostic, tracker-agnostic.

Works with any adapters implementing the protocols.
"""

import json
import logging
import time

from core.config import MergeConfig, PricingConfig, WorkspaceConfig
from core.constants import SIGNAL_DIR_NAME, TITLE_TRUNCATE_LEN, TOKEN_PRICING_UNIT
from core.hooks import run_hook, DEFAULT_HOOK_TIMEOUT_S
from core.post_run import post_run_action
from core.prompts import build_resume_prompt
from core.protocols import (
    CodingAgent, IssueTracker, Notifier, NotificationLevel, WorkspaceManager,
    AgentEventType, MarkerType, parse_marker, TrackerIssue, TrackerComment, Workspace,
)
from core.qa_flow import handle_question, handle_waiting
from core.state import StateManager

log = logging.getLogger(__name__)

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑")
RECONCILE_S = 60
QUESTION_WAIT_TIMEOUT_S = 30  # OQ-4: fallback if @@WAITING@@ never arrives
MAX_RESUMES = 10  # prevent infinite context-limit loops
COMMIT_DESC_MAX_LEN = 60     # max description length in checkpoint commit messages


class SessionRunner:
    def __init__(
        self, agent: CodingAgent, tracker: IssueTracker,
        notifier: Notifier, workspace_mgr: WorkspaceManager,
        state_mgr: StateManager, issue: TrackerIssue, prompt: str,
        max_turns: int = 50,
        terminal_statuses: tuple[str, ...] = ("closed",),
        merge_config: "MergeConfig | None" = None,
        hooks_config: "HooksConfig | None" = None,
        workspace_config: "WorkspaceConfig | None" = None,
        pricing: "PricingConfig | None" = None,
        is_review: bool = False,
    ):
        self.agent = agent
        self.tracker = tracker
        self.notifier = notifier
        self.workspace_mgr = workspace_mgr
        self.state_mgr = state_mgr
        self.issue = issue
        self.prompt = prompt
        self.max_turns = max_turns
        self.terminal_statuses = terminal_statuses
        self.pricing = pricing
        self.is_review = is_review
        self._workspace: Workspace | None = None
        self._pending_questions: list[str] = []  # OQ-7: queue, not overwrite
        self._new_comments: list[TrackerComment] = []  # accumulated from tracker reload

        if merge_config is None:
            merge_config = MergeConfig()
        self.merge_config = merge_config
        self.hooks_config = hooks_config

        if workspace_config is None:
            workspace_config = WorkspaceConfig()
        self.workspace_config = workspace_config

        # File-based signal fallback (Phase 3)
        self._signal_dir = self.state_mgr.session_dir / SIGNAL_DIR_NAME
        self._signal_dir.mkdir(exist_ok=True)

    def _init_workspace(self, workspace: Workspace | None):
        """Set up workspace and run after_create hook if new."""
        if workspace is not None:
            self._workspace = workspace
        else:
            self._workspace = self.workspace_mgr.create(self.issue)
        if self._workspace.is_new and self.hooks_config:
            self._run_hook(self.hooks_config.after_create, "after_create", fatal=True)

    def _run_hook(self, script: str | None, name: str, fatal: bool = False) -> bool:
        """Delegate hook execution to hooks module."""
        timeout = self.hooks_config.timeout_s if self.hooks_config else DEFAULT_HOOK_TIMEOUT_S
        ws_path = self._workspace.path if self._workspace else None
        return run_hook(self.workspace_mgr, ws_path, script, name, timeout, fatal)

    def _run_agent_cycle(self, prompt: str) -> bool:
        """Run one agent start->event-loop->terminate cycle. Returns False to stop."""
        if self.hooks_config:
            if not self._run_hook(self.hooks_config.before_run, "before_run", fatal=True):
                self.state_mgr.update_status("suspended:hook-failure")
                self.notifier.notify(
                    f"⚠️ {self.issue.identifier} {self.issue.title[:TITLE_TRUNCATE_LEN]}: before_run hook failed.",
                    level=NotificationLevel.ACTIONS)
                return False

        self.state_mgr.append_conversation("user", prompt)
        self.agent.start(prompt, self._workspace.path, self.max_turns)
        log.info(f"Agent started (pid={self.agent.pid})")
        try:
            self._event_loop()
        finally:
            self.agent.terminate()

        if self.hooks_config:
            self._run_hook(self.hooks_config.after_run, "after_run", fatal=False)
        return True

    def run(self, workspace: Workspace | None = None):
        """Main entry. Loops on auto-resume (no recursion)."""
        self._init_workspace(workspace)

        prompt = self.prompt
        resume_count = 0
        while True:
            if resume_count >= MAX_RESUMES:
                log.error(f"Hit max resumes ({MAX_RESUMES}). Stopping.")
                self.state_mgr.update_status("suspended:max-resumes")
                self.notifier.notify(
                    f"⚠️ {self.issue.identifier} {self.issue.title[:TITLE_TRUNCATE_LEN]} hit {MAX_RESUMES} resumes. Manual --resume needed.",
                    level=NotificationLevel.ACTIONS)
                break

            if not self._run_agent_cycle(prompt):
                break

            resume_prompt = self._post_run()
            if resume_prompt is None:
                break
            resume_count += 1
            prompt = self._inject_new_comments(resume_prompt)

    # ── Event loop ────────────────────────────────────────────

    def _event_loop(self):
        last_reconcile = time.monotonic()
        question_time: float | None = None  # OQ-4: track when question was asked

        for event in self.agent.stream_events():
            # Check file-based signals before processing agent output
            file_signal = self._check_file_signals()
            if file_signal is not None:
                result = self._handle_file_signal(file_signal)
                if result == "STOP":
                    break
                if result == "QUESTION_ASKED":
                    question_time = time.monotonic()

            self.state_mgr.append_raw(event.raw)

            result = self._dispatch_event(event)
            if result == "STOP":
                break
            if result == "QUESTION_ASKED":
                question_time = time.monotonic()

            # OQ-4: if @@QUESTION@@ was seen but @@WAITING@@ hasn't arrived
            if self._should_force_waiting(question_time):
                log.warning("@@WAITING@@ not received — forcing wait")
                self._on_waiting()
                question_time = None

            last_reconcile = self._maybe_reconcile(last_reconcile)

        # OQ-1: handle pending questions after agent exits
        if self._pending_questions:
            log.info("Agent exited with pending question(s). Collecting answer...")
            self._on_waiting()

    def _dispatch_event(self, event) -> str | None:
        """Dispatch a single agent event. Returns 'STOP' to break the loop."""
        self._maybe_record_usage(event)
        if event.type == AgentEventType.TEXT:
            return self._handle_text(event.content)
        if event.type == AgentEventType.TOOL_CALL:
            self.state_mgr.append_conversation("tool_call", event.content)
        elif event.type == AgentEventType.TOOL_RESULT:
            self.state_mgr.append_conversation("tool_result", event.content)
        elif event.type == AgentEventType.SYSTEM:
            return self._handle_system_event(event.content)
        elif event.type == AgentEventType.AUTH_FAILURE:
            log.error(f"Auth failure: {event.content}")
            self._commit_wip("auth failure")
            self.state_mgr.update_status("suspended:auth-failure")
            self.notifier.notify(
                f"🔑 {self.issue.identifier} {self.issue.title[:TITLE_TRUNCATE_LEN]}: auth failure — token may be expired. Refresh and resume.",
                level=NotificationLevel.ACTIONS)
            return "STOP"
        elif event.type == AgentEventType.PROVIDER_OVERLOAD:
            log.warning(f"Provider overload: {event.content}")
            self._commit_wip("provider overload")
            self.state_mgr.increment_overload_resumes()
            self.state_mgr.update_status("suspended:provider-overload")
            self.notifier.notify(
                f"⏳ {self.issue.identifier} {self.issue.title[:TITLE_TRUNCATE_LEN]}: provider overload — will retry with backoff.",
                level=NotificationLevel.ACTIONS)
            return "STOP"
        elif event.type == AgentEventType.STALL:
            log.warning(f"Stall: {event.content}")
            self._commit_wip("stalled")
            self.state_mgr.update_status("suspended:stall")
            return "STOP"
        elif event.type == AgentEventType.PROCESS_EXIT:
            return "STOP"
        return None

    def _maybe_record_usage(self, event):
        """If the event carries usage metadata, accumulate it in state."""
        usage = event.metadata.get("usage")
        if usage:
            cost_usd = usage.get("cost_usd", 0.0)
            if self.pricing and cost_usd == 0:
                cost_usd = (
                    usage.get("input_tokens", 0) * self.pricing.input_per_1m
                    + usage.get("output_tokens", 0) * self.pricing.output_per_1m
                ) / TOKEN_PRICING_UNIT
            self.state_mgr.update_usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_usd=cost_usd,
                model=usage.get("model", ""),
            )

    def _handle_system_event(self, content: str) -> str | None:
        """Handle a system event (context limit, etc.)."""
        if "context window" in content or "token limit" in content:
            self._commit_wip("context limit")
            self.state_mgr.update_status("suspended:context-limit")
            self._build_resume()
            return "STOP"
        self.state_mgr.append_conversation("system", content)
        return None

    def _should_force_waiting(self, question_time: float | None) -> bool:
        """Check if we should force a @@WAITING@@ due to timeout."""
        return (question_time is not None
                and bool(self._pending_questions)
                and time.monotonic() - question_time > QUESTION_WAIT_TIMEOUT_S)

    def _maybe_reconcile(self, last_reconcile: float) -> float:
        """Check if issue was closed externally and reload tracker. Returns updated timestamp."""
        if time.monotonic() - last_reconcile > RECONCILE_S:
            self._try_reload_tracker()
            if self._issue_is_terminal():
                self.state_mgr.update_status("cancelled:external")
            return time.monotonic()
        return last_reconcile

    def _try_reload_tracker(self):
        """Reload tracker data if supported, accumulating new comments."""
        if not hasattr(self.tracker, "reload"):
            return
        try:
            new_comments = self.tracker.reload()
            if new_comments:
                self._new_comments.extend(new_comments)
                log.info(f"Accumulated {len(new_comments)} new comment(s) from tracker reload")
        except Exception as e:
            log.warning(f"Tracker reload failed: {e}")

    # ── File-based signal fallback ─────────────────────────────

    def _check_file_signals(self) -> str | tuple[str, str] | None:
        """Poll /session/signal/ for file-based signals (fallback for non-MCP agents).

        Returns "DONE", ("CHECKPOINT", desc), ("QUESTION", text), or None.
        Detected signal files are unlinked to prevent re-triggering.
        """
        done_file = self._signal_dir / "done"
        if done_file.exists():
            done_file.unlink()
            return "DONE"

        question_file = self._signal_dir / "question.json"
        if question_file.exists():
            try:
                data = json.loads(question_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.error(f"Failed to read question signal file: {e}")
                data = {}
            question_file.unlink(missing_ok=True)
            return ("QUESTION", data.get("question", ""))

        checkpoint_file = self._signal_dir / "checkpoint"
        if checkpoint_file.exists():
            try:
                desc = checkpoint_file.read_text().strip()
            except OSError as e:
                log.error(f"Failed to read checkpoint signal file: {e}")
                desc = ""
            checkpoint_file.unlink(missing_ok=True)
            return ("CHECKPOINT", desc)

        return None

    def _handle_file_signal(self, signal) -> str | None:
        """Process a file signal the same way as the corresponding @@MARKER@@."""
        if signal == "DONE":
            self._on_done()
            return "STOP"
        if isinstance(signal, tuple):
            kind, content = signal
            if kind == "CHECKPOINT":
                self._on_checkpoint(content)
            elif kind == "QUESTION":
                self._on_question(content)
                return "QUESTION_ASKED"
        return None

    # ── Text / marker handling ────────────────────────────────

    def _handle_text(self, text: str) -> str | None:
        self.state_mgr.append_conversation("assistant", text)
        question_content = self._extract_question(text)

        for line in text.splitlines():
            marker = parse_marker(line)
            if not marker:
                continue
            result = self._handle_marker(marker, question_content)
            if result is not None:
                return result
        return None

    def _handle_marker(self, marker, question_content: str | None) -> str | None:
        """Process a single marker from text. Returns signal or None."""
        if marker.type == MarkerType.LOG:
            self.tracker.add_comment(self.issue.id, f"💭 {marker.content}")
            self.state_mgr.append_conversation("thought", marker.content)
        elif marker.type == MarkerType.CHECKPOINT:
            self._on_checkpoint(marker.content)
        elif marker.type == MarkerType.QUESTION:
            self._on_question(question_content or marker.content)
            return "QUESTION_ASKED"
        elif marker.type == MarkerType.WAITING:
            self._on_waiting()
        elif marker.type == MarkerType.DONE:
            self._on_done()
            return "STOP"
        return None

    @staticmethod
    def _extract_question(text: str) -> str | None:
        """Extract multiline content between @@QUESTION@@ and the next marker."""
        q_token = "@@QUESTION@@"
        idx = text.find(q_token)
        if idx == -1:
            return None
        after = text[idx + len(q_token):]
        markers = ("@@LOG@@", "@@CHECKPOINT@@", "@@WAITING@@", "@@DONE@@")
        end = len(after)
        for m in markers:
            pos = after.find(m)
            if pos != -1 and pos < end:
                end = pos
        return after[:end].strip()

    # ── Q&A delegation ────────────────────────────────────────

    def _on_question(self, question: str):
        handle_question(
            question, self.state_mgr, self.tracker, self.notifier,
            self.issue, self._pending_questions)

    def _on_waiting(self):
        handle_waiting(
            self.state_mgr, self.notifier, self.tracker,
            self.issue, self.agent, self._pending_questions)

    def _on_checkpoint(self, content: str):
        """Handle checkpoint: increment step, commit, save state, notify."""
        step = self.state_mgr.increment_step()
        commit = self._commit_checkpoint(content, step)
        self.state_mgr.add_checkpoint(content, step, commit)
        self.state_mgr.reset_overload_resumes()  # Progress made, reset backoff
        self._build_resume()
        self.tracker.add_comment(
            self.issue.id, f"📌 Checkpoint {step}: {content}")

    def _on_done(self):
        """Mark as done. Review/merge happens in _post_run after agent exits."""
        self._commit_wip(f"resolve {self.issue.identifier}")
        self.state_mgr.update_status("done:pending-review")

    # ── Post-run delegation ───────────────────────────────────

    def _post_run(self) -> str | None:
        """Returns resume prompt if auto-resuming, None if terminal."""
        return post_run_action(
            self.state_mgr, self.workspace_mgr, self._workspace,
            self.tracker, self.notifier, self.issue, self.agent,
            self._build_resume, self._commit_wip,
            base_branch=self.workspace_config.base_branch,
            test_command=self.workspace_config.test_command,
            test_timeout_s=self.workspace_config.test_timeout_s,
            is_review=self.is_review)

    # ── Workspace helpers ─────────────────────────────────────

    def _issue_is_terminal(self) -> bool:
        self.tracker.sync()
        issue = self.tracker.get_issue(self.issue.id)
        return issue is not None and issue.status in self.terminal_statuses

    def _commit_wip(self, reason: str):
        if self._workspace:
            step = self.state_mgr.load_state().step
            self.workspace_mgr.commit(
                self._workspace.path,
                f"wip: {reason} at step {step} [{self.issue.identifier}]")

    def _commit_checkpoint(self, desc: str, step: int) -> str:
        if self._workspace:
            self.workspace_mgr.commit(
                self._workspace.path,
                f"checkpoint({step}): {desc[:COMMIT_DESC_MAX_LEN]} [{self.issue.identifier}]")
            return self.workspace_mgr.get_current_commit(self._workspace.path)
        return "none"

    def _inject_new_comments(self, prompt: str) -> str:
        """Prepend accumulated new comments to a resume prompt, then clear."""
        if not self._new_comments:
            return prompt
        header = "## New comments on this issue (added since your last run)\n"
        lines = []
        for c in self._new_comments:
            author = c.author or "unknown"
            lines.append(f"- **{author}**: {c.body}")
        comment_block = header + "\n".join(lines) + "\n\n"
        self._new_comments.clear()
        return comment_block + prompt

    def _build_resume(self, checkpoint_summary: str | None = None):
        build_resume_prompt(
            self.issue.title, self.issue.body, "", self.state_mgr,
            checkpoint_summary=checkpoint_summary,
            diff_fn=lambda: self.workspace_mgr.diff_stat(
                self._workspace.path) if self._workspace else "N/A")
