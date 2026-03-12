"""Tests for core/session.py -- SessionRunner."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.protocols import (
    AgentEvent, AgentEventType, TrackerIssue, Workspace,
)
from core.config.models import HooksConfig, MergeConfig
from core.state import StateManager, SessionState
from core.session import SessionRunner, MAX_RESUMES
from core.answer_collector import collect_answer
from core.post_run import notify_done, maybe_summarize_checkpoints

# Re-use mocks from conftest
from tests.conftest import (
    MockAgent, MockTracker, MockNotifier, MockWorkspaceManager, make_test_issue,
)


# ── Helpers ──────────────────────────────────────────────────

def _init_state(session_dir: Path, issue_id: str = "test-001", branch: str = "agent/test-001"):
    """Create initial state.json so StateManager.load_state works."""
    sm = StateManager(session_dir)
    state = SessionState(issue_id=issue_id, branch=branch, status="working")
    sm._write(state)
    return sm


class ScriptedAgent(MockAgent):
    """MockAgent that plays a different script on each start() call.

    Each entry in `scripts` is a list of AgentEvent that will be yielded
    for that agent cycle.  After all scripts are exhausted, subsequent
    start() calls produce no events.
    """

    def __init__(self, scripts: list[list[AgentEvent]]):
        super().__init__([])
        self._scripts = list(scripts)
        self._cycle = -1

    def start(self, prompt, workspace, max_turns=50):
        super().start(prompt, workspace, max_turns)
        self._cycle += 1

    def stream_events(self):
        if self._cycle < len(self._scripts):
            yield from self._scripts[self._cycle]


def _make_runner(
    tmp_path: Path,
    events: list[AgentEvent] | None = None,
    issue: TrackerIssue | None = None,
    hooks_config: HooksConfig | None = None,
    merge_config: MergeConfig | None = None,
    max_turns: int = 50,
    agent: MockAgent | None = None,
) -> tuple[SessionRunner, MockAgent, MockTracker, MockNotifier, MockWorkspaceManager, StateManager]:
    issue = issue or make_test_issue()
    if agent is None:
        # Use ScriptedAgent with a single script so events are NOT replayed
        agent = ScriptedAgent([events or []])
    tracker = MockTracker({issue.id: issue})
    notifier = MockNotifier()
    ws_mgr = MockWorkspaceManager(tmp_path)
    session_dir = tmp_path / "session"
    state_mgr = _init_state(session_dir, issue.id, f"agent/{issue.identifier}")

    runner = SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=ws_mgr, state_mgr=state_mgr, issue=issue,
        prompt="Fix the widget", max_turns=max_turns,
        merge_config=merge_config, hooks_config=hooks_config,
    )
    return runner, agent, tracker, notifier, ws_mgr, state_mgr


def _text_event(text: str) -> AgentEvent:
    return AgentEvent(type=AgentEventType.TEXT, content=text, raw=text)


def _tool_call_event(content: str = "tool call") -> AgentEvent:
    return AgentEvent(type=AgentEventType.TOOL_CALL, content=content, raw=content)


def _tool_result_event(content: str = "tool result") -> AgentEvent:
    return AgentEvent(type=AgentEventType.TOOL_RESULT, content=content, raw=content)


def _system_event(content: str = "system msg") -> AgentEvent:
    return AgentEvent(type=AgentEventType.SYSTEM, content=content, raw=content)


def _stall_event(content: str = "agent stalled") -> AgentEvent:
    return AgentEvent(type=AgentEventType.STALL, content=content, raw=content)


def _exit_event() -> AgentEvent:
    return AgentEvent(type=AgentEventType.PROCESS_EXIT, content="exit", raw="exit")


# ── Tests ────────────────────────────────────────────────────

class TestSessionRunnerInit:
    def test_default_merge_config(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        assert runner.merge_config is not None
        assert runner.merge_config.require_review is True

    def test_custom_merge_config(self, tmp_path):
        mc = MergeConfig(require_review=False)
        runner, *_ = _make_runner(tmp_path, merge_config=mc)
        assert runner.merge_config.require_review is False

    def test_hooks_config_none(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        assert runner.hooks_config is None


class TestBasicRun:
    def test_run_with_no_events(self, tmp_path):
        """Agent produces no events -> 'working' status -> max-turns auto-resume loop -> max-resumes."""
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner.run()
        assert agent.started
        assert agent.terminated
        st = state_mgr.load_state()
        # No events means status stays 'working', which triggers max-turns resume,
        # looping until MAX_RESUMES is hit
        assert st.status == "suspended:max-resumes"

    def test_run_with_done_marker(self, tmp_path):
        events = [_text_event("@@DONE@@ all finished")]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "waiting:review"

    def test_run_uses_provided_workspace(self, tmp_path):
        ws = Workspace(path=tmp_path / "custom-ws", branch="custom", is_new=False)
        ws.path.mkdir()
        runner, agent, *_ = _make_runner(tmp_path, events=[_exit_event()])
        runner.run(workspace=ws)
        assert runner._workspace is ws

    def test_run_creates_workspace_when_none(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=[_exit_event()])
        runner.run()
        assert len(ws_mgr.created) == 1


class TestEventLoop:
    def test_tool_call_recorded(self, tmp_path):
        events = [_tool_call_event("read file.py"), _exit_event()]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        conv = state_mgr.conversation_log.read_text()
        assert "tool_call" in conv

    def test_tool_result_recorded(self, tmp_path):
        events = [_tool_result_event("contents of file"), _exit_event()]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        conv = state_mgr.conversation_log.read_text()
        assert "tool_result" in conv

    def test_system_event_recorded(self, tmp_path):
        events = [_system_event("some system info"), _exit_event()]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        conv = state_mgr.conversation_log.read_text()
        assert "system" in conv

    def test_process_exit_breaks_loop(self, tmp_path):
        events = [
            _text_event("working..."),
            _exit_event(),
            _text_event("should not be reached"),
        ]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        conv = state_mgr.conversation_log.read_text()
        assert "should not be reached" not in conv


class TestContextLimit:
    def test_context_window_triggers_auto_resume(self, tmp_path):
        """Context limit on first cycle, done on second."""
        agent = ScriptedAgent([
            [_system_event("context window exceeded")],
            [_text_event("@@DONE@@ finished after resume")],
        ])
        runner, _, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, agent=agent)
        runner.run()
        assert any("context limit" in c for c in ws_mgr.commits)
        st = state_mgr.load_state()
        assert st.status == "waiting:review"

    def test_token_limit_triggers_auto_resume(self, tmp_path):
        agent = ScriptedAgent([
            [_system_event("token limit reached")],
            [_text_event("@@DONE@@ done")],
        ])
        runner, *_, ws_mgr, state_mgr = _make_runner(tmp_path, agent=agent)
        runner.run()
        assert any("context limit" in c for c in ws_mgr.commits)


class TestStallDetection:
    def test_stall_triggers_auto_resume(self, tmp_path):
        agent = ScriptedAgent([
            [_stall_event("no output for 5 minutes")],
            [_text_event("@@DONE@@ done")],
        ])
        runner, _, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, agent=agent)
        runner.run()
        assert any("stalled" in c for c in ws_mgr.commits)
        st = state_mgr.load_state()
        assert st.status == "waiting:review"


class TestCheckpoint:
    def test_checkpoint_marker(self, tmp_path):
        events = [
            _text_event("@@CHECKPOINT@@ implemented widget parser"),
            _text_event("@@DONE@@ all done"),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        runner.run()
        st = state_mgr.load_state()
        assert len(st.checkpoints) == 1
        assert st.checkpoints[0].description == "implemented widget parser"
        assert st.checkpoints[0].step == 1
        assert st.checkpoints[0].commit == "abc1234"
        assert any("checkpoint" in c for c in ws_mgr.commits)

    def test_checkpoint_increments_step(self, tmp_path):
        events = [
            _text_event("@@CHECKPOINT@@ step one"),
            _text_event("@@CHECKPOINT@@ step two"),
            _text_event("@@DONE@@ done"),
        ]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        st = state_mgr.load_state()
        assert len(st.checkpoints) == 2
        assert st.checkpoints[0].step == 1
        assert st.checkpoints[1].step == 2


class TestDoneMarker:
    def test_done_commits_and_notifies(self, tmp_path):
        events = [_text_event("@@DONE@@ everything fixed")]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "waiting:review"
        assert any("resolve" in c for c in ws_mgr.commits)
        issue = tracker.issues["test-001"]
        assert "needs-review" in issue.labels
        assert any("done" in n for n in notifier.notifications)


class TestLogMarker:
    def test_log_marker_posts_comment(self, tmp_path):
        events = [
            _text_event("@@LOG@@ analyzing codebase"),
            _exit_event(),
        ]
        runner, agent, tracker, *_ = _make_runner(tmp_path, events=events)
        runner.run()
        comments = tracker.get_comments("test-001")
        assert any("analyzing codebase" in c.body for c in comments)


class TestQuestionWaitingFlow:
    def test_question_then_waiting_with_answer_file(self, tmp_path):
        """Question + waiting -> collect answer from answer.txt."""
        events = [
            _text_event("@@QUESTION@@ What color?\n@@WAITING@@"),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        # Write answer file before run so _collect_answer finds it immediately
        state_mgr.answer_file.write_text("blue")
        runner.run()
        st = state_mgr.load_state()
        assert len(st.human_answers) == 1
        assert st.human_answers[0].question == "What color?"
        assert st.human_answers[0].answer == "blue"

    def test_question_notifier_send_question_called(self, tmp_path):
        events = [
            _text_event("@@QUESTION@@ What version?"),
            _exit_event(),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        state_mgr.answer_file.write_text("v2.0")
        runner.run()
        assert len(notifier.questions) == 1
        assert notifier.questions[0]["question"] == "What version?"

    def test_question_notifier_fallback_when_send_fails(self, tmp_path):
        events = [
            _text_event("@@QUESTION@@ What API key?"),
            _exit_event(),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        notifier.send_question = lambda *a, **kw: False
        state_mgr.answer_file.write_text("abc123")
        runner.run()
        assert any("What API key?" in n for n in notifier.notifications)

    def test_waiting_without_question_ignored(self, tmp_path):
        """WAITING with no pending question should be ignored."""
        events = [
            _text_event("@@WAITING@@"),
            _exit_event(),
        ]
        runner, *_, state_mgr = _make_runner(tmp_path, events=events)
        runner.run()
        st = state_mgr.load_state()
        assert len(st.human_answers) == 0

    def test_answer_sent_to_alive_agent(self, tmp_path):
        """If agent is alive when answer arrives, send_input is called."""
        events = [
            _text_event("@@QUESTION@@ What color?\n@@WAITING@@"),
            _exit_event(),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        state_mgr.answer_file.write_text("red")
        runner.run()
        # Agent is alive during _on_waiting (before terminate in finally block)
        assert "red" in agent.inputs_sent

    def test_agent_exited_with_pending_question(self, tmp_path):
        """Agent exits with pending question -> answer collected post-loop."""
        events = [
            _text_event("@@QUESTION@@ Which DB?"),
            # No WAITING, agent just exits
            _exit_event(),
        ]
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=events)
        state_mgr.answer_file.write_text("postgres")
        runner.run()
        st = state_mgr.load_state()
        assert len(st.human_answers) == 1
        assert st.human_answers[0].answer == "postgres"

    def test_answer_ready_restarts_agent(self, tmp_path):
        """After answer collected with dead agent, _post_run returns answer for restart.

        In -p mode the agent exits after responding. When _on_waiting runs
        post-loop, the agent is still alive (terminate hasn't been called yet),
        so send_input is used and status stays 'working'. Then _post_run does
        a max-turns resume. To test the suspended:answer-ready path, we
        test _post_run directly.
        """
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        state_mgr.update_status("suspended:answer-ready")
        state_mgr.add_qa("Which DB?", "postgres")
        result = runner._post_run()
        assert result == "postgres"
        st = state_mgr.load_state()
        assert st.status == "working"


class TestAutoResume:
    def test_max_resumes_stops(self, tmp_path):
        """Hitting MAX_RESUMES stops the loop."""
        scripts = [[_system_event("context window full")] for _ in range(MAX_RESUMES + 1)]
        agent = ScriptedAgent(scripts)
        runner, _, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, agent=agent)
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "suspended:max-resumes"
        assert any(str(MAX_RESUMES) in n for n in notifier.notifications)

    def test_context_limit_auto_resumes(self, tmp_path):
        """Context limit should auto-resume with a new prompt."""
        agent = ScriptedAgent([
            [_system_event("context window exceeded")],
            [_text_event("@@DONE@@ finished after resume")],
        ])
        runner, _, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, agent=agent)
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "waiting:review"

    def test_stall_auto_resumes(self, tmp_path):
        agent = ScriptedAgent([
            [_stall_event("no progress")],
            [_text_event("@@DONE@@ done")],
        ])
        runner, *_, state_mgr = _make_runner(tmp_path, agent=agent)
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "waiting:review"


class TestPostRun:
    def test_cancelled_external(self, tmp_path):
        """If issue is closed externally."""
        issue = make_test_issue(status="closed")
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=[_exit_event()], issue=issue)
        state_mgr.update_status("cancelled:external")
        result = runner._post_run()
        assert result is None
        comments = tracker.get_comments(issue.id)
        assert any("closed externally" in c.body for c in comments)

    def test_working_status_triggers_max_turns_resume(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        state_mgr.update_status("working")
        result = runner._post_run()
        assert result is not None
        assert any("max-turns" in c for c in ws_mgr.commits)

    def test_completed_status_returns_none(self, tmp_path):
        runner, *_, state_mgr = _make_runner(tmp_path)
        state_mgr.update_status("completed")
        assert runner._post_run() is None

    def test_cancelled_review_rejected_returns_none(self, tmp_path):
        runner, *_, state_mgr = _make_runner(tmp_path)
        state_mgr.update_status("cancelled:review-rejected")
        assert runner._post_run() is None

    def test_answer_ready_restarts_with_answer(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        state_mgr.update_status("suspended:answer-ready")
        state_mgr.add_qa("What color?", "blue")
        result = runner._post_run()
        assert result == "blue"
        st = state_mgr.load_state()
        assert st.status == "working"

    def test_answer_ready_empty_answers(self, tmp_path):
        runner, *_, state_mgr = _make_runner(tmp_path)
        state_mgr.update_status("suspended:answer-ready")
        result = runner._post_run()
        assert result == ""

    def test_unexpected_status(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        state_mgr.update_status("some-weird-status")
        result = runner._post_run()
        assert result is None
        st = state_mgr.load_state()
        assert st.status == "suspended:unexpected"
        assert any("unexpectedly" in n for n in notifier.notifications)

    def test_done_pending_review_notifies(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        state_mgr.update_status("done:pending-review")
        result = runner._post_run()
        assert result is None
        st = state_mgr.load_state()
        assert st.status == "waiting:review"


class TestHooks:
    def test_after_create_hook_runs_on_new_workspace(self, tmp_path):
        hooks = HooksConfig(after_create="echo hello", before_run=None, after_run=None)
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=[_text_event("@@DONE@@ done")], hooks_config=hooks)
        runner.run()
        assert agent.started

    def test_before_run_hook_failure_stops(self, tmp_path):
        hooks = HooksConfig(after_create=None, before_run="fail", after_run=None)
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=[_exit_event()], hooks_config=hooks)
        ws_mgr.run_hook = lambda path, script, timeout: script != "fail"
        runner.run()
        st = state_mgr.load_state()
        assert st.status == "suspended:hook-failure"
        assert any("before_run hook failed" in n for n in notifier.notifications)

    def test_after_run_hook_runs(self, tmp_path):
        hooks = HooksConfig(after_create=None, before_run=None, after_run="echo done")
        hook_calls = []
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(
            tmp_path, events=[_exit_event()], hooks_config=hooks)
        orig_run_hook = ws_mgr.run_hook

        def tracking_hook(path, script, timeout):
            hook_calls.append(script)
            return orig_run_hook(path, script, timeout)

        ws_mgr.run_hook = tracking_hook
        runner.run()
        assert "echo done" in hook_calls


class TestRunHook:
    def test_run_hook_no_script(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        assert runner._run_hook(None, "test") is True

    def test_run_hook_no_workspace(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        runner._workspace = None
        assert runner._run_hook("echo hi", "test") is True

    def test_run_hook_subprocess_fallback(self, tmp_path):
        """When workspace_mgr has no run_hook, falls back to subprocess."""
        runner, *_ = _make_runner(
            tmp_path, hooks_config=HooksConfig(timeout_s=10))
        runner._workspace = Workspace(path=tmp_path, branch="test")

        # Create a workspace manager that does NOT have run_hook
        class MinimalWsMgr:
            def create(self, issue): pass
            def cleanup(self, issue): pass
            def finalize(self, issue, target_branch="master"): pass
            def commit(self, workspace, message): pass
            def has_changes(self, workspace): return True
            def diff_stat(self, workspace, base="master"): return ""
            def get_current_commit(self, workspace): return "abc"

        runner.workspace_mgr = MinimalWsMgr()
        result = runner._run_hook("echo hello", "test_hook")
        assert result is True

    def test_run_hook_subprocess_failure(self, tmp_path):
        runner, *_ = _make_runner(tmp_path, hooks_config=HooksConfig(timeout_s=5))
        runner._workspace = Workspace(path=tmp_path, branch="test")

        class MinimalWsMgr:
            def create(self, issue): pass
            def cleanup(self, issue): pass
            def finalize(self, issue, target_branch="master"): pass
            def commit(self, workspace, message): pass
            def has_changes(self, workspace): return True
            def diff_stat(self, workspace, base="master"): return ""
            def get_current_commit(self, workspace): return "abc"

        runner.workspace_mgr = MinimalWsMgr()
        result = runner._run_hook("exit 1", "fail_hook", fatal=True)
        assert result is False


class TestExtractQuestion:
    def test_extract_simple(self):
        text = "@@QUESTION@@ What color?\n@@WAITING@@"
        assert SessionRunner._extract_question(text) == "What color?"

    def test_extract_multiline(self):
        text = "@@QUESTION@@\nLine 1\nLine 2\n@@WAITING@@"
        result = SessionRunner._extract_question(text)
        assert "Line 1" in result
        assert "Line 2" in result

    def test_no_question(self):
        assert SessionRunner._extract_question("just text") is None

    def test_question_before_done(self):
        text = "@@QUESTION@@ Ready to merge?\n@@DONE@@"
        assert SessionRunner._extract_question(text) == "Ready to merge?"

    def test_question_no_end_marker(self):
        text = "@@QUESTION@@ standalone question with details"
        assert SessionRunner._extract_question(text) == "standalone question with details"


class TestReconciliation:
    def test_terminal_issue_breaks_loop(self, tmp_path):
        issue = make_test_issue(status="open")
        runner, agent, tracker, *_ = _make_runner(tmp_path, issue=issue)
        assert runner._issue_is_terminal() is False
        tracker.set_status(issue.id, "closed")
        assert runner._issue_is_terminal() is True


class TestCommitWip:
    def test_commit_wip_with_workspace(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        runner._commit_wip("test reason")
        assert any("test reason" in c for c in ws_mgr.commits)

    def test_commit_wip_without_workspace(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = None
        runner._commit_wip("test reason")
        assert len(ws_mgr.commits) == 0


class TestCommitCheckpoint:
    def test_commit_checkpoint_with_workspace(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        result = runner._commit_checkpoint("did stuff", 3)
        assert result == "abc1234"
        assert any("checkpoint(3)" in c for c in ws_mgr.commits)

    def test_commit_checkpoint_without_workspace(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        runner._workspace = None
        result = runner._commit_checkpoint("no ws", 1)
        assert result == "none"


class TestNotifyDone:
    def test_notify_done_posts_summary(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        ws = Workspace(path=tmp_path, branch="test")
        state_mgr.add_checkpoint("step one", 1, "abc1234")
        st = state_mgr.load_state()
        issue = make_test_issue()
        notify_done(state_mgr, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments("test-001")
        assert any("Work complete" in c.body for c in comments)
        assert any("step one" in c.body for c in comments)
        issue_obj = tracker.issues["test-001"]
        assert "needs-review" in issue_obj.labels

    def test_notify_done_no_checkpoints(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        ws = Workspace(path=tmp_path, branch="test")
        st = state_mgr.load_state()
        issue = make_test_issue()
        notify_done(state_mgr, ws_mgr, ws, tracker, notifier, issue, st)
        comments = tracker.get_comments("test-001")
        assert any("No checkpoints recorded" in c.body for c in comments)

    def test_notify_done_no_workspace(self, tmp_path):
        runner, _, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        st = state_mgr.load_state()
        issue = make_test_issue()
        notify_done(state_mgr, ws_mgr, None, tracker, notifier, issue, st)
        # Should use "N/A" for diff


class TestMaybeSummarizeCheckpoints:
    def test_short_checkpoint_list_skipped(self, tmp_path):
        runner, agent, *_ = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        state_mgr = runner.state_mgr
        for i in range(5):
            state_mgr.add_checkpoint(f"step {i}", i, f"commit{i}")
        maybe_summarize_checkpoints(
            state_mgr, agent, runner._workspace,
            runner._build_resume)
        # Agent should NOT have been started for summarization

    def test_long_checkpoint_list_triggers_summarization(self, tmp_path):
        summarize_agent = ScriptedAgent([
            [],  # main run (no events - handled elsewhere)
            [_text_event("Summary of key decisions"), _exit_event()],
        ])
        runner, _, *_ = _make_runner(tmp_path, agent=summarize_agent)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        state_mgr = runner.state_mgr
        for i in range(12):
            state_mgr.add_checkpoint(f"step {i}", i, f"commit{i}")
        # Pre-increment cycle since _make_runner doesn't call start
        summarize_agent._cycle = 0
        maybe_summarize_checkpoints(
            state_mgr, summarize_agent, runner._workspace,
            runner._build_resume)
        assert summarize_agent.started


class TestCollectAnswer:
    def test_collect_from_answer_file(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        state_mgr.answer_file.write_text("the answer")
        answer = collect_answer(state_mgr, notifier, tracker, "test-001")
        assert answer == "the answer"

    def test_collect_from_notifier(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        notifier.pending_answers["test-001"] = "notifier answer"
        with patch("core.answer_collector.ANSWER_POLL_S", 0):
            answer = collect_answer(state_mgr, notifier, tracker, "test-001")
        assert answer == "notifier answer"

    def test_collect_from_tracker_comments(self, tmp_path):
        runner, agent, tracker, notifier, ws_mgr, state_mgr = _make_runner(tmp_path)
        tracker.comments["test-001"] = []
        poll_count = 0
        orig_check_answer = state_mgr.check_answer

        def mock_check():
            nonlocal poll_count
            poll_count += 1
            if poll_count == 31:
                tracker.add_comment("test-001", "human says: use postgres")
            return orig_check_answer()

        state_mgr.check_answer = mock_check
        notifier.check_answer = lambda issue_id: None

        with patch("core.answer_collector.ANSWER_POLL_S", 0):
            answer = collect_answer(state_mgr, notifier, tracker, "test-001")
        assert answer == "human says: use postgres"


class TestDispatchEvent:
    def test_dispatch_text_returns_handle_text_result(self, tmp_path):
        runner, *_, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        event = _text_event("@@DONE@@ finished")
        result = runner._dispatch_event(event)
        assert result == "STOP"

    def test_dispatch_stall_returns_stop(self, tmp_path):
        runner, *_, state_mgr = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        result = runner._dispatch_event(_stall_event("stuck"))
        assert result == "STOP"

    def test_dispatch_process_exit_returns_stop(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        result = runner._dispatch_event(_exit_event())
        assert result == "STOP"

    def test_dispatch_context_limit_returns_stop(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        runner._workspace = Workspace(path=tmp_path, branch="test")
        result = runner._dispatch_event(_system_event("context window exceeded"))
        assert result == "STOP"

    def test_dispatch_normal_system_returns_none(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        result = runner._dispatch_event(_system_event("normal info"))
        assert result is None

    def test_dispatch_tool_call_returns_none(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        result = runner._dispatch_event(_tool_call_event())
        assert result is None

    def test_dispatch_tool_result_returns_none(self, tmp_path):
        runner, *_ = _make_runner(tmp_path)
        result = runner._dispatch_event(_tool_result_event())
        assert result is None
