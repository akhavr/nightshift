"""Tests for host/docker_cmd.py."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_openrouter_api_key_passthrough():
    """OPENROUTER_API_KEY is passed through to docker container."""
    from host.docker_cmd import _PASSTHROUGH_ENV_VARS

    assert "OPENROUTER_API_KEY" in _PASSTHROUGH_ENV_VARS


def test_openrouter_api_key_in_docker_command(monkeypatch, tmp_path):
    """OPENROUTER_API_KEY appears in docker command when set in environment."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    # Clear other env vars that might cause issues
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    from host.docker_cmd import build_docker_cmd

    cmd = build_docker_cmd(
        repo=tmp_path,
        workspace_mount=str(tmp_path / "ws"),
        session_dir=tmp_path / "session",
        container_name="test",
        worktree_name="test-worktree",
        issue_id="test-123",
        short_id="t123",
        max_turns=10,
        step="coder",
        is_resume=False,
        workflow_path=str(tmp_path / "WORKFLOW.md"),
        image="nightshift:latest",
    )

    cmd_str = " ".join(cmd)
    assert "OPENROUTER_API_KEY=test-openrouter-key" in cmd_str
