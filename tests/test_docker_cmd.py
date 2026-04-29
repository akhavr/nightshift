"""Tests for host/docker_cmd.py."""

import json
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


def test_skip_oauth_prevents_codex_auth_mount(tmp_path):
    """skip_oauth=True prevents mounting ~/.codex into /codex-auth."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex").mkdir()

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(skip_oauth=True)

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
            overflow=overflow,
        )

    assert "/codex-auth:ro" not in " ".join(cmd)


def test_oauth_mount_present_when_skip_oauth_false(tmp_path):
    """skip_oauth=False keeps the /codex-auth mount when ~/.codex exists."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex").mkdir()

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(skip_oauth=False)

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
            overflow=overflow,
        )

    assert "/codex-auth:ro" in " ".join(cmd)


def test_profiles_yaml_mounted_when_exists(tmp_path, monkeypatch):
    """repo/.nightshift/profiles.yaml is mounted into the container when present."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    nightshift_dir = tmp_path / ".nightshift"
    nightshift_dir.mkdir()
    profiles_yaml = nightshift_dir / "profiles.yaml"
    profiles_yaml.write_text("profiles: []\n")

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
    assert (
        f"{profiles_yaml.resolve()}:/workspace/.nightshift/profiles.yaml:ro" in cmd_str
    )


def test_profiles_yaml_not_mounted_when_missing(tmp_path, monkeypatch):
    """repo/.nightshift/profiles.yaml is not mounted when the file is absent."""
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
    assert "/workspace/.nightshift/profiles.yaml:ro" not in cmd_str


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


def test_git_mount_uses_session_overlay_only(tmp_path, monkeypatch):
    """The session git overlay should be mounted directly without an extra config bind."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Create fake .git directory with config file
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")

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
    # Main .git mount should be read-write
    assert f"{tmp_path / '.git'}:/repo-git:rw" in cmd_str
    assert f"{tmp_path / 'ws' / '.git'}:/workspace/.git:rw" in cmd_str
    assert f"{tmp_path / '.git' / 'config'}:/repo-git/config:ro" not in cmd_str


def test_codex_oauth_excludes_api_keys(tmp_path, monkeypatch):
    """When Codex OAuth is present, CODEX_API_KEY and OPENAI_API_KEY are excluded."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Set API keys in environment
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")
    monkeypatch.setenv("CODEX_MODEL", "gpt-4")  # This should still pass

    # Create fake home with OAuth tokens
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "oauth-token"}}))

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

    cmd_str = " ".join(cmd)
    # API keys should NOT be passed
    assert "CODEX_API_KEY=" not in cmd_str
    assert "OPENAI_API_KEY=" not in cmd_str
    # But other Codex vars should still be passed
    assert "CODEX_MODEL=gpt-4" in cmd_str


def test_codex_oauth_keeps_api_keys_when_skip_oauth_enabled(tmp_path, monkeypatch):
    """skip_oauth keeps API keys in the container even when Codex OAuth exists."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "oauth-token"}}))

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(skip_oauth=True)

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
            overflow=overflow,
        )

    cmd_str = " ".join(cmd)
    assert "CODEX_API_KEY=sk-codex-key" in cmd_str
    assert "OPENAI_API_KEY=sk-openai-key" in cmd_str
    assert "/codex-auth:ro" not in cmd_str


def test_codex_no_oauth_includes_api_keys(tmp_path, monkeypatch):
    """When Codex OAuth is absent, API keys are passed through."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Set API keys in environment
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

    # Create fake home WITHOUT OAuth
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # No .codex directory

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

    cmd_str = " ".join(cmd)
    # API keys SHOULD be passed when no OAuth
    assert "CODEX_API_KEY=sk-codex-key" in cmd_str
    assert "OPENAI_API_KEY=sk-openai-key" in cmd_str


def test_codex_oauth_empty_tokens_includes_api_keys(tmp_path, monkeypatch):
    """When auth.json exists but tokens is empty, API keys are passed."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {}}))  # Empty tokens

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

    cmd_str = " ".join(cmd)
    # API keys should be passed when tokens is empty
    assert "CODEX_API_KEY=sk-codex-key" in cmd_str


def test_non_codex_agent_includes_api_keys_even_with_oauth(tmp_path, monkeypatch):
    """Non-codex agents pass API keys even if OAuth exists."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "oauth-token"}}))

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
            agent_kind="claude-code",  # Not codex
        )

    cmd_str = " ".join(cmd)
    # API keys should be passed for non-codex agents
    assert "CODEX_API_KEY=sk-codex-key" in cmd_str
    assert "OPENAI_API_KEY=sk-openai-key" in cmd_str


def test_auth_mode_oauth_excludes_all_codex_vars(tmp_path, monkeypatch):
    """auth_mode=oauth excludes CODEX_API_KEY, CODEX_BASE_URL, CODEX_MODEL, OPENAI_API_KEY."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Set all Codex vars in environment
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("CODEX_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(auth_mode="oauth")

    with patch("host.docker_cmd.Path.home", return_value=fake_home):
        cmd = build_docker_cmd(
            repo=tmp_path,
            workspace_mount="/workspace",
            session_dir=tmp_path / "session",
            container_name="test",
            worktree_name="test-worktree",
            issue_id="test-id",
            short_id="test",
            max_turns=50,
            step="coder",
            is_resume=False,
            workflow_path="WORKFLOW.md",
            image="nightshift:latest",
            overflow=overflow,
            agent_kind="codex",
        )

    cmd_str = " ".join(cmd)
    # All Codex/OpenAI vars should be excluded
    assert "CODEX_API_KEY=" not in cmd_str
    assert "CODEX_BASE_URL=" not in cmd_str
    assert "CODEX_MODEL=" not in cmd_str
    assert "OPENAI_API_KEY=" not in cmd_str


def test_auth_mode_api_key_passes_all(tmp_path, monkeypatch):
    """auth_mode=api_key passes all CODEX_* vars even if OAuth is present."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-key")
    monkeypatch.setenv("CODEX_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    # Create OAuth tokens (auth_mode=api_key should ignore this)
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "auth.json").write_text('{"tokens": {"access": "tok"}}')

    from core.config.models import OverflowConfig
    from host.docker_cmd import build_docker_cmd

    overflow = OverflowConfig(auth_mode="api_key")

    with patch("host.docker_cmd.Path.home", return_value=fake_home):
        cmd = build_docker_cmd(
            repo=tmp_path,
            workspace_mount="/workspace",
            session_dir=tmp_path / "session",
            container_name="test",
            worktree_name="test-worktree",
            issue_id="test-id",
            short_id="test",
            max_turns=50,
            step="coder",
            is_resume=False,
            workflow_path="WORKFLOW.md",
            image="nightshift:latest",
            overflow=overflow,
            agent_kind="codex",
        )

    cmd_str = " ".join(cmd)
    # All vars should be passed
    assert "CODEX_API_KEY=sk-codex-key" in cmd_str
    assert "CODEX_BASE_URL=https://example.com/v1" in cmd_str
    assert "CODEX_MODEL=gpt-5" in cmd_str
    assert "OPENAI_API_KEY=sk-openai-key" in cmd_str
