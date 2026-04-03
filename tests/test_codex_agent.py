"""Tests for the Codex CLI agent adapter (REQ-033)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from adapters.agents.codex import CodexAgent, _in_docker, DOCKERENV_PATH


class TestCodexAgentStart:

    @patch("adapters.agents.codex._in_docker", return_value=True)
    @patch("subprocess.Popen")
    def test_uses_bypass_flag_in_docker(self, mock_popen, mock_docker):
        """Inside Docker, uses --dangerously-bypass-approvals-and-sandbox."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent.start("fix the bug", Path("/workspace"), max_turns=10)

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd
        assert "fix the bug" in cmd

    @patch("adapters.agents.codex._in_docker", return_value=False)
    @patch("subprocess.Popen")
    def test_uses_full_auto_on_host(self, mock_popen, mock_docker):
        """On the host, uses --full-auto."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = CodexAgent()
        agent.start("fix the bug", Path("/workspace"), max_turns=10)

        cmd = mock_popen.call_args[0][0]
        assert "--full-auto" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    @patch("adapters.agents.codex._in_docker", return_value=True)
    @patch("subprocess.Popen")
    def test_extra_args_passed(self, mock_popen, mock_docker):
        """Extra args from config are included in the command."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        agent = CodexAgent(extra_args=["--model", "o3"])
        agent.start("do stuff", Path("/workspace"))

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert "o3" in cmd


class TestCodexAgentParse:

    def test_parse_json_message(self):
        agent = CodexAgent()
        ev = agent._parse('{"type": "message", "content": "hello"}')
        assert ev is not None
        assert ev.content == "hello"

    def test_parse_json_error(self):
        from core.protocols import AgentEventType
        agent = CodexAgent()
        ev = agent._parse('{"type": "error", "message": "something broke"}')
        assert ev is not None
        assert ev.type == AgentEventType.SYSTEM
        assert "something broke" in ev.content

    def test_parse_auth_failure(self):
        from core.protocols import AgentEventType
        agent = CodexAgent()
        ev = agent._parse('{"type": "error", "message": "invalid api key"}')
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_parse_non_json(self):
        agent = CodexAgent()
        ev = agent._parse("plain text output")
        assert ev is not None
        assert ev.content == "plain text output"

    def test_parse_empty_line(self):
        agent = CodexAgent()
        ev = agent._parse("")
        assert ev is None


class TestInDocker:

    def test_detects_docker(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        with patch("adapters.agents.codex.DOCKERENV_PATH", mock_path):
            assert _in_docker() is True

    def test_detects_host(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("adapters.agents.codex.DOCKERENV_PATH", mock_path):
            assert _in_docker() is False
