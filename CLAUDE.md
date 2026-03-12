# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Nightshift is an autonomous coding agent runner. It launches a coding agent (e.g. Claude Code) inside a Docker container against an issue from a tracker (e.g. git-bug), manages the session lifecycle (checkpoints, Q&A, resume on context limits), and handles review/merge via the host CLI.

## Commands

```bash
# Build Docker image
docker build -t nightshift:latest .

# CLI commands (use `nightshift` if installed, otherwise `python host/cli.py`)
nightshift init                          # scaffold WORKFLOW.md, .env.example, .nightshift/
nightshift start <issue-id>              # create worktree + session, launch container
nightshift resume <issue-id>             # resume a suspended session
nightshift answer <issue-id> "your answer"
nightshift status                        # show all session statuses
nightshift logs <issue-id>               # tail raw agent output
nightshift history <issue-id>            # conversation timeline
nightshift accept <issue-id>             # merge agent branch into base, clean up
nightshift reject <issue-id>             # discard agent work, remove worktree + session
nightshift cleanup <issue-id>            # remove worktree (optionally keep session)
nightshift watcher                       # start host watcher (pause/unpause, Telegram)

# Direct launch (cli.py start wraps this)
python host/launch.py <issue-id>
python host/launch.py <issue-id> --resume

# Run tests
.venv/bin/python -m pytest tests/
.venv/bin/python -m pytest tests/test_stream_parser.py::test_parse_init_event -v  # single test

# Install dependencies (host-side dev)
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
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
- `session.py` — `SessionRunner`: the main event loop. Streams agent events, handles markers (`@@LOG@@`, `@@CHECKPOINT@@`, `@@QUESTION@@`, `@@WAITING@@`, `@@DONE@@`), manages auto-resume on context limits/stalls. Delegates Q&A to `qa_flow.py`, post-run lifecycle to `post_run.py`, and hook execution to `hooks.py`.
- `hooks.py` — Hook execution for workspace lifecycle events (after_create, before_run, after_run). Extracted from SessionRunner.
- `post_run.py` — Post-run lifecycle: resume logic, done notification (proof-of-work summary), checkpoint summarization. Extracted from SessionRunner.
- `qa_flow.py` — Question/answer flow handling: question raised -> notifier/tracker updated -> answer collected -> delivered. Extracted from SessionRunner.
- `state.py` — `StateManager`: atomic JSON state persistence in `/session/`. Files: `state.json`, `conversation.jsonl`, `raw-output.log`, `resume-prompt.md`, `waiting.json`, `answer.txt`.
- `prompts.py` — Jinja2 template rendering for WORKFLOW.md prompt body, plus fallback prompt builder.
- `search.py` — Keyword-based related issue search across any tracker.

**`adapters/`** — Concrete implementations organized by concern:
- `agents/` — `ClaudeCodeAgent` (fire-and-forget `-p` mode, `--resume` for multi-turn, parses `--output-format stream-json`), `CodexAgent` (stub)
- `trackers/` — `GitBugTracker` (shells out to `git-bug` CLI, v0.10.1 syntax), `StaticTracker` (read-only, reads pre-dumped JSON from session dir — used inside containers)
- `notifiers/` — `TelegramNotifier` (force_reply Q&A with polling thread), `WebhookNotifier`, `CompositeNotifier` (broadcasts to all, Q&A through primary)
- `workspaces/` — `GitWorktreeManager` (creates git worktrees per issue, host-side)

**`host/`** — Host-side scripts (run outside Docker):
- `cli.py` — User-facing CLI: init, start, resume, answer, status, logs, history, accept, reject, cleanup, watcher.
- `launch.py` — Orchestrates workspace setup, issue data dumping, and container launch. Delegates to `workspace_setup.py`, `issue_dump.py`, and `docker_cmd.py`.
- `workspace_setup.py` — Worktree creation, branch management, review session preparation.
- `issue_dump.py` — Dumps `issue.json` and `issues.json` to the session dir for the container's `StaticTracker`.
- `docker_cmd.py` — Builds the `docker run` command with all mounts, env vars, and auth credentials.
- `watcher.py` — Polls session dirs for `waiting.json`, pauses Docker containers, writes `answer.txt` on Telegram reply or CLI input, then unpauses. Zero tracker coupling.
- `env.py` — Shared `.env` file loader used by cli.py, launch.py, and watcher.py.

**`entrypoint.py`** — Container entrypoint. Reads WORKFLOW.md, uses `StaticTracker` for issue data, instantiates other adapters via config factories, runs `SessionRunner`.

## Coding Rules

- **CRITICAL — Never silently catch exceptions.** This is the #1 rule. `except Exception: pass`, `except: continue`, and any catch block that discards the error without logging is FORBIDDEN. Every `except` block MUST log or print the error with enough context to diagnose (the operation, the input, the exception). Use `logging.warning`/`logging.error` or `print(..., file=sys.stderr)`. Code that violates this rule will be rejected in review. No exceptions to this rule.
- **Rebuild Docker image after code changes.** Any change to files that run inside the container (`core/`, `adapters/`, `entrypoint.py`, `docker-entrypoint.sh`, `Dockerfile`) requires rebuilding: `sg docker "docker build -t nightshift:latest ."`
- **Test coverage target: 80%.** Run `coverage run -m pytest tests/ && coverage report` to check. Every new code path must have tests. Current: 66% (2025-03-11).
- **No magic numbers.** Timeouts, thresholds, and retry counts must be named constants, not bare literals.
- **Imports at module top.** No `import shutil` inside functions. No late imports except to break circular dependencies (and those must have a comment explaining why).
- **Functions under 50 lines.** If a function exceeds 50 lines, extract helpers. Long functions are a review blocker.
- **No God objects.** A class should have one responsibility. If it has 5+ unrelated methods, split it.
- **DRY.** If the same pattern appears in 2+ places, extract a shared helper. Duplicated session-state I/O, subprocess calls, and path construction are the most common violations.
- **Requirements traceability.** Every code change and git-bug issue must map to a requirement in `docs/requirements.md`. Before starting work, identify which REQ-xxx it falls under. If no existing requirement covers the change, ask the human whether to add a new REQ-xxx. New tests must be referenced in the traceability matrix. When a change modifies behavior covered by an existing requirement, update the requirement's test list and status if needed. Do not add, remove, or modify requirements without human approval.

## Key Design Patterns

- **Adapter registration**: `core/config.py` has `AGENT_REGISTRY`, `TRACKER_REGISTRY`, etc. mapping `kind` strings to `(module_path, class_name)` tuples. New adapters: add entry to registry + implement the Protocol.
- **WORKFLOW.md**: YAML front matter configures adapters, merge policy, hooks. The markdown body after `---` is the Jinja2 prompt template. `$VAR` references in YAML are resolved from environment variables. `.env` is loaded BEFORE WORKFLOW.md parsing.
- **StaticTracker pattern**: Host dumps issue data to `issue.json` and `issues.json` in the session dir. Container reads them via `StaticTracker`. Write operations (comments, labels) are logged but no-op inside the container.
- **Container-host communication**: Exclusively via shared files in the session directory (`/session/` inside container, `.nightshift/sessions/<id>/` on host). No network calls between container and host.
- **Q&A flow**: Agent outputs `@@QUESTION@@` then `@@WAITING@@` (or exits — 30s timeout auto-triggers waiting). Container writes `waiting.json`. Host watcher pauses the container. Answer arrives via Telegram reply, tracker comment, or `cli.py answer`. Written to `answer.txt`. Container unpaused. Agent restarted with `--resume` and answer as prompt (no stdin — `-p` mode is fire-and-forget).
- **Auto-resume**: On context limit, stall, or max-turns, `SessionRunner` builds a resume prompt with checkpoint history and recent conversation, then restarts the agent in a loop (up to `MAX_RESUMES=10`).
- **Review/merge lifecycle**: Container exits after `@@DONE@@` with status `waiting:review`. Host user runs `cli.py accept <id>` to merge or `cli.py reject <id>` to discard.
- **Auto-start**: Watcher polls tracker for new issues matching a configured label (default `nightshift`), auto-launches `nightshift start` for each. Configured via `auto_start` section in WORKFLOW.md (`enabled`, `label`, `poll_interval_s`, `max_concurrent`). Tracks already-started issues via in-memory set + session dir check. Respects `max_concurrent` to avoid resource exhaustion.

## Testing

**Target: 80% line coverage.** Check with: `.venv/bin/python -m coverage run -m pytest tests/ && .venv/bin/python -m coverage report`

Tests use mock implementations from `tests/conftest.py` (`MockAgent`, `MockTracker`, `MockNotifier`, `MockWorkspaceManager`). Test directories exist for `tests/adapters/` and `tests/integration/` but have no test files yet.

Current test files: `test_stream_parser.py` (Claude Code stream-json parsing), `test_marker_reliability.py` (marker failure modes), `oq1_stdin_test.py` (CLI behavior verification), `test_static_tracker.py`, `test_dotenv.py`, `test_cli_env.py`, `test_accept_reject.py`, `test_worktree_git_fix.py`, `test_review_step.py` (automated review), `test_auto_start.py` (auto-start config and watcher logic).

Coverage gaps (prioritized):
- `host/cli.py` (23%) — cmd_accept, cmd_reject, cmd_init, cmd_revise need tests
- `host/launch.py` (25%) — Docker command construction, worktree setup
- `host/watcher.py` (35%) — review orchestration, container lifecycle, auto-start polling
- `adapters/trackers/git_bug.py` (41%) — git-bug CLI interaction
- `adapters/notifiers/telegram.py` (55%) — Telegram API calls

## Docker

The container runs with `--user $(id -u):$(id -g)` matching the host UID so it can read `600`-permission credential files. Auth credentials are mounted read-only at `/claude-auth` and copied to a writable HOME by `docker-entrypoint.sh`.

Key Docker run pattern:
```bash
docker run --rm --user $(id -u):$(id -g) \
  -v <worktree>:/workspace:rw \
  -v <session-dir>:/session:rw \
  -v <repo>/.git:/repo-git:rw \
  -v ~/.claude:/claude-auth:ro \
  -v <repo>/WORKFLOW.md:/workspace/WORKFLOW.md:ro \
  -e ISSUE_ID=<id> -e SHORT_ID=<short-id> \
  nightshift:latest
```

`docker-entrypoint.sh` rewrites the worktree `.git` pointer to use container paths (`/repo-git/worktrees/agent-<short-id>`), enabling git operations inside the container.

## git-bug

The adapter targets git-bug v0.10.1. Commands are under the `bug` subcommand: `git-bug bug show`, `git-bug bug comment new`, `git-bug bug label new/rm`, `git-bug bug status close/open`. The top-level `git-bug pull`/`push` are used for sync.

## Dependencies

Python 3.12+. Runtime: `requests`, `pyyaml`, `jinja2`. The Docker image also installs `git-bug` v0.10.1 and `@anthropic-ai/claude-code` (npm).
