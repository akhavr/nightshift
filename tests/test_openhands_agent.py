"""Tests for adapters.agents.openhands — OpenHands agent adapter."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from adapters.agents.openhands import OpenHandsAgent
from core.protocols import AgentEventType


@pytest.fixture
def agent():
    return OpenHandsAgent(command="openhands-cli", stall_timeout_s=300)


class TestInit:
    def test_defaults(self):
        a = OpenHandsAgent()
        assert a.command == "openhands-cli"
        assert a.stall_timeout_s == 300.0
        assert a.extra_args == []
        assert a.pid is None

    def test_custom_args(self):
        a = OpenHandsAgent(
            command="/usr/bin/openhands",
            stall_timeout_s=60,
            extra_args=["--verbose"],
        )
        assert a.command == "/usr/bin/openhands"
        assert a.stall_timeout_s == 60
        assert a.extra_args == ["--verbose"]


class TestStart:
    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_start_launches_process(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent.start("fix the bug", Path("/workspace"), max_turns=10)

        mock_popen.assert_called_once()
        args = mock_popen.call_args
        cmd = args[0][0]
        assert cmd[0] == "openhands-cli"
        assert "run" in cmd
        assert "--prompt" in cmd
        assert "fix the bug" in cmd
        assert "--max-turns" in cmd
        assert "10" in cmd
        assert agent.pid == 42

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_start_includes_extra_args(self, mock_popen):
        agent = OpenHandsAgent(extra_args=["--model", "gpt-4"])
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert "gpt-4" in cmd

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_start_uses_stderr_pipe(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["stderr"] == subprocess.PIPE

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_stderr_is_logged(self, mock_popen, agent, caplog):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.stderr = iter(["warning: something bad\n", "error: fatal\n"])
        mock_popen.return_value = mock_proc

        import logging
        with caplog.at_level(logging.WARNING):
            agent.start("test", Path("/ws"))
            agent._stderr_thread.join(timeout=5)

        assert "something bad" in caplog.text
        assert "error: fatal" in caplog.text


class TestSendInput:
    def test_send_input_raises(self, agent):
        with pytest.raises(RuntimeError, match="send_input.*not supported"):
            agent.send_input("hello")


class TestIsAlive:
    def test_no_process(self, agent):
        assert agent.is_alive() is False

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_running_process(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))
        assert agent.is_alive() is True

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_exited_process(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))
        assert agent.is_alive() is False


class TestTerminate:
    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_terminate_running_process(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))
        agent.terminate()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()
        assert agent.pid is None
        assert agent._process is None

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_terminate_escalates_to_kill(self, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 10), None]
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))
        agent.terminate()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_terminate_no_process(self, agent):
        # Should not raise
        agent.terminate()


class TestParse:
    def test_empty_line(self, agent):
        assert agent._parse("") is None
        assert agent._parse("   ") is None

    def test_invalid_json(self, agent):
        assert agent._parse("not json") is None

    def test_action_message(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "message", "message": "thinking about it",
        }))
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "thinking about it"

    def test_action_run(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "run", "message": "ls -la",
        }))
        assert ev.type == AgentEventType.TOOL_CALL
        assert "run" in ev.content
        assert "ls -la" in ev.content

    def test_action_write(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "write", "message": "writing file",
        }))
        assert ev.type == AgentEventType.TOOL_CALL

    def test_action_read(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "read", "message": "reading config",
        }))
        assert ev.type == AgentEventType.TOOL_CALL

    def test_action_browse(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "browse", "message": "checking docs",
        }))
        assert ev.type == AgentEventType.TOOL_CALL

    def test_action_finish(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "finish", "message": "done",
        }))
        assert ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in ev.content

    def test_action_unknown(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "delegate", "message": "delegating",
        }))
        assert ev.type == AgentEventType.SYSTEM
        assert "delegate" in ev.content

    def test_observation(self, agent):
        ev = agent._parse(json.dumps({
            "type": "observation", "content": "file contents here",
        }))
        assert ev.type == AgentEventType.TOOL_RESULT
        assert ev.content == "file contents here"

    def test_observation_truncated(self, agent):
        long_content = "x" * 1000
        ev = agent._parse(json.dumps({
            "type": "observation", "content": long_content,
        }))
        assert len(ev.content) == 500

    def test_status_complete(self, agent):
        ev = agent._parse(json.dumps({
            "type": "status", "status": "complete", "message": "all done",
        }))
        assert ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in ev.content

    def test_status_running(self, agent):
        ev = agent._parse(json.dumps({
            "type": "status", "status": "running", "message": "step 3",
        }))
        assert ev.type == AgentEventType.SYSTEM
        assert "running" in ev.content

    def test_error_event(self, agent):
        ev = agent._parse(json.dumps({
            "type": "error", "message": "something broke",
        }))
        assert ev.type == AgentEventType.SYSTEM
        assert "error" in ev.content
        assert "something broke" in ev.content

    def test_unknown_event_type(self, agent):
        ev = agent._parse(json.dumps({
            "type": "unknown_thing", "data": 123,
        }))
        assert ev.type == AgentEventType.UNKNOWN

    def test_action_run_no_message(self, agent):
        ev = agent._parse(json.dumps({
            "type": "action", "action": "run",
        }))
        assert ev.type == AgentEventType.TOOL_CALL
        assert ev.content == "run"


class TestStreamEvents:
    @patch("adapters.agents.openhands.subprocess.Popen")
    @patch("adapters.agents.openhands.select.select")
    def test_stream_process_exit_on_empty_read(self, mock_select, mock_popen, agent):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_stdout = MagicMock()
        mock_stdout.readline.return_value = ""
        mock_proc.stdout = mock_stdout
        mock_popen.return_value = mock_proc
        mock_select.return_value = ([mock_stdout], [], [])

        agent.start("test", Path("/ws"))
        events = list(agent.stream_events())

        assert events[-1].type == AgentEventType.PROCESS_EXIT

    @patch("adapters.agents.openhands.subprocess.Popen")
    def test_stream_drains_on_process_exit(self, mock_popen, agent):
        lines = [
            json.dumps({"type": "action", "action": "message", "message": "hi"}) + "\n",
            json.dumps({"type": "action", "action": "finish"}) + "\n",
        ]
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = 0
        mock_proc.stdout = iter(lines)
        mock_popen.return_value = mock_proc

        agent.start("test", Path("/ws"))
        events = list(agent.stream_events())

        types = [e.type for e in events]
        assert AgentEventType.TEXT in types
        assert AgentEventType.PROCESS_EXIT in types

    @patch("adapters.agents.openhands.time.monotonic")
    @patch("adapters.agents.openhands.subprocess.Popen")
    @patch("adapters.agents.openhands.select.select")
    def test_stall_detection(self, mock_select, mock_popen, mock_time, agent):
        agent.stall_timeout_s = 10

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_popen.return_value = mock_proc

        # select returns empty (timeout), time shows elapsed > stall_timeout
        mock_select.return_value = ([], [], [])
        mock_time.side_effect = [0, 0, 20]  # start, start again, check

        agent.start("test", Path("/ws"))
        events = list(agent.stream_events())

        assert any(e.type == AgentEventType.STALL for e in events)

    def test_stream_no_process(self, agent):
        events = list(agent.stream_events())
        assert events == []


class TestRegistration:
    def test_openhands_in_agent_registry(self):
        from core.config.factories import AGENT_REGISTRY
        assert "openhands" in AGENT_REGISTRY
        module_path, class_name = AGENT_REGISTRY["openhands"]
        assert module_path == "adapters.agents.openhands"
        assert class_name == "OpenHandsAgent"

    def test_create_agent_openhands(self):
        from core.config.factories import create_agent
        from core.config.models import AgentConfig, WorkflowConfig

        cfg = WorkflowConfig(agent=AgentConfig(kind="openhands"))
        agent = create_agent(cfg)
        assert isinstance(agent, OpenHandsAgent)
