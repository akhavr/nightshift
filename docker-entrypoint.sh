#!/bin/sh
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
# If OAuth auth.json is present, skip API key config generation entirely.
CODEX_OAUTH_PRESENT=0
if [ -d /codex-auth ]; then
    mkdir -p "$HOME/.codex"
    cp /codex-auth/auth.json "$HOME/.codex/" 2>/dev/null || true
    cp /codex-auth/config.toml "$HOME/.codex/" 2>/dev/null || true
    if [ -f "$HOME/.codex/auth.json" ]; then
        CODEX_OAUTH_PRESENT=1
    fi
fi

# Configure git for worktree: use env vars instead of rewriting .git file.
# This avoids corruption of worktree metadata (gitdir reverse pointer).
# WORKTREE_NAME is set by launch.py (e.g. "agent-abc123" or "review-abc123").
if [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    export GIT_DIR="/repo-git/worktrees/${WORKTREE_NAME}"
    export GIT_WORK_TREE="/workspace"
fi

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
# CODEX_API_KEY → OPENAI_API_KEY fallback chain.
# If CODEX_BASE_URL is set → generate config.toml with custom provider.
# If CODEX_BASE_URL not set → export OPENAI_API_KEY, Codex uses OpenAI natively.
# Skip entirely if OAuth auth.json is present.
mkdir -p "$HOME/.codex" 2>/dev/null || true
if [ "$AGENT_KIND" = "codex" ]; then
    if [ "$CODEX_OAUTH_PRESENT" = "1" ]; then
        : # OAuth auth.json present — skip API key config generation
    else
        CODEX_KEY="${CODEX_API_KEY:-$OPENAI_API_KEY}"
        if [ -z "$CODEX_KEY" ]; then
            echo "WARNING: AGENT_KIND=codex but no CODEX_API_KEY or OPENAI_API_KEY set — Codex CLI will fail" >&2
        elif [ -n "$CODEX_BASE_URL" ]; then
            # Custom provider: generate config.toml
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
            # OpenAI native: just export the key, no config.toml needed
            export OPENAI_API_KEY="$CODEX_KEY"
        fi
    fi
    # Register MCP signal server so Codex can call nightshift_done/checkpoint/question
    codex mcp add nightshift-signals -- python3 /opt/nightshift/nightshift-mcp-server.py
fi

# Note: litellm proxy removed - agents use LLM_*/ANTHROPIC_* env vars directly
# OpenHands uses LLM_* (litellm built-in), Claude Code uses ANTHROPIC_*

python3 /opt/nightshift/entrypoint.py "$@"
