"""Question/answer flow handling for agent sessions.

Manages the lifecycle of questions asked by the agent:
question raised -> notifier/tracker updated -> answer collected -> delivered.
"""

import logging

from core.answer_collector import collect_answer, ANSWER_PREVIEW_LEN
from core.protocols import CodingAgent, IssueTracker, Notifier, TrackerIssue
from core.state import StateManager

log = logging.getLogger(__name__)


def handle_question(
    question: str,
    state_mgr: StateManager,
    tracker: IssueTracker,
    notifier: Notifier,
    issue: TrackerIssue,
    pending_questions: list[str],
) -> bool:
    """Process a @@QUESTION@@ marker. Returns whether notifier sent the question."""
    step = state_mgr.load_state().step
    state_mgr.update_status("waiting:question")
    tracker.add_comment(
        issue.id, f"❓ **Question (step {step}):** {question}")
    tracker.add_label(issue.id, "needs-human-input")
    state_mgr.append_conversation("question", question)

    sent_via_notifier = notifier.send_question(
        issue.id, question, issue.identifier)
    if not sent_via_notifier:
        notifier.notify(f"❓ [{issue.identifier}]: {question}")

    pending_questions.append(question)
    return sent_via_notifier


def handle_waiting(
    state_mgr: StateManager,
    notifier: Notifier,
    tracker: IssueTracker,
    issue: TrackerIssue,
    agent: CodingAgent,
    pending_questions: list[str],
):
    """Process a @@WAITING@@ marker: collect answer and deliver it."""
    if not pending_questions:
        log.warning("@@WAITING@@ without pending question — ignoring")
        return

    question = pending_questions.pop(0)
    state_mgr.signal_waiting(question)
    log.info("Waiting for answer. Container may be paused.")

    answer = collect_answer(state_mgr, notifier, tracker, issue.id)

    state_mgr.clear_waiting()
    state_mgr.add_qa(question, answer)
    tracker.remove_label(issue.id, "needs-human-input")
    tracker.add_comment(issue.id, f"💬 Answer: {answer[:ANSWER_PREVIEW_LEN]}")

    deliver_answer(answer, agent, state_mgr)


def deliver_answer(answer: str, agent: CodingAgent, state_mgr: StateManager):
    """Send answer to agent or mark for restart if agent exited."""
    if not agent.is_alive():
        log.info("Agent exited (expected in -p mode). Will restart with answer.")
        state_mgr.update_status("suspended:answer-ready")
        return
    agent.send_input(answer)
    state_mgr.update_status("working")
    log.info("Answer sent to agent stdin.")
