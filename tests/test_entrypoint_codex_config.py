"""Tests for docker-entrypoint.sh Codex config.toml generation."""

import os
import subprocess
from pathlib import Path

import pytest


# Extract the codex config generation portion of docker-entrypoint.sh
# into a standalone script that can be tested with controlled env vars.
_CODEX_CONFIG_SCRIPT = """\
#!/bin/sh
mkdir -p "$HOME/.codex" 2>/dev/null || true
if [ -n "$OVERFLOW_API_KEY" ]; then
    cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${OVERFLOW_MODEL:-qwen/qwen3-coder}"
model_provider = "${CODEX_MODEL_PROVIDER:-openrouter}"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "${OVERFLOW_BASE_URL:-https://openrouter.ai/api/v1}"
env_key = "OVERFLOW_API_KEY"
CODEXCFG
elif [ "$AGENT_KIND" = "codex" ]; then
    CODEX_KEY="${CODEX_API_KEY:-$ANTHROPIC_API_KEY}"
    if [ -z "$CODEX_KEY" ]; then
        echo "WARNING: AGENT_KIND=codex but no CODEX_API_KEY or ANTHROPIC_API_KEY set — Codex CLI will fail" >&2
    elif [ -n "$CODEX_KEY" ]; then
        export CODEX_API_KEY="$CODEX_KEY"
        cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${ANTHROPIC_MODEL:-claude-sonnet-4-5-20250514}"
model_provider = "${CODEX_MODEL_PROVIDER:-anthropic}"

[model_providers.anthropic]
name = "Anthropic"
base_url = "${ANTHROPIC_BASE_URL:-https://api.anthropic.com/v1}"
env_key = "CODEX_API_KEY"
CODEXCFG
    fi
fi
"""


def _run_config_script(tmp_path: Path, env_overrides: dict) -> tuple[Path, str, str]:
    """Run the codex config script with controlled env and return config path + output."""
    script = tmp_path / "gen_config.sh"
    script.write_text(_CODEX_CONFIG_SCRIPT)
    script.chmod(0o755)

    # Minimal env: HOME points to tmp_path so ~/.codex/config.toml lands there
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(env_overrides)

    result = subprocess.run(
        ["/bin/sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    config_path = tmp_path / ".codex" / "config.toml"
    return config_path, result.stdout, result.stderr


class TestCodexConfigGeneration:

    def test_overflow_mode_generates_openrouter_config(self, tmp_path):
        """When OVERFLOW_API_KEY is set, config.toml uses openrouter provider."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "OVERFLOW_API_KEY": "sk-or-test-key",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert "openrouter" in content
        assert 'env_key = "OVERFLOW_API_KEY"' in content
        assert "qwen/qwen3-coder" in content

    def test_overflow_mode_custom_model(self, tmp_path):
        """OVERFLOW_MODEL overrides the default model in overflow mode."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "OVERFLOW_API_KEY": "sk-or-test-key",
            "OVERFLOW_MODEL": "anthropic/claude-3-haiku",
        })

        content = config_path.read_text()
        assert "anthropic/claude-3-haiku" in content

    def test_non_overflow_codex_with_anthropic_key(self, tmp_path):
        """AGENT_KIND=codex with ANTHROPIC_API_KEY generates anthropic provider config."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "ANTHROPIC_API_KEY": "sk-ant-test-key",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert "anthropic" in content
        assert 'env_key = "CODEX_API_KEY"' in content
        assert "claude-sonnet-4-5-20250514" in content
        assert "api.anthropic.com" in content

    def test_non_overflow_codex_prefers_codex_api_key(self, tmp_path):
        """CODEX_API_KEY is preferred over ANTHROPIC_API_KEY when both set."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-codex-key",
            "ANTHROPIC_API_KEY": "sk-ant-key",
        })

        assert config_path.exists()
        content = config_path.read_text()
        # Config should exist (CODEX_API_KEY takes priority)
        assert 'env_key = "CODEX_API_KEY"' in content

    def test_non_overflow_codex_no_key_warns(self, tmp_path):
        """AGENT_KIND=codex without any API key warns on stderr and skips config."""
        config_path, _, stderr = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
        })

        assert not config_path.exists()
        assert "WARNING" in stderr
        assert "CODEX_API_KEY" in stderr
        assert "ANTHROPIC_API_KEY" in stderr

    def test_non_codex_agent_no_config(self, tmp_path):
        """AGENT_KIND=claude-code does not generate codex config."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "claude-code",
        })

        assert not config_path.exists()

    def test_no_agent_kind_no_overflow_no_config(self, tmp_path):
        """Without AGENT_KIND or OVERFLOW_API_KEY, no config is generated."""
        config_path, _, _ = _run_config_script(tmp_path, {})

        assert not config_path.exists()

    def test_overflow_takes_priority_over_agent_kind(self, tmp_path):
        """When both OVERFLOW_API_KEY and AGENT_KIND=codex are set, overflow wins."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "OVERFLOW_API_KEY": "sk-or-key",
            "AGENT_KIND": "codex",
            "ANTHROPIC_API_KEY": "sk-ant-key",
        })

        assert config_path.exists()
        content = config_path.read_text()
        # Should be openrouter (overflow), not anthropic
        assert "openrouter" in content
        assert 'env_key = "OVERFLOW_API_KEY"' in content

    def test_custom_anthropic_model(self, tmp_path):
        """ANTHROPIC_MODEL overrides default model in non-overflow codex mode."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "ANTHROPIC_API_KEY": "sk-ant-key",
            "ANTHROPIC_MODEL": "claude-opus-4-6-20250620",
        })

        content = config_path.read_text()
        assert "claude-opus-4-6-20250620" in content

    def test_custom_base_url(self, tmp_path):
        """ANTHROPIC_BASE_URL overrides default base URL in non-overflow codex mode."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "ANTHROPIC_API_KEY": "sk-ant-key",
            "ANTHROPIC_BASE_URL": "https://custom.api.example.com/v1",
        })

        content = config_path.read_text()
        assert "https://custom.api.example.com/v1" in content
