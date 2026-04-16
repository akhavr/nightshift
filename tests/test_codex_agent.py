"""Tests for adapters/agents/codex.py — CodexAgent adapter."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.agents.codex import CodexAgent
from core.protocols import AgentEvent, AgentEventType


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

    def test_mcp_signal_done_emits_done_marker(self):
        """mcp_tool_call with server=nightshift-signals, tool=nightshift_done → @@DONE@@."""
        agent = self._agent()
        raw = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task complete"},
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@DONE@@"

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
