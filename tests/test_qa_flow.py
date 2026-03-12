"""Tests for core/qa_flow.py — question/answer flow handling."""

from pathlib import Path
from unittest.mock import patch

from core.protocols import Workspace
from core.state import StateManager, SessionState
from core.qa_flow import handle_question, handle_waiting, deliver_answer

from tests.conftest import (
    MockAgent, MockTracker, MockNotifier, make_test_issue,
)


def _setup(tmp_path):
    issue = make_test_issue()
    agent = MockAgent([])
    agent.start("test", tmp_path, 50)  # mark as alive
    tracker = MockTracker({issue.id: issue})
    notifier = MockNotifier()
    session_dir = tmp_path / "session"
    sm = StateManager(session_dir)
    state = SessionState(issue_id=issue.id, branch="agent/test", status="working")
    sm._write(state)
    return agent, tracker, notifier, sm, issue


class TestHandleQuestion:
    def test_posts_comment_and_label(self, tmp_path):
        _, tracker, notifier, sm, issue = _setup(tmp_path)
        pending = []
        handle_question("What color?", sm, tracker, notifier, issue, pending)
        comments = tracker.get_comments(issue.id)
        assert any("What color?" in c.body for c in comments)
        assert "needs-human-input" in issue.labels
        assert len(pending) == 1
        assert pending[0] == "What color?"

    def test_sends_via_notifier(self, tmp_path):
        _, tracker, notifier, sm, issue = _setup(tmp_path)
        pending = []
        sent = handle_question("Q?", sm, tracker, notifier, issue, pending)
        assert sent is True
        assert len(notifier.questions) == 1

    def test_fallback_notify_when_send_fails(self, tmp_path):
        _, tracker, notifier, sm, issue = _setup(tmp_path)
        notifier.send_question = lambda *a, **kw: False
        pending = []
        sent = handle_question("Q?", sm, tracker, notifier, issue, pending)
        assert sent is False
        assert any("Q?" in n for n in notifier.notifications)


class TestHandleWaiting:
    def test_collects_answer_from_file(self, tmp_path):
        agent, tracker, notifier, sm, issue = _setup(tmp_path)
        pending = ["What color?"]
        sm.answer_file.write_text("blue")
        handle_waiting(sm, notifier, tracker, issue, agent, pending)
        st = sm.load_state()
        assert len(st.human_answers) == 1
        assert st.human_answers[0].answer == "blue"
        assert len(pending) == 0

    def test_ignores_when_no_pending(self, tmp_path):
        agent, tracker, notifier, sm, issue = _setup(tmp_path)
        pending = []
        handle_waiting(sm, notifier, tracker, issue, agent, pending)
        assert len(sm.load_state().human_answers) == 0

    def test_removes_label_after_answer(self, tmp_path):
        agent, tracker, notifier, sm, issue = _setup(tmp_path)
        issue.labels.append("needs-human-input")
        pending = ["Q?"]
        sm.answer_file.write_text("A!")
        handle_waiting(sm, notifier, tracker, issue, agent, pending)
        assert "needs-human-input" not in issue.labels


class TestDeliverAnswer:
    def test_sends_to_alive_agent(self, tmp_path):
        agent, _, _, sm, _ = _setup(tmp_path)
        deliver_answer("the answer", agent, sm)
        assert "the answer" in agent.inputs_sent
        assert sm.load_state().status == "working"

    def test_marks_answer_ready_when_agent_dead(self, tmp_path):
        agent, _, _, sm, _ = _setup(tmp_path)
        agent.terminate()  # mark as dead
        deliver_answer("the answer", agent, sm)
        assert sm.load_state().status == "suspended:answer-ready"
        assert len(agent.inputs_sent) == 0
