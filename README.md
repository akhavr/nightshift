# Agent Worker

Autonomous coding agent runner — pluggable agent, tracker, notifier, and workspace adapters.

See [SPEC.md](SPEC.md) for the full implementation specification with open questions and design decisions.

## Quick Start

```bash
# 1. Verify Claude Code stdin behavior (OQ-1 — do this first!)
mkfifo /tmp/ccpipe
claude --dangerously-skip-permissions -p "Say hi, then read my next message" < /tmp/ccpipe
# In another terminal: echo "follow-up" > /tmp/ccpipe

# 2. Capture real stream-json schema (OQ-2)
claude --dangerously-skip-permissions --output-format stream-json \
  -p "List 3 files in the current directory" 2>/dev/null | head -50

# 3. Build the Docker image
docker build -t agent-worker:latest .

# 4. Start the host watcher (monitors all sessions, pauses idle containers)
python host/watcher.py --sessions-dir .agent-worker/sessions &

# 5. Launch a worker on a git-bug issue
python host/launch.py <issue-id>

# 6. Answer questions (from terminal — works even if container is paused)
python host/cli.py answer <issue-id> "Use the Foo library"

# 7. Check status
python host/cli.py status
```

## Configuration

Edit `WORKFLOW.md` in your repo root. See [WORKFLOW.md](WORKFLOW.md) for an example.

## Architecture

```
core/           Protocol-based core (agent/tracker agnostic)
adapters/       Concrete implementations (Claude Code, git-bug, Telegram, etc.)
host/           Host-side scripts (launcher, watcher, CLI)
```

## Adapter Matrix

| Component | Provided | Planned |
|---|---|---|
| Agent | Claude Code | Codex, Aider |
| Tracker | git-bug | GitHub Issues, Linear |
| Notifier | Telegram, Webhook | Slack, Discord |
| Workspace | Git worktree | Plain directory |
