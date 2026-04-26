#!/bin/sh

# WT-1.6/1.7: Shared function to sanitize core.worktree if set to container path.
# Called at startup (WT-1.6) and exit trap (WT-1.7) for defense-in-depth.
# Arg $1: context string for log message (e.g. "startup" or "exit")
sanitize_core_worktree() {
    if [ -n "$GIT_DIR" ]; then
        WORKTREE_VAL=$(git config --get core.worktree 2>/dev/null || true)
        if [ "$WORKTREE_VAL" = "/workspace" ]; then
            git config --unset core.worktree 2>/dev/null || true
            echo "Sanitized core.worktree=/workspace (${1:-unknown})"
        fi
    fi
}
trap 'sanitize_core_worktree exit' EXIT

# Copy read-only credentials to writable HOME so Claude Code can function.
# The host mounts ~/.claude at /claude-auth:ro for security.
if [ -d /claude-auth ]; then
    mkdir -p "$HOME/.claude"
    cp /claude-auth/settings.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/settings.local.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/.credentials.json "$HOME/.claude/" 2>/dev/null || true
    # Copy plugins so Claude Code can discover installed skills (e.g. caveman)
    if [ -d /claude-auth/plugins ]; then
        cp -r /claude-auth/plugins "$HOME/.claude/plugins"
    fi
fi

# Copy Codex login credentials from read-only mount to writable HOME.
# OAuth (auth.json) skips API key export; config.toml is handled separately.
CODEX_OAUTH_PRESENT=0
if [ -d /codex-auth ]; then
    mkdir -p "$HOME/.codex"
    cp /codex-auth/auth.json "$HOME/.codex/" 2>/dev/null || true
    cp /codex-auth/config.toml "$HOME/.codex/" 2>/dev/null || true
    if [ -f "$HOME/.codex/auth.json" ]; then
        CODEX_OAUTH_PRESENT=1
        echo "Codex: OAuth auth.json found, using OAuth authentication" >&2
    fi
fi

# Configure git for worktree: use env vars instead of rewriting .git file.
# This avoids corruption of worktree metadata (gitdir reverse pointer).
# WORKTREE_NAME is set by launch.py (e.g. "agent-abc123" or "review-abc123").
if [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    export GIT_DIR="/repo-git/worktrees/${WORKTREE_NAME}"
    export GIT_WORK_TREE="/workspace"
fi

# WT-1.6: Sanitize core.worktree at startup (defense-in-depth with exit trap).
sanitize_core_worktree startup

# Create OpenHands conversation persistence directory
mkdir -p "$HOME/.openhands" 2>/dev/null || true

# OpenHands Docker workaround: shadow the openhands binary with a patched launcher.
# Fixes two bugs: condenser crash on startup + inflated max_output_tokens.
# See docs/openhands-docker-investigation.md for details.
if [ "$AGENT_KIND" = "openhands" ]; then
    mkdir -p "$HOME/bin"
    cat > "$HOME/bin/openhands" << 'OHWRAP'
#!/bin/sh
exec python3 /opt/nightshift/openhands-launcher.py "$@"
OHWRAP
    chmod +x "$HOME/bin/openhands"
    export PATH="$HOME/bin:$PATH"
fi

# Generate Codex config from env vars.
# OAuth (auth.json) and model config (config.toml) are INDEPENDENT:
# - OAuth provides authentication credentials
# - config.toml provides model selection (overflow or native)
# When overflow is active (CODEX_BASE_URL/CODEX_MODEL set), generate config.toml
# regardless of OAuth presence. Only skip API key export when OAuth is present.
mkdir -p "$HOME/.codex" 2>/dev/null || true
if [ "$AGENT_KIND" = "codex" ]; then
    # Step 1: Generate config.toml if model override or custom provider specified (independent of OAuth)
    if [ -n "$CODEX_BASE_URL" ] || [ -n "$CODEX_MODEL" ]; then
        if [ -n "$CODEX_BASE_URL" ]; then
            # Custom provider with base URL
            echo "Codex: Generating config.toml for custom provider (model=${CODEX_MODEL:-o3})" >&2
            cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${CODEX_MODEL:-o3}"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "${CODEX_BASE_URL}"
env_key = "CODEX_API_KEY"
CODEXCFG
        else
            # Model override only, use OpenAI provider
            echo "Codex: Generating config.toml for openai provider (model=${CODEX_MODEL})" >&2
            cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "${CODEX_MODEL}"
model_provider = "openai"
CODEXCFG
        fi
    fi

    # Step 2: API key config only needed when OAuth not present
    if [ "$CODEX_OAUTH_PRESENT" = "1" ]; then
        echo "Codex: Using OAuth authentication (skipping API key config)" >&2
    else
        CODEX_KEY="${CODEX_API_KEY:-$OPENAI_API_KEY}"
        if [ -z "$CODEX_KEY" ]; then
            echo "WARNING: AGENT_KIND=codex but no CODEX_API_KEY or OPENAI_API_KEY set — Codex CLI will fail" >&2
        elif [ -n "$CODEX_BASE_URL" ]; then
            # Custom provider: export the key for the config.toml env_key reference
            export CODEX_API_KEY="$CODEX_KEY"
        else
            # OpenAI native: export key and generate default config if no model override
            export OPENAI_API_KEY="$CODEX_KEY"
            # Only generate default config if no model override was specified in Step 1
            # AND no host config.toml was copied from /codex-auth
            if [ -z "$CODEX_MODEL" ] && [ ! -f "$HOME/.codex/config.toml" ]; then
                cat > "$HOME/.codex/config.toml" << CODEXCFG
model = "gpt-4o-mini"
model_provider = "openai"
CODEXCFG
            fi
        fi
    fi
    # Register MCP signal server so Codex can call nightshift_done/checkpoint/question
    codex mcp add nightshift-signals -- python3 /opt/nightshift/nightshift-mcp-server.py
fi

# Note: litellm proxy removed - agents use LLM_*/ANTHROPIC_* env vars directly
# OpenHands uses LLM_* (litellm built-in), Claude Code uses ANTHROPIC_*

python3 /opt/nightshift/entrypoint.py "$@"
