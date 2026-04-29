"""Tests for adapters/agents/codex.py — CodexAgent adapter."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.agents.codex import CodexAgent
from core.session import SessionRunner
from core.state import StateManager, SessionState
from core.protocols import AgentEvent, AgentEventType
from core.protocols import Workspace
from tests.conftest import (
    MockAgent,
    MockNotifier,
    MockTracker,
    MockWorkspaceManager,
    make_test_issue,
)


# ── Helpers ────────────────────────────────────────────────


def _ev(event_type: str, **extra) -> str:
    """Build a JSON event string."""
    d = {"type": event_type}
    d.update(extra)
    return json.dumps(d)


def _item_ev(event_type: str, item_type: str, **item_fields) -> str:
    """Build an item.started/item.completed event string."""
    item = {"id": "item_0", "type": item_type}
    item.update(item_fields)
    return json.dumps({"type": event_type, "item": item})


# ── Constructor ────────────────────────────────────────────


class TestConstructor:
    def test_default_command(self):
        agent = CodexAgent()
        assert agent.command == "codex"

    def test_custom_command(self):
        agent = CodexAgent(command="/usr/local/bin/codex")
        assert agent.command == "/usr/local/bin/codex"

    def test_extra_args(self):
        agent = CodexAgent(extra_args=["-m", "o3"])
        assert agent.extra_args == ["-m", "o3"]

    def test_stall_timeout(self):
        agent = CodexAgent(stall_timeout_s=120)
        assert agent.stall_timeout_s == 120


class TestSignalMethodOutput:
    def _done_file(self) -> Path:
        return Path("/session/signal/done")

    def test_signal_method_file_writes_file_signal(self):
        done_file = self._done_file()
        done_file.unlink(missing_ok=True)

        agent = CodexAgent(signal_method="file")
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task complete"},
        )

        ev = agent._parse(raw)

        assert ev is None
        assert done_file.exists()
        done_file.unlink(missing_ok=True)

    def test_signal_method_text_emits_marker(self):
        done_file = self._done_file()
        done_file.unlink(missing_ok=True)

        agent = CodexAgent(signal_method="text")
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task complete"},
        )

        assert agent._parse(raw) is None

        turn_raw = _ev("turn.completed", usage={
            "input_tokens": 20000, "output_tokens": 80,
        })
        ev = agent._parse(turn_raw)

        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@DONE@@"
        assert not done_file.exists()

    def test_signal_method_auto_emits_text_and_done(self):
        done_file = self._done_file()
        done_file.unlink(missing_ok=True)

        agent = CodexAgent(signal_method="auto")
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task complete"},
        )

        assert agent._parse(raw) is None

        turn_raw = _ev("turn.completed", usage={
            "input_tokens": 20000, "output_tokens": 80,
        })
        ev = agent._parse(turn_raw)
        extras = list(agent._drain_extra())

        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@DONE@@"
        assert len(extras) == 1
        assert extras[0].type == AgentEventType.DONE
        assert extras[0].raw == raw
        assert done_file.exists()
        done_file.unlink(missing_ok=True)


# ── start() ───────────────────────────────────────────────


class TestStart:
    @patch("adapters.agents.codex._in_docker", return_value=False)
    @patch("adapters.agents.codex.subprocess.Popen")
    def test_start_builds_exec_command(self, mock_popen, mock_in_docker):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent.start("do stuff", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "--json" in cmd
        assert "--full-auto" in cmd
        assert "do stuff" in cmd
        assert "resume" not in cmd
        assert agent.pid == 42

    @patch("adapters.agents.codex._in_docker", return_value=True)
    @patch("adapters.agents.codex.subprocess.Popen")
    def test_start_in_docker_uses_bypass_flag(self, mock_popen, mock_in_docker):
        """In Docker, use --dangerously-bypass-approvals-and-sandbox instead of --full-auto."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent.start("do stuff", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd

    @patch("adapters.agents.codex._in_docker", return_value=False)
    @patch("adapters.agents.codex.subprocess.Popen")
    def test_start_resume_uses_thread_id(self, mock_popen, mock_in_docker):
        mock_proc = MagicMock()
        mock_proc.pid = 99
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent._session_id = "019d-abc-123"
        agent.start("continue", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "resume" in cmd
        assert "019d-abc-123" in cmd
        assert "continue" in cmd

    @patch("adapters.agents.codex._in_docker", return_value=False)
    @patch("adapters.agents.codex.subprocess.Popen")
    def test_start_passes_extra_args(self, mock_popen, mock_in_docker):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent = CodexAgent(extra_args=["-m", "gpt-5.4"])
        agent.start("task", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "-m" in cmd
        assert "gpt-5.4" in cmd

    @patch("adapters.agents.codex.subprocess.Popen")
    def test_start_uses_workspace_as_cwd(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent.start("task", Path("/my/workspace"))

        kwargs = mock_popen.call_args[1]
        assert kwargs["cwd"] == "/my/workspace"


# ── _parse() — thread events ─────────────────────────────


class TestParseThreadEvents:
    def _agent(self):
        return CodexAgent()

    def test_thread_started_extracts_session_id(self):
        agent = self._agent()
        raw = _ev("thread.started", thread_id="019d-abc-123")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert agent._session_id == "019d-abc-123"

    def test_turn_started_returns_none(self):
        agent = self._agent()
        raw = _ev("turn.started")
        ev = agent._parse(raw)
        assert ev is None

    def test_turn_completed_emits_done(self):
        agent = self._agent()
        raw = _ev("turn.completed", usage={
            "input_tokens": 20000, "output_tokens": 80,
        })
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in ev.content

    def test_turn_completed_includes_usage_metadata(self):
        agent = self._agent()
        raw = _ev("turn.completed", usage={
            "input_tokens": 20000, "cached_input_tokens": 10000,
            "output_tokens": 80,
        })
        ev = agent._parse(raw)
        assert ev.metadata["usage"]["input_tokens"] == 20000
        assert ev.metadata["usage"]["output_tokens"] == 80

    def test_turn_completed_no_usage(self):
        agent = self._agent()
        raw = _ev("turn.completed")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in ev.content


# ── _parse() — item events ───────────────────────────────


class TestParseItemEvents:
    def _agent(self):
        return CodexAgent()

    def test_agent_message_completed(self):
        agent = self._agent()
        raw = _item_ev("item.completed", "agent_message", text="I'll create the file.")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TEXT
        assert "create the file" in ev.content

    def test_agent_message_empty_text_returns_none(self):
        agent = self._agent()
        raw = _item_ev("item.completed", "agent_message", text="")
        ev = agent._parse(raw)
        assert ev is None

    def test_command_execution_in_progress(self):
        agent = self._agent()
        raw = _item_ev("item.started", "command_execution",
                       command="echo hello", status="in_progress")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_CALL
        assert "echo hello" in ev.content

    def test_command_execution_completed(self):
        agent = self._agent()
        raw = _item_ev("item.completed", "command_execution",
                       command="echo hello", aggregated_output="hello\n",
                       exit_code=0, status="completed")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_RESULT
        assert "exit=0" in ev.content
        assert "hello" in ev.content

    def test_command_execution_failed(self):
        agent = self._agent()
        raw = _item_ev("item.completed", "command_execution",
                       command="false", aggregated_output="",
                       exit_code=1, status="completed")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_RESULT
        assert "exit=1" in ev.content

    def test_unknown_item_type(self):
        agent = self._agent()
        raw = _item_ev("item.completed", "file_edit", path="/foo.py")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "file_edit" in ev.content


# ── _parse() — error events ──────────────────────────────


class TestParseErrorEvents:
    def _agent(self):
        return CodexAgent()

    def test_error_event(self):
        agent = self._agent()
        raw = _ev("error", message="something went wrong")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "something went wrong" in ev.content

    def test_error_auth_failure(self):
        agent = self._agent()
        raw = _ev("error", message="unexpected status 401 Unauthorized: Missing Authentication header")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_turn_failed_auth(self):
        agent = self._agent()
        raw = _ev("turn.failed", error={"message": "status 401 Unauthorized"})
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_turn_failed_non_auth(self):
        agent = self._agent()
        raw = _ev("turn.failed", error={"message": "internal server error"})
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "turn.failed" in ev.content

    def test_turn_failed_usage_limit(self):
        """Usage limit errors in turn.failed should be treated as transient."""
        agent = self._agent()
        raw = _ev("turn.failed", error={
            "message": "You've hit your usage limit. Upgrade to Pro or try again at 7:22 PM."
        })
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "turn.failed" in ev.content

    def test_error_rate_limit_not_auth_failure(self):
        """Rate limit errors are handled as transient errors, not auth failures."""
        agent = self._agent()
        raw = _ev("error", message="status 429 rate limit exceeded")
        ev = agent._parse(raw)
        # Rate limit is now a transient error, not auth failure
        assert ev.type == AgentEventType.SYSTEM

    def test_error_reconnecting_not_auth_failure(self):
        """Reconnecting messages are transient — not auth failures unless they match patterns."""
        agent = self._agent()
        raw = _ev("error", message="Reconnecting... 1/5 (timeout)")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM

    def test_high_demand_detected_as_overload(self):
        """High demand errors at retry limit (5/5) should emit PROVIDER_OVERLOAD."""
        agent = self._agent()
        raw = _ev("error", message="Reconnecting... 5/5 (We're currently experiencing high demand, which may cause temporary errors.)")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.PROVIDER_OVERLOAD
        assert "high demand" in ev.content

    def test_high_demand_early_retries_not_overload(self):
        """High demand errors before retry limit should be SYSTEM (transient)."""
        agent = self._agent()
        raw = _ev("error", message="Reconnecting... 3/5 (high demand)")
        ev = agent._parse(raw)
        # Early retries are transient, not overload
        assert ev.type == AgentEventType.SYSTEM


# ── _parse() — edge cases ────────────────────────────────


class TestParseEdgeCases:
    def _agent(self):
        return CodexAgent()

    def test_empty_line(self):
        assert self._agent()._parse("") is None
        assert self._agent()._parse("   ") is None

    def test_invalid_json(self):
        agent = self._agent()
        ev = agent._parse("not json at all")
        assert ev.type == AgentEventType.TEXT
        assert "not json at all" in ev.content

    def test_non_dict_json(self):
        agent = self._agent()
        ev = agent._parse("[1, 2, 3]")
        assert ev is None

    def test_unknown_event_type(self):
        agent = self._agent()
        raw = _ev("session.archived")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.UNKNOWN


# ── _is_auth_failure() ───────────────────────────────────


class TestIsAuthFailure:
    def test_usage_limit_not_detected_as_auth_failure(self):
        """Usage limit errors should not be detected as auth failures."""
        assert not CodexAgent._is_auth_failure(
            "You've hit your usage limit. Upgrade to Pro or try again at 7:22 PM."
        )
        assert not CodexAgent._is_auth_failure("hit your usage limit")
        assert not CodexAgent._is_auth_failure("usage limit exceeded")

    def test_detects_401(self):
        assert CodexAgent._is_auth_failure("unexpected status 401 Unauthorized")

    def test_429_not_auth_failure(self):
        """429 errors are handled as transient errors, not auth failures."""
        # 429/rate limit are now handled by HeadlessAgentBase._is_transient_error()
        assert not CodexAgent._is_auth_failure("status 429 rate limit")

    def test_detects_invalid_api_key(self):
        assert CodexAgent._is_auth_failure("Invalid API key provided")

    def test_detects_insufficient_quota(self):
        assert CodexAgent._is_auth_failure("You have insufficient_quota")

    def test_detects_missing_authentication(self):
        assert CodexAgent._is_auth_failure("Missing Authentication header")

    def test_ignores_500(self):
        assert not CodexAgent._is_auth_failure("status 500 internal server error")

    def test_ignores_normal_output(self):
        assert not CodexAgent._is_auth_failure("Created file hello.py")

    def test_case_insensitive(self):
        assert CodexAgent._is_auth_failure("UNAUTHORIZED access denied")

    def test_missing_env_var_is_auth_failure(self):
        """Missing environment variable should be detected as auth failure."""
        assert CodexAgent._is_auth_failure("Missing environment variable: CODEX_API_KEY")
        assert CodexAgent._is_auth_failure("Missing environment variable")
        assert CodexAgent._is_auth_failure("missing environment variable: OPENAI_API_KEY")


# ── is_alive / terminate ─────────────────────────────────


class TestLifecycle:
    def test_not_alive_before_start(self):
        assert CodexAgent().is_alive() is False

    def test_alive_when_running(self):
        agent = CodexAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc
        assert agent.is_alive() is True

    def test_not_alive_after_exit(self):
        agent = CodexAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        agent._process = mock_proc
        assert agent.is_alive() is False

    def test_terminate_preserves_session_id(self):
        agent = CodexAgent()
        agent._session_id = "019d-abc"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc
        agent.terminate()
        assert agent._session_id == "019d-abc"
        assert agent._process is None

    def test_send_input_raises(self):
        with pytest.raises(RuntimeError):
            CodexAgent().send_input("hello")


# ── MCP signal tool calls ────────────────────────────────


class TestMCPSignalParsing:
    def _agent(self):
        return CodexAgent()

    def test_mcp_signal_done_buffers_for_usage(self):
        """mcp_tool_call with nightshift_done buffers @@DONE@@ for turn.completed usage."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task complete"},
        )
        ev = agent._parse(raw)
        # Should return None and buffer for turn.completed
        assert ev is None
        assert agent._pending_done_raw == raw

    def test_mcp_signal_checkpoint_emits_checkpoint_marker(self):
        """mcp_tool_call with tool=nightshift_checkpoint → @@CHECKPOINT@@ description."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_checkpoint",
            arguments={"description": "progress"},
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@CHECKPOINT@@ progress"

    def test_mcp_signal_question_emits_question_and_waiting(self):
        """mcp_tool_call with tool=nightshift_question → @@QUESTION@@ then @@WAITING@@."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_question",
            arguments={"question": "What branch?"},
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@QUESTION@@ What branch?"
        # Drain the extra @@WAITING@@ event
        extras = list(agent._drain_extra())
        assert len(extras) == 1
        assert extras[0].content == "@@WAITING@@"

    def test_non_signal_mcp_tool_parsed_as_system(self):
        """mcp_tool_call with a different server is parsed as SYSTEM."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="other-server", tool="some_tool",
            arguments={},
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.SYSTEM
        assert "mcp_tool_call" in ev.content


# ── Usage buffering (@@DONE@@ waits for turn.completed) ─────


class TestUsageBuffering:
    """Tests for buffering @@DONE@@ until turn.completed arrives with usage data."""

    def _agent(self):
        return CodexAgent()

    def _make_runner(self, tmp_path: Path, events: list[AgentEvent]):
        issue = make_test_issue()
        session_dir = tmp_path / "session"
        state_mgr = StateManager(session_dir)
        state_mgr._write(SessionState(issue_id=issue.id, branch="agent/test"))

        agent = MockAgent(events)
        tracker = MockTracker({issue.id: issue})
        notifier = MockNotifier()
        workspace_mgr = MockWorkspaceManager(tmp_path)

        runner = SessionRunner(
            agent=agent, tracker=tracker, notifier=notifier,
            workspace_mgr=workspace_mgr, state_mgr=state_mgr,
            issue=issue, prompt="Fix the bug",
        )
        workspace = Workspace(path=tmp_path / "ws", branch="agent/test", is_new=False)
        workspace.path.mkdir()
        return runner, state_mgr, workspace

    def test_usage_captured_before_done_marker(self):
        """agent_message with @@DONE@@ waits for turn.completed to get usage."""
        agent = self._agent()
        # First: agent_message contains only the @@DONE@@ marker
        msg_raw = _item_ev("item.completed", "agent_message", text="@@DONE@@")
        ev1 = agent._parse(msg_raw)
        # Should return None (buffered)
        assert ev1 is None
        assert agent._pending_done_raw == msg_raw

        # Then: turn.completed arrives with usage
        turn_raw = _ev("turn.completed", usage={
            "input_tokens": 5000, "output_tokens": 200, "cost_usd": 0.05
        })
        ev2 = agent._parse(turn_raw)
        assert ev2 is not None
        assert ev2.type == AgentEventType.TEXT
        assert ev2.content == "@@DONE@@"
        # Raw should be from the buffered @@DONE@@ event
        assert ev2.raw == msg_raw
        # Usage metadata should be attached
        assert ev2.metadata["usage"]["input_tokens"] == 5000
        assert ev2.metadata["usage"]["output_tokens"] == 200
        assert ev2.metadata["usage"]["cost_usd"] == 0.05
        # Buffer should be cleared
        assert agent._pending_done_raw is None

    def test_done_with_verdict_emits_text(self):
        """agent_message with @@DONE@@ and verdict text should not be discarded."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed",
            "agent_message",
            text="All good. @nightshift revise @@DONE@@",
        )
        ev = agent._parse(raw)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "All good. @nightshift revise"
        assert ev.raw == raw
        assert agent._pending_done_raw == raw

    def test_verdict_in_same_message_as_done_logged(self, tmp_path):
        """conversation.jsonl should keep verdict text when @@DONE@@ is in the same message."""
        agent = self._agent()
        text_raw = _item_ev(
            "item.completed",
            "agent_message",
            text="All good. @nightshift approve @@DONE@@",
        )
        text_ev = agent._parse(text_raw)
        assert text_ev is not None

        done_raw = _ev("turn.completed", usage={
            "input_tokens": 100,
            "output_tokens": 10,
        })
        done_ev = agent._parse(done_raw)
        assert done_ev is not None

        runner, state_mgr, workspace = self._make_runner(tmp_path, [text_ev, done_ev])
        runner.run(workspace=workspace)

        entries = [
            json.loads(line)
            for line in state_mgr.conversation_log.read_text().strip().splitlines()
        ]
        assert any(
            e["role"] == "assistant" and "@nightshift approve" in e["content"]
            for e in entries
        )
        st = state_mgr.load_state()
        assert st.status == "waiting:review"
        assert st.usage.input_tokens == 100
        assert st.usage.output_tokens == 10

    def test_usage_from_turn_completed_after_mcp_done(self):
        """MCP nightshift_done waits for turn.completed to get usage."""
        agent = self._agent()
        # First: MCP nightshift_done
        mcp_raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Done"}
        )
        ev1 = agent._parse(mcp_raw)
        assert ev1 is None
        assert agent._pending_done_raw == mcp_raw

        # Then: turn.completed with usage
        turn_raw = _ev("turn.completed", usage={
            "input_tokens": 10000, "output_tokens": 500, "cost_usd": 0.12,
            "model": "gpt-5.4"
        })
        ev2 = agent._parse(turn_raw)
        assert ev2 is not None
        assert ev2.content == "@@DONE@@"
        assert ev2.raw == mcp_raw
        assert ev2.metadata["usage"]["input_tokens"] == 10000
        assert ev2.metadata["usage"]["model"] == "gpt-5.4"

    def test_buffered_done_emitted_on_process_exit(self):
        """If stream ends before turn.completed, buffered @@DONE@@ is emitted via _on_process_exit."""
        agent = self._agent()
        # Buffer a @@DONE@@
        mcp_raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={}
        )
        agent._parse(mcp_raw)
        assert agent._pending_done_raw is not None

        # Process exits without turn.completed
        agent._on_process_exit()

        # Buffered @@DONE@@ should be in extra_events
        extras = list(agent._drain_extra())
        assert len(extras) == 1
        assert extras[0].content == "@@DONE@@"
        assert extras[0].raw == mcp_raw
        # No usage metadata (turn.completed never arrived)
        assert not extras[0].metadata
        # Buffer should be cleared
        assert agent._pending_done_raw is None

    def test_no_buffered_done_on_normal_turn_completed(self):
        """turn.completed without prior @@DONE@@ uses its own raw."""
        agent = self._agent()
        # No prior @@DONE@@ buffered
        assert agent._pending_done_raw is None

        turn_raw = _ev("turn.completed", usage={"input_tokens": 1000, "output_tokens": 50})
        ev = agent._parse(turn_raw)
        assert ev is not None
        assert ev.content == "@@DONE@@"
        # Raw should be from turn.completed itself
        assert ev.raw == turn_raw
        assert ev.metadata["usage"]["input_tokens"] == 1000

    def test_before_stream_clears_pending_done(self):
        """_before_stream() resets pending @@DONE@@ state."""
        agent = self._agent()
        # Buffer a @@DONE@@
        agent._pending_done_raw = "some raw"
        agent._extra_events.append(AgentEvent(type=AgentEventType.TEXT, content="test"))

        # Reset state
        agent._before_stream()

        assert agent._pending_done_raw is None
        assert len(agent._extra_events) == 0


# ── stream_events() / JSONL parsing ───────────────────────


class TestStreamParsing:
    def test_stream_yields_agent_events(self):
        agent = CodexAgent()
        lines = [
            _ev("thread.started", thread_id="019d-abc-123") + "\n",
            _item_ev("item.started", "command_execution", command="pwd", status="in_progress") + "\n",
            _item_ev(
                "item.completed",
                "command_execution",
                command="pwd",
                aggregated_output="/workspace\n",
                exit_code=0,
                status="completed",
            ) + "\n",
            _ev("turn.completed", usage={"input_tokens": 120, "output_tokens": 8}) + "\n",
        ]

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter(lines)
        mock_proc.returncode = 0
        agent._process = mock_proc
        agent._last_event = 0

        events = list(agent.stream_events())

        assert all(isinstance(event, AgentEvent) for event in events)
        assert [event.type for event in events] == [
            AgentEventType.SYSTEM,
            AgentEventType.TOOL_CALL,
            AgentEventType.TOOL_RESULT,
            AgentEventType.TEXT,
            AgentEventType.PROCESS_EXIT,
        ]
        assert events[0].content == "thread:019d-abc-123"
        assert events[2].content.startswith("exit=0")
        assert events[3].content == "@@DONE@@"
        assert events[3].metadata["usage"]["input_tokens"] == 120

    def test_jsonl_parsed_to_events(self):
        agent = CodexAgent()
        raw_events = [
            _ev("thread.started", thread_id="019d-abc-123"),
            _item_ev("item.started", "command_execution", command="ls", status="in_progress"),
            _item_ev(
                "item.completed",
                "command_execution",
                command="ls",
                aggregated_output="a.py\nb.py\n",
                exit_code=0,
                status="completed",
            ),
            _ev("turn.completed", usage={"input_tokens": 42, "output_tokens": 3}),
        ]

        events = [agent._parse(raw) for raw in raw_events]

        assert all(isinstance(event, AgentEvent) for event in events if event is not None)
        assert [event.type for event in events if event is not None] == [
            AgentEventType.SYSTEM,
            AgentEventType.TOOL_CALL,
            AgentEventType.TOOL_RESULT,
            AgentEventType.TEXT,
        ]
        assert events[0].raw == raw_events[0]
        assert events[1].content == "ls"
        assert events[2].content.startswith("exit=0")
        assert events[3].metadata["usage"]["output_tokens"] == 3


# ── Registry ─────────────────────────────────────────────


class TestRegistry:
    def test_codex_in_agent_registry(self):
        from core.config.factories import AGENT_REGISTRY
        assert "codex" in AGENT_REGISTRY

    def test_codex_instantiable(self):
        from core.config.factories import AGENT_REGISTRY
        module_path, class_name = AGENT_REGISTRY["codex"]
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        agent = cls()
        assert isinstance(agent, CodexAgent)
