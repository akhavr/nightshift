"""Post-run lifecycle: resume logic, done notification, checkpoint summarization.

Extracted from SessionRunner to keep session.py focused on the event loop.
"""

import logging
from pathlib import Path

from core.protocols import (
    AgentEventType, CodingAgent, IssueTracker, Notifier,
    TrackerIssue, Workspace, WorkspaceManager,
)
from core.constants import TITLE_TRUNCATE_LEN
from core.rebase import attempt_pre_review_rebase
from core.state import StateManager

log = logging.getLogger(__name__)

CHECKPOINT_SUMMARIZE_THRESHOLD = 10
CHECKPOINT_SUMMARY_COUNT = 5


def post_run_action(
    state_mgr: StateManager,
    workspace_mgr: WorkspaceManager,
    workspace: Workspace | None,
    tracker: IssueTracker,
    notifier: Notifier,
    issue: TrackerIssue,
    agent: CodingAgent,
    build_resume_fn,
    commit_wip_fn,
    base_branch: str = "master",
    test_command: str | None = None,
) -> str | None:
    """Determine what to do after an agent cycle. Returns resume prompt or None."""
    st = state_mgr.load_state()

    if st.status == "suspended:answer-ready":
        return resume_with_answer(state_mgr, st)
    if st.status == "done:pending-review":
        resume_prompt = attempt_pre_review_rebase(
            workspace_mgr, workspace, base_branch, test_command)
        if resume_prompt is not None:
            tracker.add_comment(issue.id, "🔄 Rebase needed — resuming agent to fix...")
            state_mgr.update_status("working")
            return resume_prompt
        notify_done(state_mgr, workspace_mgr, workspace, tracker, notifier, issue, st)
        return None
    if st.status in ("completed", "cancelled:review-rejected"):
        return None
    if st.status == "cancelled:external":
        tracker.add_comment(issue.id, "🛑 Stopped: closed externally.")
        notifier.notify(f"🛑 {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — stopped.")
        return None
    if st.status in ("suspended:context-limit", "suspended:stall"):
        reason = st.status.split(":")[1]
        return prepare_resume(
            state_mgr, tracker, notifier, issue, agent, workspace,
            build_resume_fn, reason)
    if st.status == "working":
        commit_wip_fn("max-turns")
        return prepare_resume(
            state_mgr, tracker, notifier, issue, agent, workspace,
            build_resume_fn, "max-turns")
    # Unexpected
    commit_wip_fn("unexpected exit")
    state_mgr.update_status("suspended:unexpected")
    build_resume_fn()
    notifier.notify(f"⚠️ {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — ended unexpectedly.")
    return None


def notify_done(
    state_mgr: StateManager,
    workspace_mgr: WorkspaceManager,
    workspace: Workspace | None,
    tracker: IssueTracker,
    notifier: Notifier,
    issue: TrackerIssue,
    state,
):
    """Post proof-of-work summary and notify."""
    state_mgr.update_status("waiting:review")
    diff = workspace_mgr.diff_stat(workspace.path) if workspace else "N/A"
    ticks = "```"

    summary_lines = [f"- {cp.description}" for cp in state.checkpoints]
    summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

    proof = (
        f"🏁 **Work complete — awaiting review**\n\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Q&A exchanges:** {len(state.human_answers)}\n"
        f"**Changes:**\n{ticks}\n{diff}\n{ticks}"
    )
    tracker.add_comment(issue.id, proof)
    tracker.add_label(issue.id, "needs-review")
    tracker.remove_label(issue.id, "agent-in-progress")
    notifier.notify(
        f"🏁 {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — done."
        f" nightshift accept/reject/revise {issue.identifier}"
    )


def resume_with_answer(state_mgr: StateManager, st) -> str:
    """Restart agent with collected answer via --resume."""
    answer = st.human_answers[-1].answer if st.human_answers else ""
    state_mgr.update_status("working")
    state_mgr.append_conversation("human_answer_sent", answer)
    log.info("Restarting agent with answer via --resume")
    return answer


def prepare_resume(
    state_mgr: StateManager,
    tracker: IssueTracker,
    notifier: Notifier,
    issue: TrackerIssue,
    agent: CodingAgent,
    workspace: Workspace | None,
    build_resume_fn,
    reason: str,
) -> str:
    """Build resume prompt and notify. Returns the prompt for the loop."""
    build_resume_fn()
    maybe_summarize_checkpoints(state_mgr, agent, workspace, build_resume_fn)
    tracker.add_comment(issue.id, f"🔄 {reason} — auto-resuming...")
    notifier.notify(f"{reason} for {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]}. Resuming.")
    state_mgr.update_status("working")
    return state_mgr.read_resume_prompt()


def maybe_summarize_checkpoints(
    state_mgr: StateManager,
    agent: CodingAgent,
    workspace: Workspace | None,
    build_resume_fn,
):
    """Compress checkpoint history when it gets long."""
    state = state_mgr.load_state()
    if len(state.checkpoints) <= CHECKPOINT_SUMMARIZE_THRESHOLD:
        return
    cp_text = "\n".join(
        f"Step {c.step}: {c.description}" for c in state.checkpoints
    )
    try:
        _run_summarizer(agent, workspace, cp_text, build_resume_fn)
    except Exception as e:
        log.warning(f"Checkpoint summarization failed: {e}")


def _run_summarizer(
    agent: CodingAgent,
    workspace: Workspace | None,
    cp_text: str,
    build_resume_fn,
):
    """Use agent to summarize checkpoints, updating resume prompt."""
    agent.start(
        prompt=f"Summarize these checkpoints to {CHECKPOINT_SUMMARY_COUNT} key decisions. "
               f"Output only the summary:\n\n{cp_text}",
        workspace=workspace.path if workspace else Path("/tmp"),
        max_turns=1,
    )
    summary_parts = []
    for event in agent.stream_events():
        if event.type == AgentEventType.TEXT:
            summary_parts.append(event.content)
        elif event.type in (AgentEventType.PROCESS_EXIT, AgentEventType.STALL):
            break
    agent.terminate()

    summary = " ".join(summary_parts).strip()
    if summary:
        build_resume_fn(checkpoint_summary=summary)
