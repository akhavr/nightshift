"""Tests for adapters/agents/opencode.py — OpenCodeAgent adapter."""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.agents.opencode import OpenCodeAgent
from core.protocols import AgentEvent, AgentEventType


# ── Helpers ────────────────────────────────────────────────


def _ev(event_type: str, **extra) -> str:
    """Build a JSON event string."""
    d = {"type": event_type}
    d.update(extra)
    return json.dumps(d)


# ── Constructor ────────────────────────────────────────────


class TestConstructor:
    def test_default_command(self):
        agent = OpenCodeAgent()
        assert agent.command == "opencode"

    def test_custom_command(self):
        agent = OpenCodeAgent(command="/usr/local/bin/opencode")
        assert agent.command == "/usr/local/bin/opencode"

    def test_extra_args(self):
        agent = OpenCodeAgent(extra_args=["--model", "gpt-4"])
        assert agent.extra_args == ["--model", "gpt-4"]

    def test_stall_timeout(self):
        agent = OpenCodeAgent(stall_timeout_s=120)
        assert agent.stall_timeout_s == 120


# ── start() ───────────────────────────────────────────────


class TestStart:
    @patch("adapters.agents.opencode.subprocess.Popen")
    def test_start_builds_correct_command(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = OpenCodeAgent()
        agent.start("do stuff", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd
        assert "json" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "do stuff" in cmd
        assert "--session" not in cmd
        assert agent.pid == 42

    @patch("adapters.agents.opencode.subprocess.Popen")
    def test_start_resume_uses_session_id(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 99
        mock_popen.return_value = mock_proc

        agent = OpenCodeAgent()
        agent._session_id = "sess-abc-123"
        agent.start("continue", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "--session" in cmd
        assert "sess-abc-123" in cmd
        assert "continue" in cmd

    @patch("adapters.agents.opencode.subprocess.Popen")
    def test_start_passes_extra_args(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent = OpenCodeAgent(extra_args=["--model", "claude-3"])
        agent.start("task", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert "claude-3" in cmd

    @patch("adapters.agents.opencode.subprocess.Popen")
    def test_start_uses_workspace_as_cwd(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent = OpenCodeAgent()
        agent.start("task", Path("/my/workspace"))

        kwargs = mock_popen.call_args[1]
        assert kwargs["cwd"] == "/my/workspace"


# ── _parse() — text events ───────────────────────────────


class TestParseTextEvent:
    def _agent(self):
        return OpenCodeAgent()

    def test_parse_text_event(self):
        agent = self._agent()
        raw = _ev("text", text="I'll create the file.")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TEXT
        assert "create the file" in ev.content

    def test_parse_text_content_field(self):
        """Also supports 'content' field instead of 'text'."""
        agent = self._agent()
        raw = _ev("text", content="Using content field")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TEXT
        assert "Using content field" in ev.content

    def test_parse_text_empty_returns_none(self):
        agent = self._agent()
        raw = _ev("text", text="")
        ev = agent._parse(raw)
        assert ev is None


# ── _parse() — tool_use events ───────────────────────────


class TestParseToolUseEvent:
    def _agent(self):
        return OpenCodeAgent()

    def test_parse_tool_use_event(self):
        agent = self._agent()
        raw = _ev("tool_use", name="read_file", input={"path": "/foo.py"})
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_CALL
        assert "read_file" in ev.content
        assert "/foo.py" in ev.content

    def test_parse_tool_use_with_tool_field(self):
        """Also supports 'tool' field instead of 'name'."""
        agent = self._agent()
        raw = _ev("tool_use", tool="write_file", arguments={"path": "/bar.py"})
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_CALL
        assert "write_file" in ev.content

    def test_parse_tool_use_part_wrapped(self):
        """Extracts tool name and input from part.tool/part.state.input wrapper."""
        agent = self._agent()
        raw = json.dumps({
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"input": {"command": "ls -la"}}
            }
        })
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_CALL
        assert "bash" in ev.content
        assert "ls -la" in ev.content

    def test_parse_tool_result(self):
        agent = self._agent()
        raw = _ev("tool_result", result="file contents here")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.TOOL_RESULT
        assert "file contents" in ev.content


# ── _parse() — step_finish events ────────────────────────


class TestParseStepFinish:
    def _agent(self):
        return OpenCodeAgent()

    def test_parse_step_finish_stop_emits_system(self):
        """step_finish with reason='stop' emits SYSTEM, NOT @@DONE@@.

        Note: reason='stop' just means current step finished without tool calls,
        NOT that the agent completed its task. True completion is signaled by
        process exit or file signals (/session/signal/done).
        """
        agent = self._agent()
        raw = _ev("step_finish", reason="stop")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "step_finish:stop" in ev.content
        assert "@@DONE@@" not in ev.content

    def test_parse_step_finish_tool_calls(self):
        """step_finish with reason='tool-calls' emits SYSTEM event."""
        agent = self._agent()
        raw = _ev("step_finish", reason="tool-calls")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "step_finish:tool-calls" in ev.content

    def test_parse_step_finish_other_reason(self):
        """step_finish with any reason emits SYSTEM event."""
        agent = self._agent()
        raw = _ev("step_finish", reason="tool_use")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "step_finish:tool_use" in ev.content

    def test_extracts_usage_metadata(self):
        agent = self._agent()
        raw = _ev("step_finish", reason="stop", part={
            "tokens": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cost_usd": 0.05,
                "model": "gpt-4"
            }
        })
        ev = agent._parse(raw)
        assert ev.metadata["usage"]["input_tokens"] == 1000
        assert ev.metadata["usage"]["output_tokens"] == 200
        assert ev.metadata["usage"]["cost_usd"] == 0.05
        assert ev.metadata["usage"]["model"] == "gpt-4"

    def test_extracts_usage_from_top_level_tokens(self):
        """Usage can also be at top level of step_finish event."""
        agent = self._agent()
        raw = _ev("step_finish", reason="stop", tokens={
            "input": 500,
            "output": 100,
            "cost": 0.02,
        })
        ev = agent._parse(raw)
        assert ev.metadata["usage"]["input_tokens"] == 500
        assert ev.metadata["usage"]["output_tokens"] == 100
        assert ev.metadata["usage"]["cost_usd"] == 0.02


class TestSignalMethodOutput:
    def _done_file(self) -> Path:
        return Path("/session/signal/done")

    @pytest.mark.parametrize(
        ("signal_method", "expected_types", "expect_file"),
        [
            ("file", [AgentEventType.SYSTEM, AgentEventType.PROCESS_EXIT], True),
            ("text", [AgentEventType.SYSTEM, AgentEventType.TEXT, AgentEventType.PROCESS_EXIT], False),
            ("mcp", [AgentEventType.SYSTEM, AgentEventType.DONE, AgentEventType.PROCESS_EXIT], False),
            ("auto", [AgentEventType.SYSTEM, AgentEventType.TEXT, AgentEventType.DONE, AgentEventType.PROCESS_EXIT], True),
        ],
    )
    def test_signal_method_completion_outputs(self, signal_method, expected_types, expect_file):
        done_file = self._done_file()
        done_file.unlink(missing_ok=True)

        agent = OpenCodeAgent(signal_method=signal_method)
        lines = [_ev("step_finish", reason="stop") + "\n"]

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter(lines)
        mock_proc.returncode = 0
        agent._process = mock_proc
        agent._last_event = time.monotonic()

        events = list(agent.stream_events())

        assert [event.type for event in events] == expected_types
        assert events[0].content == "step_finish:stop"
        assert events[-1].type == AgentEventType.PROCESS_EXIT
        assert done_file.exists() is expect_file
        done_file.unlink(missing_ok=True)


# ── _parse() — error events ──────────────────────────────


class TestParseErrorEvent:
    def _agent(self):
        return OpenCodeAgent()

    def test_parse_error_event(self):
        agent = self._agent()
        raw = _ev("error", message="something went wrong")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "something went wrong" in ev.content

    def test_parse_error_with_error_field(self):
        """Also supports 'error' field instead of 'message'."""
        agent = self._agent()
        raw = _ev("error", error="network failure")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.SYSTEM
        assert "network failure" in ev.content


# ── _is_auth_failure() ───────────────────────────────────


class TestAuthFailureDetection:
    def test_auth_failure_detection(self):
        assert OpenCodeAgent._is_auth_failure("invalid_api_key")
        assert OpenCodeAgent._is_auth_failure("authentication_error: bad key")
        assert OpenCodeAgent._is_auth_failure("status 401 Unauthorized")
        assert OpenCodeAgent._is_auth_failure("status 403 Forbidden")

    def test_rate_limit_not_auth_failure(self):
        """Rate limit errors are handled as transient errors, not auth failures."""
        # These are now handled by HeadlessAgentBase._is_transient_error()
        assert not OpenCodeAgent._is_auth_failure("rate limit exceeded")
        assert not OpenCodeAgent._is_auth_failure("rate_limit_exceeded")

    def test_detects_insufficient_quota(self):
        assert OpenCodeAgent._is_auth_failure("insufficient_quota for this request")

    def test_ignores_500(self):
        assert not OpenCodeAgent._is_auth_failure("status 500 internal server error")

    def test_ignores_normal_output(self):
        assert not OpenCodeAgent._is_auth_failure("Created file hello.py")

    def test_case_insensitive(self):
        assert OpenCodeAgent._is_auth_failure("UNAUTHORIZED access denied")

    def test_auth_failure_event_from_error(self):
        agent = OpenCodeAgent()
        raw = _ev("error", message="status 401 Unauthorized: Missing API key")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.AUTH_FAILURE


# ── Session ID extraction ────────────────────────────────


class TestExtractsSessionId:
    def test_extracts_session_id(self):
        agent = OpenCodeAgent()
        assert agent._session_id is None
        raw = _ev("session", sessionID="sess-xyz-789")
        ev = agent._parse(raw)
        assert agent._session_id == "sess-xyz-789"
        assert ev.type == AgentEventType.SYSTEM

    def test_extracts_session_id_from_snake_case(self):
        """Also supports session_id field."""
        agent = OpenCodeAgent()
        raw = _ev("text", text="hello", session_id="sess-abc-123")
        agent._parse(raw)
        assert agent._session_id == "sess-abc-123"

    def test_does_not_override_existing_session_id(self):
        agent = OpenCodeAgent()
        agent._session_id = "existing-id"
        raw = _ev("text", text="hello", sessionID="new-id")
        agent._parse(raw)
        assert agent._session_id == "existing-id"


# ── _parse() — edge cases ────────────────────────────────


class TestParseEdgeCases:
    def _agent(self):
        return OpenCodeAgent()

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
        raw = _ev("custom_event")
        ev = agent._parse(raw)
        assert ev.type == AgentEventType.UNKNOWN


# ── is_alive / terminate ─────────────────────────────────


class TestLifecycle:
    def test_not_alive_before_start(self):
        assert OpenCodeAgent().is_alive() is False

    def test_alive_when_running(self):
        agent = OpenCodeAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc
        assert agent.is_alive() is True

    def test_not_alive_after_exit(self):
        agent = OpenCodeAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        agent._process = mock_proc
        assert agent.is_alive() is False

    def test_terminate_preserves_session_id(self):
        agent = OpenCodeAgent()
        agent._session_id = "sess-abc"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc
        agent.terminate()
        assert agent._session_id == "sess-abc"
        assert agent._process is None

    def test_send_input_raises(self):
        with pytest.raises(RuntimeError):
            OpenCodeAgent().send_input("hello")


# ── Registry ─────────────────────────────────────────────


class TestRegistry:
    def test_opencode_in_agent_registry(self):
        from core.config.factories import AGENT_REGISTRY
        assert "opencode" in AGENT_REGISTRY

    def test_opencode_instantiable(self):
        from core.config.factories import AGENT_REGISTRY
        module_path, class_name = AGENT_REGISTRY["opencode"]
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        agent = cls()
        assert isinstance(agent, OpenCodeAgent)
