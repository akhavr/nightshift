"""Tests for core/post_run.py — post-run lifecycle functions."""

from pathlib import Path

from core.protocols import (
    AgentEvent, AgentEventType, RebaseResult, TrackerIssue, Workspace,
)
from core.state import StateManager, SessionState
from core.constants import TITLE_TRUNCATE_LEN
from core.post_run import (
    post_run_action, notify_done, resume_with_answer,
    prepare_resume, maybe_summarize_checkpoints,
)

from tests.conftest import (
    MockAgent, MockTracker, MockNotifier, MockWorkspaceManager, make_test_issue,
)
from tests.test_session_runner import ScriptedAgent, _text_event, _exit_event


def _setup(tmp_path, issue=None):
    """Create standard test fixtures."""
    issue = issue or make_test_issue()
    agent = MockAgent([])
    tracker = MockTracker({issue.id: issue})
    notifier = MockNotifier()
    ws_mgr = MockWorkspaceManager(tmp_path)
    session_dir = tmp_path / "session"
    sm = StateManager(session_dir)
    state = SessionState(issue_id=issue.id, branch=f"agent/{issue.identifier}", status="working")
    sm._write(state)
    ws = Workspace(path=tmp_path, branch="test")
    return agent, tracker, notifier, ws_mgr, sm, ws, issue


class TestPostRunAction:
    def test_done_pending_review(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result is None
        assert sm.load_state().status == "waiting:review"

    def test_answer_ready(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:answer-ready")
        sm.add_qa("What?", "this")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result == "this"
        assert sm.load_state().status == "working"

    def test_completed_returns_none(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("completed")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result is None

    def test_cancelled_external(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("cancelled:external")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result is None
        comments = tracker.get_comments(issue.id)
        assert any("closed externally" in c.body for c in comments)

    def test_context_limit_resumes(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:context-limit")
        # build_resume_fn writes a resume prompt file
        sm.write_resume_prompt("resume prompt")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result == "resume prompt"

    def test_working_triggers_max_turns(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.write_resume_prompt("resume")
        commits = []
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: commits.append(r))
        assert result == "resume"
        assert "max-turns" in commits

    def test_unexpected_status(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("some-weird-status")
        build_called = []
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: build_called.append(True), lambda r: None)
        assert result is None
        assert sm.load_state().status == "suspended:unexpected"
        assert any("unexpectedly" in n for n in notifier.notifications)

    def test_cancelled_external_includes_title(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("cancelled:external")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert any(issue.title in n for n in notifier.notifications)

    def test_unexpected_status_includes_title(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("some-weird-status")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert any(issue.title in n for n in notifier.notifications)

    def test_context_limit_resume_includes_title(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:context-limit")
        sm.write_resume_prompt("resume prompt")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert any(issue.title in n for n in notifier.notifications)

    def test_long_title_truncated_in_notification(self, tmp_path):
        long_title = "A" * 100
        issue = make_test_issue(title=long_title)
        agent, tracker, notifier, ws_mgr, sm, ws, _ = _setup(tmp_path, issue=issue)
        sm.update_status("cancelled:external")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        notification = notifier.notifications[-1]
        assert long_title[:TITLE_TRUNCATE_LEN] in notification
        assert long_title not in notification


class TestResumeWithAnswer:
    def test_returns_answer(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.add_qa("Q?", "A!")
        st = sm.load_state()
        result = resume_with_answer(sm, st)
        assert result == "A!"
        assert sm.load_state().status == "working"

    def test_empty_answers(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        st = sm.load_state()
        result = resume_with_answer(sm, st)
        assert result == ""


class TestNotifyDone:
    def test_posts_summary(self, tmp_path):
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.add_checkpoint("step one", 1, "abc1234")
        st = sm.load_state()
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments(issue.id)
        assert any("Work complete" in c.body for c in comments)
        assert any("step one" in c.body for c in comments)

    def test_no_workspace_uses_na(self, tmp_path):
        _, tracker, notifier, ws_mgr, sm, _, issue = _setup(tmp_path)
        st = sm.load_state()
        notify_done(sm, ws_mgr, None, tracker, notifier, issue, st)
        assert sm.load_state().status == "waiting:review"

    def test_done_notification_includes_title(self, tmp_path):
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        st = sm.load_state()
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        assert any(issue.title in n for n in notifier.notifications)


class TestMaybeSummarizeCheckpoints:
    def test_skips_short_list(self, tmp_path):
        agent, _, _, ws_mgr, sm, ws, _ = _setup(tmp_path)
        for i in range(5):
            sm.add_checkpoint(f"step {i}", i, f"commit{i}")
        maybe_summarize_checkpoints(sm, agent, ws, lambda **kw: None)
        assert not agent.started

    def test_triggers_on_long_list(self, tmp_path):
        summarize_agent = ScriptedAgent([
            [_text_event("Summary"), _exit_event()],
        ])
        _, _, _, ws_mgr, sm, ws, _ = _setup(tmp_path)
        for i in range(12):
            sm.add_checkpoint(f"step {i}", i, f"commit{i}")
        maybe_summarize_checkpoints(sm, summarize_agent, ws, lambda **kw: None)
        assert summarize_agent.started


class TestPostRunRebase:
    """Tests for pre-review rebase integration in post_run_action."""

    def test_successful_rebase_proceeds_to_review(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master")
        assert result is None
        assert sm.load_state().status == "waiting:review"
        assert len(ws_mgr.rebase_calls) == 1

    def test_rebase_conflict_resumes_agent(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        ws_mgr.rebase_result = RebaseResult(
            success=False, conflict_details="conflict in main.py")
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master")
        assert result is not None
        assert "REBASE CONFLICT" in result
        assert sm.load_state().status == "working"
        comments = tracker.get_comments(issue.id)
        assert any("Rebase needed" in c.body for c in comments)

    def test_rebase_passes_base_branch(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("done:pending-review")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="main")
        assert ws_mgr.rebase_calls[0][1] == "main"

    def test_rebase_with_test_failure_resumes(self, tmp_path):
        from unittest.mock import patch
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("done:pending-review")
        with patch("core.post_run.attempt_pre_review_rebase",
                    return_value="POST-REBASE TEST FAILURE: tests failed"):
            result = post_run_action(
                sm, ws_mgr, ws, tracker, notifier, issue, agent,
                lambda **kw: None, lambda r: None)
        assert result is not None
        assert "TEST FAILURE" in result
        assert sm.load_state().status == "working"

    def test_non_done_status_ignores_rebase(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:context-limit")
        sm.write_resume_prompt("resume prompt")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert len(ws_mgr.rebase_calls) == 0
