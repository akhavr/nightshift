"""Tests for host/docker_cmd.py."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_codex_oauth_mount_added(tmp_path):
    """When ~/.codex exists, docker command includes the /codex-auth mount."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex").mkdir()

    from host.docker_cmd import build_docker_cmd

    with patch("host.docker_cmd.Path.home", return_value=fake_home):
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
            agent_kind="codex",
        )

    assert "/codex-auth:ro" in " ".join(cmd)


def test_overflow_profile_env_var_passed(tmp_path, monkeypatch):
    """OVERFLOW_PROFILE env var is included when overflow has a profile_name."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(
        profile_name="openhands-qwen",
        agent_kind="openhands",
        env={"OVERFLOW_API_KEY": "test-key"},
    )

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
        overflow=overflow,
    )

    cmd_str = " ".join(cmd)
    assert "OVERFLOW_PROFILE=openhands-qwen" in cmd_str
    assert "OVERFLOW_ACTIVE=1" in cmd_str


def test_overflow_no_profile_name_no_env_var(tmp_path, monkeypatch):
    """OVERFLOW_PROFILE env var is NOT included when overflow has no profile_name."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(
        profile_name=None,  # No profile name
        agent_kind="codex",
        env={"OVERFLOW_API_KEY": "test-key"},
    )

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
        overflow=overflow,
    )

    cmd_str = " ".join(cmd)
    assert "OVERFLOW_PROFILE=" not in cmd_str
    # OVERFLOW_ACTIVE should still be set
    assert "OVERFLOW_ACTIVE=1" in cmd_str
