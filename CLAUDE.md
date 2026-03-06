# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Agent Worker is an autonomous coding agent runner. It launches a coding agent (e.g. Claude Code) inside a Docker container against an issue from a tracker (e.g. git-bug), manages the session lifecycle (checkpoints, Q&A, resume on context limits), and merges results after human review.

## Commands

```bash
# Build Docker image
docker build -t agent-worker:latest .

# Launch a worker on an issue
python host/launch.py <issue-id>

# Resume a suspended session
python host/launch.py <issue-id> --resume

# CLI (wraps launch.py)
python host/cli.py start <issue-id>
python host/cli.py resume <issue-id>
python host/cli.py answer <issue-id> "your answer"
python host/cli.py status
python host/cli.py logs <issue-id>
python host/cli.py history <issue-id>
python host/cli.py cleanup <issue-id>

# Start host watcher (pauses idle containers, polls Telegram)
python host/watcher.py --sessions-dir .agent-worker/sessions

# Run tests
python -m pytest tests/
python -m pytest tests/conftest.py::TestName -v   # single test

# Install dependencies (host-side, for running host/ scripts or tests)
pip install -r requirements.txt
```

## Architecture

The system has a strict three-layer split:

**`core/`** — Protocol-based core, no concrete adapter imports. All external boundaries defined as `Protocol` classes in `core/protocols.py`:
- `CodingAgent` — start/stream/send_input/terminate lifecycle
- `IssueTracker` — CRUD for issues, comments, labels, sync
- `WorkspaceManager` — create/commit/finalize workspaces
- `Notifier` — notify + round-trip Q&A (send_question/check_answer)

Key core modules:
- `config.py` — Parses `WORKFLOW.md` YAML front matter into typed dataclasses. Contains adapter registries and factory functions (`create_agent`, `create_tracker`, etc.) that use `importlib` for dynamic instantiation.
- `session.py` — `SessionRunner`: the main event loop. Streams agent events, handles markers (`@@LOG@@`, `@@CHECKPOINT@@`, `@@QUESTION@@`, `@@WAITING@@`, `@@DONE@@`), manages auto-resume on context limits/stalls, and orchestrates the review/merge gate.
- `state.py` — `StateManager`: atomic JSON state persistence in `/session/`. Files: `state.json`, `conversation.jsonl`, `raw-output.log`, `resume-prompt.md`, `waiting.json`, `answer.txt`.
- `prompts.py` — Jinja2 template rendering for WORKFLOW.md prompt body, plus fallback prompt builder.
- `search.py` — Keyword-based related issue search across any tracker.

**`adapters/`** — Concrete implementations organized by concern:
- `agents/` — `ClaudeCodeAgent` (PTY-based, parses `--output-format stream-json`), `CodexAgent` (stub)
- `trackers/` — `GitBugTracker` (shells out to `git-bug` CLI), `GitHubIssuesTracker` (stub)
- `notifiers/` — `TelegramNotifier` (force_reply Q&A with polling thread), `WebhookNotifier`, `CompositeNotifier` (broadcasts to all, Q&A through primary)
- `workspaces/` — `GitWorktreeManager` (creates git worktrees per issue)

**`host/`** — Host-side scripts (run outside Docker):
- `launch.py` — Creates worktree + session dir, builds `docker run` command with volume mounts (workspace, session, auth, WORKFLOW.md), then `execvp` into Docker.
- `watcher.py` — Polls session dirs for `waiting.json`, pauses Docker containers, writes `answer.txt` on Telegram reply or CLI input, then unpauses. Zero tracker coupling.
- `cli.py` — User-facing CLI that delegates to launch.py/watcher.py.

**`entrypoint.py`** — Container entrypoint. Reads WORKFLOW.md, instantiates adapters via config factories, runs `SessionRunner`.

## Key Design Patterns

- **Adapter registration**: `core/config.py` has `AGENT_REGISTRY`, `TRACKER_REGISTRY`, etc. mapping `kind` strings to `(module_path, class_name)` tuples. New adapters: add entry to registry + implement the Protocol.
- **WORKFLOW.md**: YAML front matter configures adapters, merge policy, hooks. The markdown body after `---` is the Jinja2 prompt template. `$VAR` references in YAML are resolved from environment variables.
- **Container-host communication**: Exclusively via shared files in the session directory (`/session/` inside container, `.agent-worker/sessions/<id>/` on host). No network calls between container and host.
- **Q&A flow**: Agent outputs `@@QUESTION@@` then `@@WAITING@@`. Container writes `waiting.json`. Host watcher pauses the container. Answer arrives via Telegram reply, tracker comment, or `cli.py answer`. Written to `answer.txt`. Container unpaused. Agent receives answer on stdin.
- **Auto-resume**: On context limit, stall, or max-turns, `SessionRunner` builds a resume prompt with checkpoint history and recent conversation, then restarts the agent in a loop (up to `MAX_RESUMES=10`).

## Testing

Tests use mock implementations from `tests/conftest.py` (`MockAgent`, `MockTracker`, `MockNotifier`, `MockWorkspaceManager`). Test directories exist for `tests/adapters/` and `tests/integration/` but have no test files yet.

## Dependencies

Python 3.12+. Runtime: `requests`, `pyyaml`, `jinja2`. The Docker image also installs `git-bug` and `@anthropic-ai/claude-code` (npm).
