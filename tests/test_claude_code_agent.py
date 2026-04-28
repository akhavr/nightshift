"""Tests for ClaudeCodeAgent prompt file handling."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from adapters.agents.claude_code import ClaudeCodeAgent
from core.constants import PROMPT_FILE_THRESHOLD, PROMPT_FILE_NAME


class TestLargePromptHandling:
    """Tests for large prompt file-based passing."""

    def test_large_prompt_uses_file(self, tmp_path):
        """When prompt > 100KB, agent uses -p @/session/prompt.txt."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        large_prompt = "x" * (PROMPT_FILE_THRESHOLD + 1)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(large_prompt, workspace)

            cmd = mock_popen.call_args[0][0]

            # Should use file reference
            assert "-p" in cmd
            p_index = cmd.index("-p")
            prompt_arg = cmd[p_index + 1]
            assert prompt_arg == f"@{tmp_path / PROMPT_FILE_NAME}"

            # File should exist with content
            prompt_file = tmp_path / PROMPT_FILE_NAME
            assert prompt_file.exists()
            assert prompt_file.read_text() == large_prompt

            # Agent should track the file for cleanup
            assert agent._prompt_file == prompt_file

    def test_small_prompt_uses_inline(self, tmp_path):
        """When prompt < 100KB, agent uses -p "prompt" directly."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        small_prompt = "Do something simple"

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(small_prompt, workspace)

            cmd = mock_popen.call_args[0][0]

            # Should use inline prompt
            assert "-p" in cmd
            p_index = cmd.index("-p")
            prompt_arg = cmd[p_index + 1]
            assert prompt_arg == small_prompt

            # No file should be created
            prompt_file = tmp_path / PROMPT_FILE_NAME
            assert not prompt_file.exists()

            # Agent should not track a prompt file
            assert agent._prompt_file is None

    def test_prompt_at_threshold_uses_inline(self, tmp_path):
        """Prompt exactly at threshold should use inline (not file)."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        threshold_prompt = "x" * PROMPT_FILE_THRESHOLD

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(threshold_prompt, workspace)

            cmd = mock_popen.call_args[0][0]
            p_index = cmd.index("-p")
            prompt_arg = cmd[p_index + 1]
            assert prompt_arg == threshold_prompt
            assert agent._prompt_file is None

    def test_prompt_file_cleaned_up_on_process_exit(self, tmp_path):
        """Prompt file is deleted when process exits."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        large_prompt = "x" * (PROMPT_FILE_THRESHOLD + 1)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(large_prompt, workspace)

            prompt_file = tmp_path / PROMPT_FILE_NAME
            assert prompt_file.exists()

            # Simulate process exit
            agent._on_process_exit()

            assert not prompt_file.exists()
            assert agent._prompt_file is None

    def test_cleanup_handles_missing_file(self, tmp_path):
        """Cleanup handles case where file was already deleted."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        large_prompt = "x" * (PROMPT_FILE_THRESHOLD + 1)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(large_prompt, workspace)

            prompt_file = tmp_path / PROMPT_FILE_NAME
            prompt_file.unlink()  # Manually delete

            # Should not raise
            agent._on_process_exit()
            assert agent._prompt_file is None

    def test_no_session_dir_uses_inline(self, tmp_path):
        """When session_dir doesn't exist, use inline prompt even for large prompts."""
        nonexistent_dir = tmp_path / "nonexistent"
        agent = ClaudeCodeAgent(session_dir=nonexistent_dir)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        large_prompt = "x" * (PROMPT_FILE_THRESHOLD + 1)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            agent.start(large_prompt, workspace)

            cmd = mock_popen.call_args[0][0]
            p_index = cmd.index("-p")
            prompt_arg = cmd[p_index + 1]

            # Falls back to inline since session_dir doesn't exist
            assert prompt_arg == large_prompt
            assert agent._prompt_file is None

    def test_default_session_dir(self):
        """Default session_dir is /session."""
        agent = ClaudeCodeAgent()
        assert agent._session_dir == Path("/session")

    def test_custom_session_dir(self, tmp_path):
        """Can pass custom session_dir."""
        agent = ClaudeCodeAgent(session_dir=tmp_path)
        assert agent._session_dir == tmp_path


class TestSignalMethodOutput:
    def test_signal_method_file_writes_file_signal(self):
        done_file = Path("/session/signal/done")
        done_file.unlink(missing_ok=True)

        agent = ClaudeCodeAgent(signal_method="file")
        raw = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__nightshift-signals__nightshift_done",
                        "input": {"summary": "Task complete"},
                    }
                ]
            },
        })

        ev = agent._parse(raw)

        assert ev is None
        assert done_file.exists()
        done_file.unlink(missing_ok=True)


class TestPromptFileThreshold:
    """Tests for the threshold constant."""

    def test_threshold_value(self):
        """Threshold is 100KB as specified."""
        assert PROMPT_FILE_THRESHOLD == 100_000

    def test_prompt_file_name(self):
        """Prompt file name is prompt.txt."""
        assert PROMPT_FILE_NAME == "prompt.txt"
