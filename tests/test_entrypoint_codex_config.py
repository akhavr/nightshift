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
if [ "$AGENT_KIND" = "codex" ]; then
    CODEX_KEY="${CODEX_API_KEY:-$OPENAI_API_KEY}"
    if [ -z "$CODEX_KEY" ]; then
        echo "WARNING: AGENT_KIND=codex but no CODEX_API_KEY or OPENAI_API_KEY set — Codex CLI will fail" >&2
    elif [ -n "$CODEX_BASE_URL" ]; then
        export CODEX_API_KEY="$CODEX_KEY"
        cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${CODEX_MODEL:-o3}"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "${CODEX_BASE_URL}"
env_key = "CODEX_API_KEY"
CODEXCFG
    else
        export OPENAI_API_KEY="$CODEX_KEY"
        # Echo exported var so tests can verify
        echo "OPENAI_API_KEY=$OPENAI_API_KEY"
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

    def test_codex_api_key_only_no_config_toml(self, tmp_path):
        """With CODEX_API_KEY but no CODEX_BASE_URL, no config.toml generated, OPENAI_API_KEY exported."""
        config_path, stdout, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-openai-test",
        })

        assert not config_path.exists()
        assert "OPENAI_API_KEY=sk-openai-test" in stdout

    def test_codex_base_url_generates_config(self, tmp_path):
        """With CODEX_BASE_URL set, config.toml generated with custom provider."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-or-test",
            "CODEX_BASE_URL": "https://openrouter.ai/api/v1",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert 'model_provider = "custom"' in content
        assert "https://openrouter.ai/api/v1" in content
        assert 'env_key = "CODEX_API_KEY"' in content

    def test_codex_model_in_config(self, tmp_path):
        """CODEX_MODEL appears in generated config.toml."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-test",
            "CODEX_BASE_URL": "https://example.com/v1",
            "CODEX_MODEL": "qwen/qwen3-coder",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert "qwen/qwen3-coder" in content

    def test_codex_model_default(self, tmp_path):
        """Without CODEX_MODEL, default model is o3."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-test",
            "CODEX_BASE_URL": "https://example.com/v1",
        })

        content = config_path.read_text()
        assert 'model = "o3"' in content

    def test_codex_api_key_fallback_to_openai(self, tmp_path):
        """Without CODEX_API_KEY, OPENAI_API_KEY is used."""
        config_path, stdout, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "OPENAI_API_KEY": "sk-openai-fallback",
        })

        # No CODEX_BASE_URL → no config.toml, but OPENAI_API_KEY exported
        assert not config_path.exists()
        assert "OPENAI_API_KEY=sk-openai-fallback" in stdout

    def test_codex_api_key_fallback_with_base_url(self, tmp_path):
        """Without CODEX_API_KEY but with OPENAI_API_KEY and CODEX_BASE_URL, config.toml uses fallback key."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "OPENAI_API_KEY": "sk-openai-fallback",
            "CODEX_BASE_URL": "http://localhost:8080/v1",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert "http://localhost:8080/v1" in content
        assert 'env_key = "CODEX_API_KEY"' in content

    def test_codex_no_key_warns(self, tmp_path):
        """AGENT_KIND=codex without any API key warns on stderr and skips config."""
        config_path, _, stderr = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
        })

        assert not config_path.exists()
        assert "WARNING" in stderr
        assert "CODEX_API_KEY" in stderr
        assert "OPENAI_API_KEY" in stderr

    def test_non_codex_agent_no_config(self, tmp_path):
        """AGENT_KIND=claude-code does not generate codex config."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "claude-code",
        })

        assert not config_path.exists()

    def test_no_agent_kind_no_config(self, tmp_path):
        """Without AGENT_KIND, no config is generated."""
        config_path, _, _ = _run_config_script(tmp_path, {})

        assert not config_path.exists()

    def test_codex_api_key_preferred_over_openai(self, tmp_path):
        """CODEX_API_KEY is preferred over OPENAI_API_KEY when both set."""
        config_path, stdout, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "sk-codex-key",
            "OPENAI_API_KEY": "sk-openai-key",
        })

        # No base URL → OPENAI_API_KEY exported with CODEX_API_KEY value
        assert not config_path.exists()
        assert "OPENAI_API_KEY=sk-codex-key" in stdout

    def test_local_inference_config(self, tmp_path):
        """Local inference setup: CODEX_BASE_URL pointing to localhost."""
        config_path, _, _ = _run_config_script(tmp_path, {
            "AGENT_KIND": "codex",
            "CODEX_API_KEY": "not-needed",
            "CODEX_BASE_URL": "http://localhost:8080/v1",
            "CODEX_MODEL": "local-model",
        })

        assert config_path.exists()
        content = config_path.read_text()
        assert "http://localhost:8080/v1" in content
        assert "local-model" in content
