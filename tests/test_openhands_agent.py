"""Tests for adapters.agents.openhands — OpenHands agent adapter.

REQ: REQ-030
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.protocols import AgentEvent, AgentEventType


# ── Helpers ──────────────────────────────────────────────────

def _action_event(action_type: str, **extra) -> str:
    """Build a JSON ActionEvent string."""
    ev = {"kind": "ActionEvent", "source": "agent", "action_type": action_type}
    ev.update(extra)
    return json.dumps(ev)


def _observation_event(obs_type: str = "CmdOutputObservation", **extra) -> str:
    """Build a JSON ObservationEvent string."""
    ev = {"kind": "ObservationEvent", "source": "environment", "observation_type": obs_type}
    ev.update(extra)
    return json.dumps(ev)


def _message_event(source: str = "user", **extra) -> str:
    """Build a JSON MessageEvent string."""
    ev = {"kind": "MessageEvent", "source": source}
    ev.update(extra)
    return json.dumps(ev)


EVENT_SEPARATOR = "--JSON Event--"


# ── Constructor ──────────────────────────────────────────────


class TestConstructor:
    def test_defaults(self):
        from adapters.agents.openhands import OpenHandsAgent
        from adapters.agents.base import STALL_TIMEOUT_S

        agent = OpenHandsAgent()
        assert agent.command == "openhands"
        assert agent.stall_timeout_s == STALL_TIMEOUT_S
        assert agent.extra_args == []

    def test_custom_args(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent(
            command="/usr/local/bin/openhands",
            stall_timeout_s=60,
            extra_args=["--workspace-dir", "/tmp"],
        )
        assert agent.command == "/usr/local/bin/openhands"
        assert agent.stall_timeout_s == 60
        assert agent.extra_args == ["--workspace-dir", "/tmp"]


# ── start() ──────────────────────────────────────────────────


class TestStart:
    def test_builds_correct_command(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent(extra_args=["--debug"])
        with patch("adapters.agents.openhands.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 42
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            agent.start("Fix the bug", Path("/workspace"))

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd == [
            "openhands", "--headless", "--json", "--always-approve",
            "--override-with-envs",
            "--debug",
            "-t", "Fix the bug",
        ]

    def test_inherits_parent_env(self):
        """Subprocess inherits parent env (no env= kwarg), so LLM_* vars pass through."""
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        with patch("adapters.agents.openhands.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 42
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            agent.start("test", Path("/workspace"))

        # No env= kwarg means subprocess inherits parent environment
        assert "env" not in mock_popen.call_args[1]

    def test_resume_includes_flag(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        agent._session_id = "conv-abc-123"
        with patch("adapters.agents.openhands.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 42
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            agent.start("Continue working", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "conv-abc-123"

    def test_sets_pid(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        with patch("adapters.agents.openhands.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            agent.start("test", Path("/workspace"))

        assert agent.pid == 99

    def test_sets_cwd(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        with patch("adapters.agents.openhands.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 1
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            agent.start("test", Path("/my/workspace"))

        assert mock_popen.call_args[1]["cwd"] == "/my/workspace"


# ── _parse() ─────────────────────────────────────────────────


class TestParse:
    def _agent(self):
        from adapters.agents.openhands import OpenHandsAgent
        return OpenHandsAgent()

    def test_checkpoint_for_file_editor_action(self):
        agent = self._agent()
        raw = _action_event("FileEditorAction", summary="Created main.py")
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert "@@CHECKPOINT@@" in ev.content
        assert "Created main.py" in ev.content

    def test_done_for_finish_action(self):
        agent = self._agent()
        raw = _action_event("FinishAction")
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in ev.content

    def test_log_for_reasoning_content(self):
        agent = self._agent()
        raw = json.dumps({
            "kind": "ActionEvent", "source": "agent",
            "action_type": "MessageAction",
            "reasoning_content": "Thinking about the problem...",
        })
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert "@@LOG@@" in ev.content
        assert "Thinking about the problem" in ev.content

    def test_tool_call_for_terminal_action(self):
        agent = self._agent()
        raw = _action_event("TerminalAction", command="ls -la")
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TOOL_CALL
        assert "ls -la" in ev.content

    def test_tool_result_for_observation(self):
        agent = self._agent()
        raw = _observation_event("CmdOutputObservation", content="file1.py\nfile2.py")
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TOOL_RESULT
        assert "file1.py" in ev.content

    def test_message_event_returns_text(self):
        agent = self._agent()
        raw = _message_event(source="user", content="Please fix the bug")
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TEXT

    def test_empty_line_returns_none(self):
        agent = self._agent()
        assert agent._parse("") is None
        assert agent._parse("   ") is None

    def test_separator_line_returns_none(self):
        agent = self._agent()
        assert agent._parse(EVENT_SEPARATOR) is None

    def test_unparseable_json_falls_back_to_text(self):
        agent = self._agent()
        ev = agent._parse("not valid json at all")
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert "not valid json at all" in ev.content

    def test_unknown_action_type(self):
        agent = self._agent()
        raw = _action_event("BrowseAction", url="http://example.com")
        ev = agent._parse(raw)
        assert ev is not None
        # Should still produce an event (TEXT for unknown action types)
        assert ev.type == AgentEventType.TEXT

    def test_checkpoint_without_summary(self):
        agent = self._agent()
        raw = _action_event("FileEditorAction")
        ev = agent._parse(raw)
        assert ev is not None
        assert "@@CHECKPOINT@@" in ev.content

    def test_finish_action_not_shadowed_by_reasoning(self):
        """FinishAction must emit @@DONE@@ even when reasoning_content is present."""
        agent = self._agent()
        raw = json.dumps({
            "kind": "ActionEvent", "source": "agent",
            "action_type": "FinishAction",
            "reasoning_content": "I'm done with the task.",
        })
        ev = agent._parse(raw)
        assert ev is not None
        assert "@@DONE@@" in ev.content
        assert "@@LOG@@" not in ev.content

    def test_checkpoint_not_shadowed_by_reasoning(self):
        """FileEditorAction must emit @@CHECKPOINT@@ even when reasoning_content is present."""
        agent = self._agent()
        raw = json.dumps({
            "kind": "ActionEvent", "source": "agent",
            "action_type": "FileEditorAction",
            "summary": "edited file",
            "reasoning_content": "Thinking about the edit...",
        })
        ev = agent._parse(raw)
        assert ev is not None
        assert "@@CHECKPOINT@@" in ev.content
        assert "@@LOG@@" not in ev.content

    def test_terminal_action_not_shadowed_by_reasoning(self):
        """TerminalAction must emit TOOL_CALL even when reasoning_content is present."""
        agent = self._agent()
        raw = json.dumps({
            "kind": "ActionEvent", "source": "agent",
            "action_type": "TerminalAction",
            "command": "ls -la",
            "reasoning_content": "Let me check the directory...",
        })
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TOOL_CALL
        assert "ls -la" in ev.content
        assert "@@LOG@@" not in ev.content


# ── stream_events() ──────────────────────────────────────────


class TestStreamEvents:
    def test_parses_events_separated_by_marker(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        lines = [
            _action_event("TerminalAction", command="pwd") + "\n",
            EVENT_SEPARATOR + "\n",
            _observation_event(content="/workspace") + "\n",
            EVENT_SEPARATOR + "\n",
            _action_event("FinishAction") + "\n",
        ]

        mock_proc = MagicMock()
        # poll returns 0 immediately so stream_events drains via the
        # "process exited" path, avoiding select.select on a mock.
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter(lines)
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        agent._process = mock_proc
        agent._last_event = time.monotonic()

        events = list(agent.stream_events())
        types = [e.type for e in events]
        assert AgentEventType.TOOL_CALL in types
        assert AgentEventType.TOOL_RESULT in types
        assert AgentEventType.PROCESS_EXIT in types

    def test_process_exit_on_end(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter([])
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        agent._process = mock_proc
        agent._last_event = time.monotonic()

        events = list(agent.stream_events())
        assert any(e.type == AgentEventType.PROCESS_EXIT for e in events)

    def test_session_id_extraction_from_stderr(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter([])
        mock_proc.stderr.read.return_value = "Conversation ID: abc-def-123\n"
        mock_proc.returncode = 0
        agent._process = mock_proc
        agent._last_event = time.monotonic()

        list(agent.stream_events())
        assert agent._session_id == "abc-def-123"

    def test_no_events_when_no_process(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        events = list(agent.stream_events())
        assert events == []


# ── Stall detection ──────────────────────────────────────────


class TestStallDetection:
    def test_stall_emitted_after_timeout(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent(stall_timeout_s=0.1)
        mock_proc = MagicMock()
        # poll returns None (process alive), then stdout blocks (select returns empty)
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.fileno.return_value = 3
        agent._process = mock_proc
        agent._last_event = time.monotonic() - 1.0  # already past stall

        with patch("adapters.agents.base.select.select", return_value=([], [], [])):
            events = list(agent.stream_events())

        assert any(e.type == AgentEventType.STALL for e in events)


# ── terminate() ──────────────────────────────────────────────


class TestTerminate:
    def test_kills_process(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc

        agent.terminate()

        mock_proc.terminate.assert_called_once()
        assert agent._process is None
        assert agent._pid is None

    def test_preserves_session_id(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        agent._session_id = "keep-me"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc

        agent.terminate()
        assert agent._session_id == "keep-me"

    def test_noop_when_no_process(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        agent.terminate()  # should not raise


# ── send_input() ─────────────────────────────────────────────


class TestSendInput:
    def test_raises_runtime_error(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        with pytest.raises(RuntimeError, match="send_input.*not supported"):
            agent.send_input("hello")


# ── is_alive() ───────────────────────────────────────────────


class TestIsAlive:
    def test_alive_when_running(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        agent._process = mock_proc
        assert agent.is_alive() is True

    def test_not_alive_when_no_process(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        assert agent.is_alive() is False

    def test_not_alive_when_exited(self):
        from adapters.agents.openhands import OpenHandsAgent

        agent = OpenHandsAgent()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        agent._process = mock_proc
        assert agent.is_alive() is False


# ── _is_auth_failure() ─────────────────────────────────────


class TestIsAuthFailure:
    def _agent(self):
        from adapters.agents.openhands import OpenHandsAgent
        return OpenHandsAgent()

    def test_detects_401_invalid_key(self):
        agent = self._agent()
        assert agent._is_auth_failure("AuthenticationError: Error code: 401 - Invalid API key")

    def test_429_rate_limit_not_auth_failure(self):
        """429/rate limit errors are handled as transient errors, not auth failures."""
        agent = self._agent()
        # These are now handled by HeadlessAgentBase._is_transient_error()
        assert not agent._is_auth_failure("Rate limit exceeded: Error code: 429")

    def test_detects_404_model_not_found(self):
        agent = self._agent()
        assert agent._is_auth_failure("NotFoundError: Error code: 404 - model not found")

    def test_detects_invalid_api_key_text(self):
        agent = self._agent()
        assert agent._is_auth_failure("Incorrect API key provided: sk-proj-****")

    def test_detects_litellm_auth_error(self):
        agent = self._agent()
        assert agent._is_auth_failure("litellm.AuthenticationError: invalid api key")

    def test_detects_connection_error(self):
        agent = self._agent()
        assert agent._is_auth_failure("Connection error to LLM provider: refused")

    def test_detects_litellm_prefix(self):
        agent = self._agent()
        assert agent._is_auth_failure("litellm.RateLimitError: you exceeded your quota")

    def test_ignores_500_server_error(self):
        agent = self._agent()
        assert not agent._is_auth_failure("Error code: 500 - Internal server error")

    def test_ignores_timeout(self):
        agent = self._agent()
        assert not agent._is_auth_failure("Request timed out after 30 seconds")

    def test_ignores_normal_output(self):
        agent = self._agent()
        assert not agent._is_auth_failure("Successfully completed task")

    def test_case_insensitive(self):
        agent = self._agent()
        assert agent._is_auth_failure("AUTHENTICATION_ERROR: Invalid Key")


# ── AUTH_FAILURE in _parse() ────────────────────────────────


class TestParseAuthFailure:
    def _agent(self):
        from adapters.agents.openhands import OpenHandsAgent
        return OpenHandsAgent()

    def test_observation_error_with_auth_pattern(self):
        """ObservationEvent with is_error=true and auth content emits AUTH_FAILURE."""
        agent = self._agent()
        raw = _observation_event(
            "ErrorObservation",
            content="AuthenticationError: Error code: 401 - Invalid API key",
            is_error=True,
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE
        assert "401" in ev.content

    def test_observation_error_with_429(self):
        """ObservationEvent with rate limit error emits AUTH_FAILURE."""
        agent = self._agent()
        raw = _observation_event(
            "ErrorObservation",
            content="litellm.RateLimitError: Error code: 429",
            is_error=True,
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_observation_error_non_auth_stays_tool_result(self):
        """ObservationEvent with is_error=true but non-auth content stays TOOL_RESULT."""
        agent = self._agent()
        raw = _observation_event(
            "ErrorObservation",
            content="Error code: 500 - Internal server error",
            is_error=True,
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TOOL_RESULT

    def test_observation_without_error_flag_not_auth_failure(self):
        """ObservationEvent without is_error=true is never AUTH_FAILURE."""
        agent = self._agent()
        raw = _observation_event(
            "CmdOutputObservation",
            content="Error code: 401 - but this is just command output",
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.TOOL_RESULT

    def test_observation_error_with_litellm_message(self):
        """litellm error messages in ObservationEvent trigger AUTH_FAILURE."""
        agent = self._agent()
        raw = _observation_event(
            "ErrorObservation",
            content="litellm.AuthenticationError: OpenAIException - Incorrect API key",
            is_error=True,
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_observation_error_with_connection_error(self):
        """Connection error to LLM provider triggers AUTH_FAILURE."""
        agent = self._agent()
        raw = _observation_event(
            "ErrorObservation",
            content="Connection error to LLM provider: connection refused",
            is_error=True,
        )
        ev = agent._parse(raw)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE
