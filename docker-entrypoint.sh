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

# Generate Codex config from env vars.
# Overflow mode: use OVERFLOW_API_KEY with openrouter provider.
# Non-overflow codex mode: use CODEX_API_KEY or ANTHROPIC_API_KEY with anthropic provider.
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
    # Non-overflow codex: pick the best available API key
    CODEX_KEY="${CODEX_API_KEY:-$ANTHROPIC_API_KEY}"
    if [ -n "$CODEX_KEY" ]; then
        # Export so Codex CLI can read it via env_key reference
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

# Note: litellm proxy removed - agents use LLM_*/ANTHROPIC_* env vars directly
# OpenHands uses LLM_* (litellm built-in), Claude Code uses ANTHROPIC_*

exec python3 /opt/nightshift/entrypoint.py "$@"
