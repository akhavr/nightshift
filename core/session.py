"""Session runner — agent-agnostic, tracker-agnostic.

Works with any adapters implementing the protocols.
"""

import logging
import time

from core.config import MergeConfig
from core.hooks import run_hook, DEFAULT_HOOK_TIMEOUT_S
from core.post_run import post_run_action
from core.prompts import build_resume_prompt
from core.protocols import (
    CodingAgent, IssueTracker, Notifier, WorkspaceManager,
    AgentEventType, MarkerType, parse_marker, TrackerIssue, Workspace,
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
        self._workspace: Workspace | None = None
        self._pending_questions: list[str] = []  # OQ-7: queue, not overwrite

        if merge_config is None:
            merge_config = MergeConfig()
        self.merge_config = merge_config
        self.hooks_config = hooks_config

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
                    f"⚠️ {self.issue.identifier}: before_run hook failed.")
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
                    f"⚠️ {self.issue.identifier} hit {MAX_RESUMES} resumes. Manual --resume needed.")
                break

            if not self._run_agent_cycle(prompt):
                break

            resume_prompt = self._post_run()
            if resume_prompt is None:
                break
            resume_count += 1
            prompt = resume_prompt

    # ── Event loop ────────────────────────────────────────────

    def _event_loop(self):
        last_reconcile = time.monotonic()
        question_time: float | None = None  # OQ-4: track when question was asked

        for event in self.agent.stream_events():
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
        if event.type == AgentEventType.TEXT:
            return self._handle_text(event.content)
        if event.type == AgentEventType.TOOL_CALL:
            self.state_mgr.append_conversation("tool_call", event.content)
        elif event.type == AgentEventType.TOOL_RESULT:
            self.state_mgr.append_conversation("tool_result", event.content)
        elif event.type == AgentEventType.SYSTEM:
            return self._handle_system_event(event.content)
        elif event.type == AgentEventType.STALL:
            log.warning(f"Stall: {event.content}")
            self._commit_wip("stalled")
            self.state_mgr.update_status("suspended:stall")
            return "STOP"
        elif event.type == AgentEventType.PROCESS_EXIT:
            return "STOP"
        return None

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
        """Check if issue was closed externally. Returns updated timestamp."""
        if time.monotonic() - last_reconcile > RECONCILE_S:
            if self._issue_is_terminal():
                self.state_mgr.update_status("cancelled:external")
            return time.monotonic()
        return last_reconcile

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
            step = self.state_mgr.increment_step()
            commit = self._commit_checkpoint(marker.content, step)
            self.state_mgr.add_checkpoint(marker.content, step, commit)
            self._build_resume()
            self.tracker.add_comment(
                self.issue.id, f"📌 Checkpoint {step}: {marker.content}")
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
            self._build_resume, self._commit_wip)

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

    def _build_resume(self, checkpoint_summary: str | None = None):
        build_resume_prompt(
            self.issue.title, self.issue.body, "", self.state_mgr,
            checkpoint_summary=checkpoint_summary,
            diff_fn=lambda: self.workspace_mgr.diff_stat(
                self._workspace.path) if self._workspace else "N/A")
