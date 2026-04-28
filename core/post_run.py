"""Post-run lifecycle: resume logic, done notification, checkpoint summarization.

Extracted from SessionRunner to keep session.py focused on the event loop.
"""

import json
import logging
import subprocess
from pathlib import Path

from core.protocols import (
    AgentEventType, CodingAgent, IssueTracker, Notifier, NotificationLevel,
    TrackerIssue, Workspace, WorkspaceManager,
)
from core.constants import TITLE_TRUNCATE_LEN
from core.review import parse_nightshift_command
from core.state import StateManager

log = logging.getLogger(__name__)

CHECKPOINT_SUMMARIZE_THRESHOLD = 10
CHECKPOINT_SUMMARY_COUNT = 5


def check_empty_session(repo: Path, branch: str, base: str) -> bool:
    """Return True when branch has no commits beyond base."""
    result = subprocess.run(
        ["git", "log", "--oneline", f"{base}..{branch}"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    if result.returncode != 0:
        log.warning(
            "Failed to check commits for %s against %s: %s",
            branch,
            base,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    return not result.stdout.strip()


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
        # Pre-review rebase now runs on host side (review_orchestrator) to avoid
        # bind-mount issues where git cannot unlink mounted files like WORKFLOW.md.
        # Container just transitions to waiting:review.
        notify_done(
            state_mgr, workspace_mgr, workspace, tracker, notifier, issue, st,
            base_branch=base_branch,
        )
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
        # Provider overload retries are handled by host-side watcher, not container.
        # Just return None to stop the session; watcher will resume with backoff.
        return None
    if st.status == "working" and is_review:
        commit_wip_fn("max-turns")
        return _handle_review_max_turns(
            state_mgr, workspace_mgr, workspace, tracker, notifier,
            issue, st, build_resume_fn, base_branch=base_branch)
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
    base_branch: str = "master",
) -> None:
    """Handle a review session that hit max-turns without @@DONE@@.

    If a verdict (@nightshift approve/revise) was emitted in the conversation,
    treat as successful completion (waiting:review). Otherwise, fall back to
    suspended:review-no-verdict so the watcher escalates to human review.
    """
    verdict = scan_conversation_for_verdict(state_mgr)
    if verdict:
        log.info(f"Review hit max-turns but verdict '{verdict}' found — treating as done")
        notify_done(
            state_mgr, workspace_mgr, workspace, tracker, notifier, issue, st,
            base_branch=base_branch,
        )
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
    base_branch: str = "master",
):
    """Post proof-of-work summary and notify."""
    state_mgr.mark_done("waiting:review")
    # Re-read state to get latest usage (accumulated during session)
    current_state = state_mgr.load_state()
    diff = workspace_mgr.diff_stat(workspace.path) if workspace else "N/A"
    ticks = "```"
    empty_session = (
        workspace is not None
        and check_empty_session(workspace.path, state.branch, base_branch)
    )
    if empty_session:
        state_mgr.update_status("waiting:human-review")

    summary_lines = [f"- {cp.description}" for cp in state.checkpoints]
    summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

    cost_line = format_cost_line(current_state.usage, resumes=current_state.step)
    cost_section = f"\n{cost_line}" if cost_line else ""

    heading = "🏁 **Work complete — awaiting review**"
    note = ""
    if empty_session:
        heading = "🏁 **Empty session — awaiting human review**"
        note = "\n⚠️ No commits detected. Did you forget to commit your changes?"
    proof = (
        f"{heading}\n\n"
        f"{note}\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Q&A exchanges:** {len(state.human_answers)}\n"
        f"**Changes:**\n{ticks}\n{diff}\n{ticks}"
        f"{cost_section}"
    )
    tracker.add_comment(issue.id, proof)
    tracker.add_label(issue.id, "needs-review")
    tracker.remove_label(issue.id, "agent-in-progress")
    notifier.notify(
        f"🏁 {issue.identifier} {issue.title[:TITLE_TRUNCATE_LEN]}"
        f"{' — empty session.' if empty_session else ' — done.'}"
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
) -> str:
    """Build resume prompt and notify. Returns the prompt for the loop."""
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
