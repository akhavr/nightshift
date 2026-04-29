"""Tests for core/post_run.py — post-run lifecycle functions."""

import json
from pathlib import Path
from unittest.mock import patch

from core.protocols import (
    AgentEvent, AgentEventType, RebaseResult, TrackerIssue, Workspace,
)
from core.state import StateManager, SessionState
from core.constants import TITLE_TRUNCATE_LEN
from core.post_run import (
    post_run_action, notify_done, resume_with_answer,
    prepare_resume, maybe_summarize_checkpoints,
    scan_conversation_for_verdict, check_empty_session,
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
        sm.update_status("waiting:review")
        sm.update_status("accepted")
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

    def test_auth_failure_returns_none(self, tmp_path):
        """Auth failure is terminal — no auto-resume."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:auth-failure")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result is None
        assert sm.load_state().status == "suspended:auth-failure"

    def test_auth_failure_permanent_returns_none(self, tmp_path):
        """Permanent auth failure is also terminal."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:auth-failure-permanent")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert result is None
        assert sm.load_state().status == "suspended:auth-failure-permanent"

    def test_unexpected_status(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        # Use a valid but unhandled state to test the fallback branch
        sm.update_status("suspended:hook-failure")
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
        # Use a valid but unhandled state to test the fallback branch
        sm.update_status("suspended:hook-failure")
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

    def test_sets_completed_at(self, tmp_path):
        """notify_done must set completed_at to prevent orphan misclassification."""
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        st = sm.load_state()
        assert st.completed_at == ""
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        st = sm.load_state()
        assert st.completed_at != ""
        assert st.status == "waiting:review"

    def test_uses_atomic_mark_done(self, tmp_path, monkeypatch):
        """notify_done must use mark_done for atomic status+completed_at update."""
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        st = sm.load_state()
        calls = []
        original_mark_done = sm.mark_done
        def tracking_mark_done(status):
            calls.append(("mark_done", status))
            return original_mark_done(status)
        monkeypatch.setattr(sm, "mark_done", tracking_mark_done)
        monkeypatch.setattr(sm, "update_status", lambda s: calls.append(("update_status", s)))
        monkeypatch.setattr(sm, "mark_completed", lambda: calls.append(("mark_completed",)))
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        assert ("mark_done", "waiting:review") in calls
        assert ("update_status", "waiting:review") not in calls
        assert ("mark_completed",) not in calls

    def test_empty_session_flags_human_review(self, tmp_path):
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        st = sm.load_state()
        with patch("core.post_run.check_empty_session", return_value=True):
            notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        assert sm.load_state().status == "waiting:human-review"
        comments = tracker.get_comments(issue.id)
        assert any("Empty session" in c.body for c in comments)
        assert any("empty session" in n.lower() for n in notifier.notifications)


class TestUsageInNotifyDone:
    def test_cost_line_in_proof_comment(self, tmp_path):
        """notify_done() should include cost line when usage data exists."""
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_usage(45000, 12000, 0.38, "claude-sonnet-4-6")
        st = sm.load_state()
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments(issue.id)
        assert any("Cost:" in c.body for c in comments)
        assert any("45K input" in c.body for c in comments)
        assert any("$0.38" in c.body for c in comments)

    def test_cost_line_includes_resumes(self, tmp_path):
        """Cost line includes resume count when step > 0."""
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_usage(45000, 12000, 0.38, "claude-sonnet-4-6")
        # Simulate 3 resumes
        for _ in range(3):
            sm.increment_step()
        st = sm.load_state()
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments(issue.id)
        assert any("3 resumes" in c.body for c in comments)

    def test_no_cost_line_when_zero_usage(self, tmp_path):
        """No cost line when usage is all zeros."""
        _, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        st = sm.load_state()
        notify_done(sm, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments(issue.id)
        assert not any("Cost:" in c.body for c in comments)


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
    """Tests for pre-review rebase behavior in post_run_action.

    Pre-review rebase now runs on the HOST side (review_orchestrator) to avoid
    bind-mount issues where git cannot unlink mounted files like WORKFLOW.md.
    The container-side post_run_action should NOT call rebase.
    """

    def test_done_pending_review_transitions_without_rebase(self, tmp_path):
        """Container should transition to waiting:review without calling rebase."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master")
        assert result is None
        assert sm.load_state().status == "waiting:review"
        # Rebase should NOT be called from container side
        assert len(ws_mgr.rebase_calls) == 0

    def test_rebase_not_called_in_container(self, tmp_path):
        """Verify rebase is not called from container regardless of config."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        # Even with rebase result configured to fail, it should not be called
        ws_mgr.rebase_result = RebaseResult(
            success=False, conflict_details="conflict in main.py")
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master")
        # Should proceed to waiting:review without trying rebase
        assert result is None
        assert sm.load_state().status == "waiting:review"
        assert len(ws_mgr.rebase_calls) == 0

    def test_non_done_status_ignores_rebase(self, tmp_path):
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("suspended:context-limit")
        sm.write_resume_prompt("resume prompt")
        post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None)
        assert len(ws_mgr.rebase_calls) == 0


class TestScanConversationForVerdict:
    def test_finds_approve(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "All tests pass. @nightshift approve")
        assert scan_conversation_for_verdict(sm) == "approve"

    def test_finds_revise(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "Fix error handling. @nightshift revise")
        assert scan_conversation_for_verdict(sm) == "revise"

    def test_returns_none_without_verdict(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "Running tests...")
        assert scan_conversation_for_verdict(sm) is None

    def test_returns_none_no_log(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        assert scan_conversation_for_verdict(sm) is None

    def test_ignores_code_blocks(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "```\n@nightshift approve\n```")
        assert scan_conversation_for_verdict(sm) is None

    def test_finds_latest_verdict(self, tmp_path):
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "@nightshift revise")
        sm.append_conversation("assistant", "Fixed. @nightshift approve")
        assert scan_conversation_for_verdict(sm) == "approve"

    def test_finds_bold_verdict(self, tmp_path):
        """Flexible verdict parsing: recognizes **APPROVE** format."""
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "The code looks good.\n\n**APPROVE**")
        assert scan_conversation_for_verdict(sm) == "approve"

    def test_finds_bold_reject(self, tmp_path):
        """Flexible verdict parsing: recognizes **REJECT** format."""
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "Issues found:\n- Missing tests\n\n**REJECT**")
        assert scan_conversation_for_verdict(sm) == "reject"

    def test_finds_heading_verdict(self, tmp_path):
        """Flexible verdict parsing: recognizes Verdict: APPROVE format."""
        _, _, _, _, sm, _, _ = _setup(tmp_path)
        sm.append_conversation("assistant", "All tests pass.\n\nVerdict: APPROVE")
        assert scan_conversation_for_verdict(sm) == "approve"


