#!/bin/sh
# Copy read-only credentials to writable HOME so Claude Code can function.
# The host mounts ~/.claude at /claude-auth:ro for security.
if [ -d /claude-auth ]; then
    mkdir -p "$HOME/.claude"
    cp /claude-auth/settings.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/settings.local.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/.credentials.json "$HOME/.claude/" 2>/dev/null || true
fi

# Fix worktree .git pointer: the host path doesn't exist inside the container.
# Rewrite to use the mounted /repo-git path so commits land on the correct branch.
# WORKTREE_NAME is set by launch.py (e.g. "agent-abc123" or "review-abc123").
if [ -f /workspace/.git ] && [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    echo "gitdir: /repo-git/worktrees/${WORKTREE_NAME}" > /workspace/.git
fi

# Create OpenHands conversation persistence directory
mkdir -p "$HOME/.openhands" 2>/dev/null || true

# Codex config: generate ~/.codex/config.toml when using a non-default provider.
# Regular mode (OPENAI_API_KEY set): no config needed, Codex uses OpenAI natively.
# Overflow / OpenRouter: generate config.toml pointing to the alternate provider.
# Fallback: if AGENT_KIND=codex but no OPENAI_API_KEY, fill from OVERFLOW_* vars.
mkdir -p "$HOME/.codex" 2>/dev/null || true
if [ "$AGENT_KIND" = "codex" ] && [ -z "$OPENAI_API_KEY" ] && [ -n "$OVERFLOW_API_KEY" ]; then
    export OPENAI_API_KEY="$OVERFLOW_API_KEY"
    # Note: do NOT set OPENAI_BASE_URL — it's deprecated by Codex CLI
    # and causes routing issues. base_url in config.toml is sufficient.
    cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${OVERFLOW_MODEL:-qwen/qwen3-coder}"
model_provider = "${CODEX_MODEL_PROVIDER:-openrouter}"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "${OVERFLOW_BASE_URL:-https://openrouter.ai/api/v1}"
env_key = "OPENAI_API_KEY"
CODEXCFG
elif [ -n "$OPENAI_API_KEY" ] && [ -n "$OPENAI_BASE_URL" ]; then
    # Custom OpenAI-compatible provider (e.g. local inference)
    cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${OPENAI_MODEL:-o3}"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "${OPENAI_BASE_URL}"
env_key = "OPENAI_API_KEY"
CODEXCFG
fi

# Note: litellm proxy removed - agents use LLM_*/ANTHROPIC_* env vars directly
# OpenHands uses LLM_* (litellm built-in), Claude Code uses ANTHROPIC_*

exec python3 /opt/nightshift/entrypoint.py "$@"
