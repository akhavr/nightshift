"""Session runner — agent-agnostic, tracker-agnostic.

Works with any adapters implementing the protocols.
"""

import logging
import time
from pathlib import Path

from core.protocols import (
    CodingAgent, IssueTracker, Notifier, WorkspaceManager,
    AgentEventType, MarkerType, parse_marker, TrackerIssue, Workspace,
)
from core.state import StateManager

log = logging.getLogger(__name__)

BOT_PREFIXES = ("💭", "🤖", "❓", "📌", "⚠️", "✅", "⏸️", "🔄", "👤", "💬", "🛑")
RECONCILE_S = 60
ANSWER_POLL_S = 1
QUESTION_WAIT_TIMEOUT_S = 30  # OQ-4: fallback if @@WAITING@@ never arrives
MAX_RESUMES = 10  # prevent infinite context-limit loops


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
        self._question_sent_via_notifier = False

        # Merge policy from WORKFLOW.md (defaults if not provided)
        if merge_config is None:
            from core.config import MergeConfig
            merge_config = MergeConfig()
        self.merge_config = merge_config

        # Hooks from WORKFLOW.md
        self.hooks_config = hooks_config

    def run(self, workspace: Workspace | None = None):
        """Main entry. Loops on auto-resume (no recursion → no stack overflow).

        If *workspace* is provided, use it directly (e.g. container mode where
        the host already mounted the worktree). Otherwise delegate to
        workspace_mgr.create().
        """
        if workspace is not None:
            self._workspace = workspace
        else:
            self._workspace = self.workspace_mgr.create(self.issue)

        # Run after_create hook on new workspaces
        if self._workspace.is_new and self.hooks_config:
            self._run_hook(self.hooks_config.after_create, "after_create", fatal=True)

        prompt = self.prompt
        resume_count = 0
        while True:
            # Guard against infinite resume loops
            if resume_count >= MAX_RESUMES:
                log.error(f"Hit max resumes ({MAX_RESUMES}). Stopping.")
                self.state_mgr.update_status("suspended:max-resumes")
                self.notifier.notify(
                    f"⚠️ {self.issue.identifier} hit {MAX_RESUMES} resumes. Manual --resume needed.")
                break

            # Run before_run hook
            if self.hooks_config:
                if not self._run_hook(self.hooks_config.before_run, "before_run", fatal=True):
                    self.state_mgr.update_status("suspended:hook-failure")
                    self.notifier.notify(
                        f"⚠️ {self.issue.identifier}: before_run hook failed.")
                    break

            self.state_mgr.append_conversation("user", prompt)
            self.agent.start(prompt, self._workspace.path, self.max_turns)
            log.info(f"Agent started (pid={self.agent.pid})")
            try:
                self._event_loop()
            finally:
                self.agent.terminate()

            # Run after_run hook (best-effort)
            if self.hooks_config:
                self._run_hook(self.hooks_config.after_run, "after_run", fatal=False)

            # Check if we need to auto-resume or stop
            resume_prompt = self._post_run()
            if resume_prompt is None:
                break  # terminal state — exit the loop
            resume_count += 1
            prompt = resume_prompt  # auto-resume with new prompt

    def _run_hook(self, script: str | None, name: str, fatal: bool = False) -> bool:
        """Execute a hook script. Returns True on success."""
        if not script:
            return True
        if not self._workspace:
            return True
        log.info(f"Running {name} hook...")
        timeout = self.hooks_config.timeout_s if self.hooks_config else 60
        # Use workspace manager's hook runner if available, else subprocess
        if hasattr(self.workspace_mgr, "run_hook"):
            ok = self.workspace_mgr.run_hook(self._workspace.path, script, timeout)
        else:
            import subprocess
            try:
                subprocess.run(
                    ["sh", "-c", script], cwd=str(self._workspace.path),
                    timeout=timeout, check=True, capture_output=True,
                )
                ok = True
            except Exception as e:
                log.warning(f"{name} hook failed: {e}")
                ok = False
        if not ok and fatal:
            log.error(f"{name} hook failed (fatal)")
        return ok

    def _event_loop(self):
        last_reconcile = time.monotonic()
        question_time: float | None = None  # OQ-4: track when question was asked

        for event in self.agent.stream_events():
            self.state_mgr.append_raw(event.raw)

            if event.type == AgentEventType.TEXT:
                result = self._handle_text(event.content)
                if result == "STOP":
                    break
                if result == "QUESTION_ASKED":
                    question_time = time.monotonic()

            elif event.type == AgentEventType.TOOL_CALL:
                self.state_mgr.append_conversation("tool_call", event.content)

            elif event.type == AgentEventType.TOOL_RESULT:
                self.state_mgr.append_conversation("tool_result", event.content)

            elif event.type == AgentEventType.SYSTEM:
                if "context window" in event.content or "token limit" in event.content:
                    self._commit_wip("context limit")
                    self.state_mgr.update_status("suspended:context-limit")
                    self._build_resume()
                    break
                self.state_mgr.append_conversation("system", event.content)

            elif event.type == AgentEventType.STALL:
                log.warning(f"Stall: {event.content}")
                self._commit_wip("stalled")
                self.state_mgr.update_status("suspended:stall")
                break

            elif event.type == AgentEventType.PROCESS_EXIT:
                break

            # OQ-4: if @@QUESTION@@ was seen but @@WAITING@@ hasn't arrived
            if (question_time and self._pending_questions
                    and time.monotonic() - question_time > QUESTION_WAIT_TIMEOUT_S):
                log.warning("@@WAITING@@ not received — forcing wait")
                self._on_waiting()
                question_time = None

            # Reconciliation
            if time.monotonic() - last_reconcile > RECONCILE_S:
                last_reconcile = time.monotonic()
                if self._issue_is_terminal():
                    self.state_mgr.update_status("cancelled:external")
                    break

        # OQ-1: In -p mode, the agent exits after responding. If a question
        # was asked but @@WAITING@@ was never seen (or process exited before
        # the OQ-4 timer), handle pending questions now.
        if self._pending_questions:
            log.info("Agent exited with pending question(s). Collecting answer...")
            self._on_waiting()

    def _handle_text(self, text: str) -> str | None:
        # Record the full assistant text block so that commands like
        # @nightshift approve/revise are captured in conversation.jsonl
        # (not just marker-extracted content).
        self.state_mgr.append_conversation("assistant", text)

        # Extract multiline question content: everything between
        # @@QUESTION@@ and the next marker (@@WAITING@@, @@DONE@@, etc.)
        question_content = self._extract_question(text)

        for line in text.splitlines():
            marker = parse_marker(line)
            if not marker:
                continue

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
        # Find next marker
        markers = ("@@LOG@@", "@@CHECKPOINT@@", "@@WAITING@@", "@@DONE@@")
        end = len(after)
        for m in markers:
            pos = after.find(m)
            if pos != -1 and pos < end:
                end = pos
        return after[:end].strip()

    def _on_question(self, question: str):
        step = self.state_mgr.load_state().step
        self.state_mgr.update_status("waiting:question")
        self.tracker.add_comment(
            self.issue.id, f"❓ **Question (step {step}):** {question}")
        self.tracker.add_label(self.issue.id, "needs-human-input")
        self.state_mgr.append_conversation("question", question)

        self._question_sent_via_notifier = self.notifier.send_question(
            self.issue.id, question, self.issue.identifier)
        if not self._question_sent_via_notifier:
            self.notifier.notify(f"❓ [{self.issue.identifier}]: {question}")

        self._pending_questions.append(question)  # OQ-7: queue

    def _on_waiting(self):
        if not self._pending_questions:
            log.warning("@@WAITING@@ without pending question — ignoring")
            return

        question = self._pending_questions.pop(0)  # OQ-7: pop oldest
        self.state_mgr.signal_waiting(question)
        log.info("Waiting for answer. Container may be paused.")

        answer = self._collect_answer()

        self.state_mgr.clear_waiting()
        self.state_mgr.add_qa(question, answer)
        self.tracker.remove_label(self.issue.id, "needs-human-input")
        self.tracker.add_comment(self.issue.id, f"💬 Answer: {answer[:200]}")

        # OQ-1: In -p mode, the agent exits after responding. It will not
        # be alive by the time we collect the answer. The answer is saved
        # in state; _post_run() will restart with --resume and the answer
        # as the new prompt.
        if not self.agent.is_alive():
            log.info("Agent exited (expected in -p mode). Will restart with answer.")
            self.state_mgr.update_status("suspended:answer-ready")
            return

        self.agent.send_input(answer)
        self.state_mgr.update_status("working")
        log.info("Answer sent to agent stdin.")

    def _on_done(self):
        """Mark as done. Review/merge happens in _post_run after agent exits."""
        self._commit_wip(f"resolve {self.issue.identifier}")
        self.state_mgr.update_status("done:pending-review")

    def _notify_done(self, state):
        """Post proof-of-work summary and notify. Does NOT block."""
        self.state_mgr.update_status("waiting:review")
        diff = self.workspace_mgr.diff_stat(self._workspace.path) if self._workspace else "N/A"
        ticks = "```"

        # Build summary of work done from checkpoints
        summary_lines = []
        for cp in state.checkpoints:
            summary_lines.append(f"- {cp.description}")
        summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

        proof = (
            f"🏁 **Work complete — awaiting review**\n\n"
            f"**Summary:**\n{summary}\n\n"
            f"**Q&A exchanges:** {len(state.human_answers)}\n"
            f"**Changes:**\n{ticks}\n{diff}\n{ticks}"
        )
        self.tracker.add_comment(self.issue.id, proof)
        self.tracker.add_label(self.issue.id, "needs-review")
        self.tracker.remove_label(self.issue.id, "agent-in-progress")
        self.notifier.notify(
            f"🏁 {self.issue.identifier} done. nightshift accept/reject/revise {self.issue.identifier}"
        )

    def _collect_answer(self) -> str:
        last_count = len(self.tracker.get_comments(self.issue.id))
        gb_counter = 0
        while True:
            # Source 1: answer.txt from host watcher
            if a := self.state_mgr.check_answer():
                self.notifier.clear_pending(self.issue.id); return a
            # Source 2: notifier (Telegram)
            if a := self.notifier.check_answer(self.issue.id):
                return a
            # Source 3: tracker comments
            gb_counter += 1
            if gb_counter >= 30:
                gb_counter = 0; self.tracker.sync()
                comments = self.tracker.get_comments(self.issue.id)
                if len(comments) > last_count:
                    latest = comments[-1].body
                    if not any(latest.startswith(p) for p in BOT_PREFIXES):
                        self.notifier.clear_pending(self.issue.id); return latest
                    last_count = len(comments)
            time.sleep(ANSWER_POLL_S)  # container may be paused here

    def _post_run(self) -> str | None:
        """Returns resume prompt if auto-resuming, None if terminal.

        Called after agent is terminated — safe to do blocking I/O.
        """
        st = self.state_mgr.load_state()

        # OQ-1: Agent exited in -p mode, answer was collected. Restart
        # with the answer as the prompt. The agent uses --resume to
        # preserve the full conversation context.
        if st.status == "suspended:answer-ready":
            answer = st.human_answers[-1].answer if st.human_answers else ""
            self.state_mgr.update_status("working")
            self.state_mgr.append_conversation("human_answer_sent", answer)
            log.info("Restarting agent with answer via --resume")
            return answer

        if st.status == "done:pending-review":
            # Post proof-of-work and notify, then exit.
            # Review/merge is handled by the host (nightshift accept/reject).
            self._notify_done(st)
            return None

        if st.status in ("completed", "cancelled:review-rejected"):
            return None
        if st.status == "cancelled:external":
            self.tracker.add_comment(self.issue.id, "🛑 Stopped: closed externally.")
            self.notifier.notify(f"🛑 {self.issue.identifier} stopped.")
            return None
        if st.status in ("suspended:context-limit", "suspended:stall"):
            return self._prepare_resume(st.status.split(":")[1])
        if st.status == "working":
            self._commit_wip("max-turns")
            return self._prepare_resume("max-turns")
        # Unexpected
        self._commit_wip("unexpected exit")
        self.state_mgr.update_status("suspended:unexpected")
        self._build_resume()
        self.notifier.notify(f"⚠️ {self.issue.identifier} ended unexpectedly.")
        return None  # unexpected = manual --resume needed

    def _prepare_resume(self, reason: str) -> str:
        """Build resume prompt and notify. Returns the prompt for the loop."""
        self._build_resume()
        self._maybe_summarize_checkpoints()
        self.tracker.add_comment(self.issue.id, f"🔄 {reason} — auto-resuming...")
        self.notifier.notify(f"{reason} for {self.issue.identifier}. Resuming.")
        self.state_mgr.update_status("working")
        return self.state_mgr.read_resume_prompt()

    def _maybe_summarize_checkpoints(self):
        """Compress checkpoint history when it gets long (>10 entries)."""
        state = self.state_mgr.load_state()
        if len(state.checkpoints) <= 10:
            return
        cp_text = "\n".join(
            f"Step {c.step}: {c.description}" for c in state.checkpoints
        )
        try:
            self.agent.start(
                prompt=f"Summarize these checkpoints to 5 key decisions. "
                       f"Output only the summary:\n\n{cp_text}",
                workspace=self._workspace.path if self._workspace else Path("/tmp"),
                max_turns=1,
            )
            summary_parts = []
            for event in self.agent.stream_events():
                if event.type == AgentEventType.TEXT:
                    summary_parts.append(event.content)
                elif event.type in (AgentEventType.PROCESS_EXIT, AgentEventType.STALL):
                    break
            self.agent.terminate()

            summary = " ".join(summary_parts).strip()
            if summary:
                self._build_resume(checkpoint_summary=summary)
        except Exception as e:
            log.warning(f"Checkpoint summarization failed: {e}")

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
                f"checkpoint({step}): {desc[:60]} [{self.issue.identifier}]")
            return self.workspace_mgr.get_current_commit(self._workspace.path)
        return "none"

    def _build_resume(self, checkpoint_summary: str | None = None):
        from core.prompts import build_resume_prompt
        build_resume_prompt(
            self.issue.title, self.issue.body, "", self.state_mgr,
            checkpoint_summary=checkpoint_summary,
            diff_fn=lambda: self.workspace_mgr.diff_stat(
                self._workspace.path) if self._workspace else "N/A")
