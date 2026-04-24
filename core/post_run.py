"""Post-run lifecycle: resume logic, done notification, checkpoint summarization.

Extracted from SessionRunner to keep session.py focused on the event loop.
"""

import json
import logging
import time
from pathlib import Path

from core.protocols import (
    AgentEventType, CodingAgent, IssueTracker, Notifier, NotificationLevel,
    TrackerIssue, Workspace, WorkspaceManager,
)
from core.constants import TITLE_TRUNCATE_LEN
from core.rebase import attempt_pre_review_rebase
from core.review import parse_nightshift_command
from core.state import StateManager

log = logging.getLogger(__name__)

CHECKPOINT_SUMMARIZE_THRESHOLD = 10
CHECKPOINT_SUMMARY_COUNT = 5
OVERLOAD_BACKOFF_DELAYS = [30, 60, 120, 240]  # seconds


def should_resume(state_mgr: StateManager, step: str) -> int | None:
    """Check if session should resume, returning backoff delay or None.

    For provider overload, returns the next backoff delay based on overload_resumes counter.
    Returns None if we've exhausted all retry attempts.
    """
    st = state_mgr.load_state()
    if st.status == "suspended:provider-overload":
        idx = st.overload_resumes - 1  # Counter already incremented
        if idx >= len(OVERLOAD_BACKOFF_DELAYS):
            return None  # Exhausted retries
        return OVERLOAD_BACKOFF_DELAYS[idx]
    return 0  # No delay for other statuses


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
    test_timeout_s: int = 120,
    is_review: bool = False,
) -> str | None:
    """Determine what to do after an agent cycle. Returns resume prompt or None."""
    st = state_mgr.load_state()

    if st.status == "suspended:answer-ready":
        return resume_with_answer(state_mgr, st)
    if st.status == "done:pending-review":
        # Skip pre-review rebase for review sessions — reviewers don't make code
        # changes and cannot fix rebase conflicts. Rebase is only for coder sessions.
        if not is_review:
            resume_prompt = attempt_pre_review_rebase(
                workspace_mgr, workspace, base_branch, test_command, test_timeout_s)
            if resume_prompt is not None:
                tracker.add_comment(issue.id, "🔄 Rebase needed — resuming agent to fix...")
                state_mgr.update_status("working")
                return resume_prompt
        notify_done(state_mgr, workspace_mgr, workspace, tracker, notifier, issue, st)
        return None
    if st.status in ("accepted", "rejected", "closed",
                      "suspended:auth-failure", "suspended:auth-failure-permanent"):
        return None
    if st.status == "cancelled:external":
        tracker.add_comment(issue.id, "🛑 Stopped: closed externally.")
        notifier.notify(f"🛑 {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — stopped.",
                        level=NotificationLevel.ACTIONS)
        return None
    if st.status in ("suspended:context-limit", "suspended:stall"):
        reason = st.status.split(":")[1]
        return prepare_resume(
            state_mgr, tracker, notifier, issue, agent, workspace,
            build_resume_fn, reason)
    if st.status == "suspended:provider-overload":
        delay = should_resume(state_mgr, "coder")
        if delay is None:
            # Max overload retries reached — don't auto-resume
            log.error("Provider overload retry limit reached. Stopping.")
            state_mgr.update_status("suspended:provider-overload-permanent")
            notifier.notify(
                f"⏳ {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]}: provider overload retry limit reached.",
                level=NotificationLevel.ACTIONS)
            return None
        return prepare_resume(
            state_mgr, tracker, notifier, issue, agent, workspace,
            build_resume_fn, "provider-overload", backoff_delay=delay)
    if st.status == "working" and is_review:
        commit_wip_fn("max-turns")
        return _handle_review_max_turns(
            state_mgr, workspace_mgr, workspace, tracker, notifier,
            issue, st, build_resume_fn)
    if st.status == "working":
        commit_wip_fn("max-turns")
        return prepare_resume(
            state_mgr, tracker, notifier, issue, agent, workspace,
            build_resume_fn, "max-turns")
    # Unexpected
    commit_wip_fn("unexpected exit")
    state_mgr.update_status("suspended:unexpected")
    build_resume_fn()
    notifier.notify(f"⚠️ {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — ended unexpectedly.",
                    level=NotificationLevel.ACTIONS)
    return None


