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
# Rewrite to use the mounted /repo-git path so commits land on the agent branch.
if [ -f /workspace/.git ] && [ -d /repo-git ] && [ -n "$SHORT_ID" ]; then
    echo "gitdir: /repo-git/worktrees/agent-${SHORT_ID}" > /workspace/.git
fi

exec python3 /opt/nightshift/entrypoint.py "$@"
