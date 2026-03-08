# Nightshift

Autonomous coding agent runner — pluggable agent, tracker, notifier, and workspace adapters. Inspired by [OpenAI Symphony](https://github.com/openai/symphony).

See [SPEC.md](SPEC.md) for the full implementation specification with design decisions.

## Quick Start

```bash
# 1. Install dependencies
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest

# 2. Initialize your repo
python host/cli.py init
# Creates WORKFLOW.md, .env.example, .agent-worker/, updates .gitignore

# 3. Configure credentials
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.

# 4. Review and customize WORKFLOW.md
# YAML front matter configures adapters; markdown body is the prompt template

# 5. Build the Docker image
docker build -t agent-worker:latest .

# 6. Start the host watcher (monitors sessions, pauses idle containers, polls Telegram)
python host/cli.py watcher &

# 7. Launch a worker on an issue
python host/cli.py start <issue-id>

# 8. Monitor progress
python host/cli.py status          # all sessions
python host/cli.py logs <issue-id> # live output
python host/cli.py history <id>    # conversation timeline

# 9. Answer agent questions (works even if container is paused)
python host/cli.py answer <issue-id> "Use the Foo library"

# 10. Review and merge (or reject)
python host/cli.py accept <issue-id>   # merge agent branch into base
python host/cli.py reject <issue-id>   # discard agent work and clean up
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `init` | Scaffold WORKFLOW.md, .env.example, .agent-worker/ |
| `start <id>` | Create worktree + session, launch Docker container |
| `resume <id>` | Resume a suspended session |
| `answer <id> "msg"` | Write answer for a waiting agent |
| `status` | Show all session statuses |
| `logs <id>` | Tail raw agent output |
| `history <id>` | Show conversation timeline with icons |
| `accept <id>` | Merge agent branch into base, clean up worktree |
| `reject <id>` | Discard agent work, remove worktree + session |
| `cleanup <id>` | Remove worktree and optionally session |
| `watcher` | Start host watcher (pause/unpause, Telegram polling) |

## Configuration

**WORKFLOW.md** in your repo root configures everything:
- YAML front matter: agent, tracker, workspace, notifications, merge policy, hooks
- Markdown body (after `---`): Jinja2 prompt template sent to the agent
- `$VAR` references in YAML are resolved from `.env` / environment variables

Run `python host/cli.py init` to generate a starter WORKFLOW.md.

## Architecture

```
core/           Protocol-based core (agent/tracker agnostic)
adapters/       Concrete implementations (Claude Code, git-bug, Telegram, etc.)
host/           Host-side scripts (launcher, watcher, CLI)
```

The host creates a git worktree and session directory, dumps issue data to JSON, then launches a Docker container with volume mounts. Inside the container, a `StaticTracker` reads the pre-dumped issue data (no tracker needed inside Docker). The agent runs in fire-and-forget mode; container-host communication is exclusively via shared files in the session directory.

## Adapter Matrix

| Component | Provided | Planned |
|---|---|---|
| Agent | Claude Code | Codex, Aider |
| Tracker | git-bug, Static (container) | GitHub Issues |
| Notifier | Telegram, Webhook, Composite | Slack, Discord |
| Workspace | Git worktree | — |

## Docker

The container runs with `--user $(id -u):$(id -g)` to match host UID for credential file access. Key mounts:

| Mount | Container path | Mode |
|-------|---------------|------|
| Git worktree | `/workspace` | rw |
| Session dir | `/session` | rw |
| Repo `.git` | `/repo-git` | rw |
| `~/.claude` | `/claude-auth` | ro |
| WORKFLOW.md | `/workspace/WORKFLOW.md` | ro |

`docker-entrypoint.sh` copies auth credentials to a writable HOME and rewrites the worktree `.git` pointer to use container paths.

## Testing

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m pytest tests/test_stream_parser.py -v  # single file
```

## Dependencies

Python 3.12+. Runtime: `requests`, `pyyaml`, `jinja2`. Docker image includes `git-bug` v0.10.1 and `@anthropic-ai/claude-code` (npm).