def _handle_review_max_turns(
    state_mgr: StateManager,
    workspace_mgr: WorkspaceManager,
    workspace: Workspace | None,
    tracker: IssueTracker,
    notifier: Notifier,
    issue: TrackerIssue,
    st,
    build_resume_fn,
) -> None:
    """Handle a review session that hit max-turns without @@DONE@@.

    If a verdict (@nightshift approve/revise) was emitted in the conversation,
    treat as successful completion (waiting:review). Otherwise, fall back to
    suspended:review-no-verdict so the watcher escalates to human review.
    """
    verdict = scan_conversation_for_verdict(state_mgr)
    if verdict:
        log.info(f"Review hit max-turns but verdict '{verdict}' found — treating as done")
        notify_done(state_mgr, workspace_mgr, workspace, tracker, notifier, issue, st)
        return None
    log.warning("Review hit max-turns with no verdict — falling back to human review")
    state_mgr.update_status("suspended:review-no-verdict")
    build_resume_fn()
    notifier.notify(
        f"⚠️ {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]}"
        f" — review hit max-turns without verdict, needs human review.",
        level=NotificationLevel.ACTIONS)
    return None


def scan_conversation_for_verdict(state_mgr: StateManager) -> str | None:
    """Scan conversation.jsonl for a @nightshift approve/revise verdict."""
    conv_log = state_mgr.conversation_log
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
    return None


def format_token_count(n: int) -> str:
    """Format a token count as 'NK' for thousands or plain number for smaller values."""
    return f"{n / 1000:.0f}K" if n >= 1000 else str(n)


def format_cost_line(usage, resumes: int = 0) -> str:
    """Format a one-line cost summary from a UsageData dataclass instance."""
    input_t = getattr(usage, "input_tokens", 0) or 0
    output_t = getattr(usage, "output_tokens", 0) or 0
    cost = getattr(usage, "cost_usd", 0.0) or 0.0
    model = getattr(usage, "model", "") or ""
    if input_t == 0 and output_t == 0 and cost == 0.0:
        return ""
    in_k = format_token_count(input_t)
    out_k = format_token_count(output_t)
    parts = [f"{in_k} input / {out_k} output tokens, ${cost:.2f}"]
    detail_parts = []
    if model:
        detail_parts.append(model)
    if resumes:
        detail_parts.append(f"{resumes} resumes")
    if detail_parts:
        parts.append(f"({', '.join(detail_parts)})")
    return "**Cost:** " + " ".join(parts)


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
    state_mgr.mark_completed()
    # Re-read state to get latest usage (accumulated during session)
    current_state = state_mgr.load_state()
    diff = workspace_mgr.diff_stat(workspace.path) if workspace else "N/A"
    ticks = "```"

    summary_lines = [f"- {cp.description}" for cp in state.checkpoints]
    summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

    cost_line = format_cost_line(current_state.usage, resumes=current_state.step)
    cost_section = f"\n{cost_line}" if cost_line else ""

    proof = (
        f"🏁 **Work complete — awaiting review**\n\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Q&A exchanges:** {len(state.human_answers)}\n"
        f"**Changes:**\n{ticks}\n{diff}\n{ticks}"
        f"{cost_section}"
    )
    tracker.add_comment(issue.id, proof)
    tracker.add_label(issue.id, "needs-review")
    tracker.remove_label(issue.id, "agent-in-progress")
    notifier.notify(
        f"🏁 {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]} — done."
        f" nightshift accept/reject/revise {issue.identifier}",
        level=NotificationLevel.ACTIONS,
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
    backoff_delay: int | None = None,
) -> str:
    """Build resume prompt and notify. Returns the prompt for the loop.

    If backoff_delay is set (for provider overload), sleeps before resuming.
    """
    if backoff_delay:
        log.info(f"Backoff delay: sleeping {backoff_delay}s before resume...")
        time.sleep(backoff_delay)
    build_resume_fn()
    maybe_summarize_checkpoints(state_mgr, agent, workspace, build_resume_fn)
    tracker.add_comment(issue.id, f"🔄 {reason} — auto-resuming...")
    notifier.notify(f"{reason} for {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]}. Resuming.",
                    level=NotificationLevel.ALL)
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