class TestEmptySessionDetection:
    def test_detects_empty_session(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        with patch("core.post_run.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            })()
            assert check_empty_session(repo, "agent/test", "master") is True
            mock_run.assert_called_once_with(
                ["git", "log", "--oneline", "master..agent/test"],
                capture_output=True,
                text=True,
                cwd=repo,
            )


class TestRebaseMovedToHost:
    """Rebase now runs on host side, not in container.

    Both coder and review sessions transition to waiting:review from container.
    The host-side review_orchestrator handles rebase before launching review.
    """

    def test_review_done_no_rebase_in_container(self, tmp_path):
        """Review sessions should not attempt rebase in container."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        ws_mgr.rebase_result = RebaseResult(
            success=False, conflict_details="conflict in main.py")
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master", is_review=True)
        assert result is None
        assert sm.load_state().status == "waiting:review"
        assert len(ws_mgr.rebase_calls) == 0

    def test_coder_done_no_rebase_in_container(self, tmp_path):
        """Coder sessions also should not attempt rebase in container.

        Rebase is now handled on the host side by review_orchestrator.
        """
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        ws_mgr.rebase_result = RebaseResult(
            success=False, conflict_details="conflict in main.py")
        sm.update_status("done:pending-review")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            base_branch="master", is_review=False)
        # Coder should proceed to waiting:review, host will handle rebase
        assert result is None
        assert sm.load_state().status == "waiting:review"
        assert len(ws_mgr.rebase_calls) == 0


class TestReviewMaxTurns:
    """Review sessions should not auto-resume on max-turns."""

    def test_review_max_turns_with_verdict_treats_as_done(self, tmp_path):
        """When review hits max-turns but a verdict was emitted, treat as done."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.append_conversation("assistant", "All tests pass. @nightshift approve")
        commits = []
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: commits.append(r),
            is_review=True)
        assert result is None
        assert sm.load_state().status == "waiting:review"
        assert "max-turns" in commits

    def test_review_max_turns_forwards_base_branch_to_done(self, tmp_path):
        """Review completion must preserve the configured base branch."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.append_conversation("assistant", "All tests pass. @nightshift approve")
        with patch("core.post_run.notify_done") as mock_notify_done:
            result = post_run_action(
                sm, ws_mgr, ws, tracker, notifier, issue, agent,
                lambda **kw: None, lambda r: None,
                base_branch="agent/base", is_review=True)
        assert result is None
        mock_notify_done.assert_called_once()
        assert mock_notify_done.call_args.kwargs["base_branch"] == "agent/base"

    def test_review_max_turns_without_verdict_falls_back(self, tmp_path):
        """When review hits max-turns with no verdict, set suspended:review-no-verdict."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.append_conversation("assistant", "Running tests...")
        commits = []
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: commits.append(r),
            is_review=True)
        assert result is None
        assert sm.load_state().status == "suspended:review-no-verdict"
        assert "max-turns" in commits
        assert any("human review" in n for n in notifier.notifications)

    def test_coder_max_turns_still_auto_resumes(self, tmp_path):
        """Non-review sessions should still auto-resume on max-turns."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.write_resume_prompt("resume")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            is_review=False)
        assert result == "resume"

    def test_review_max_turns_with_revise_verdict(self, tmp_path):
        """Review max-turns with revise verdict should also treat as done."""
        agent, tracker, notifier, ws_mgr, sm, ws, issue = _setup(tmp_path)
        sm.update_status("working")
        sm.append_conversation("assistant", "Tests fail. @nightshift revise")
        result = post_run_action(
            sm, ws_mgr, ws, tracker, notifier, issue, agent,
            lambda **kw: None, lambda r: None,
            is_review=True)
        assert result is None
        assert sm.load_state().status == "waiting:review"
