#!/bin/sh
# Copy read-only credentials to writable HOME so Claude Code can function.
# The host mounts ~/.claude at /claude-auth:ro for security.
if [ -d /claude-auth ]; then
    mkdir -p "$HOME/.claude"
    cp /claude-auth/.credentials.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/settings.json "$HOME/.claude/" 2>/dev/null || true
    cp /claude-auth/settings.local.json "$HOME/.claude/" 2>/dev/null || true
fi

# Fix worktree .git pointer: the host path doesn't exist inside the container.
# Rewrite to use the mounted /repo-git path so commits land on the correct branch.
# WORKTREE_NAME is set by launch.py (e.g. "agent-abc123" or "review-abc123").
if [ -f /workspace/.git ] && [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    echo "gitdir: /repo-git/worktrees/${WORKTREE_NAME}" > /workspace/.git
fi

# Start litellm proxy if config file exists (overflow mode with model remapping).
LITELLM_CONFIG="/session/litellm-config.yaml"
if [ -f "$LITELLM_CONFIG" ]; then
    echo "Starting litellm proxy (config: $LITELLM_CONFIG)..."
    litellm --config "$LITELLM_CONFIG" --port 4000 > /session/litellm.log 2>&1 &
    LITELLM_PID=$!

    # Wait for proxy to become healthy (up to 30s)
    LITELLM_READY=0
    for i in $(seq 1 60); do
        if curl -sf http://localhost:4000/health > /dev/null 2>&1; then
            LITELLM_READY=1
            break
        fi
        sleep 0.5
    done

    if [ "$LITELLM_READY" = "1" ]; then
        echo "litellm proxy ready on port 4000 (pid $LITELLM_PID)"
    else
        echo "ERROR: litellm proxy failed to start within 30s" >&2
        cat /session/litellm.log >&2
        kill "$LITELLM_PID" 2>/dev/null || true
        exit 1
    fi
fi

exec python3 /opt/nightshift/entrypoint.py "$@"
