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
nightshift history <issue-id>            # conversation timeline (-f to follow)
nightshift accept <issue-id>             # merge agent branch into base, clean up
nightshift revise <issue-id> "msg"       # request revisions (review or mid-flight)
nightshift reject <issue-id>             # discard agent work, remove worktree + session
nightshift cleanup <issue-id>            # remove worktree (optionally keep session)
nightshift upgrade                       # show prompt updates from canonical template (--apply to patch)
nightshift issue <args...>               # pass args to tracker CLI with lock retry
nightshift watcher                       # start host watcher (pause/unpause, Telegram)

# Monitor sessions (Claude Code skill)
# Use /nightshift-watch to poll status, review diffs, and accept/reject/revise.
# See ~/.claude/skills/nightshift-watch/SKILL.md for full protocol.

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
- `config/` — Package: `models.py` (typed dataclasses), `loader.py` (YAML front matter parsing, env var resolution), `factories.py` (adapter registries and dynamic instantiation). `__init__.py` re-exports all public symbols for backward compatibility (`from core.config import load_workflow` still works).
- `session.py` — `SessionRunner`: the main event loop. Streams agent events, handles markers (`@@LOG@@`, `@@CHECKPOINT@@`, `@@QUESTION@@`, `@@WAITING@@`, `@@DONE@@`), manages auto-resume on context limits/stalls. Delegates Q&A to `qa_flow.py`, post-run lifecycle to `post_run.py`, and hook execution to `hooks.py`.
- `hooks.py` — Hook execution for workspace lifecycle events (after_create, before_run, after_run). Extracted from SessionRunner.
- `post_run.py` — Post-run lifecycle: resume logic, done notification (proof-of-work summary), checkpoint summarization. Extracted from SessionRunner.
- `qa_flow.py` — Question/answer flow handling: question raised -> notifier/tracker updated -> answer collected -> delivered. Extracted from SessionRunner.
- `state.py` — `StateManager`: atomic JSON state persistence in `/session/`. Files: `state.json`, `conversation.jsonl`, `raw-output.log`, `resume-prompt.md`, `waiting.json`, `answer.txt`.
- `prompts.py` — Jinja2 template rendering for WORKFLOW.md prompt body, plus fallback prompt builder.
- `rebase.py` — Pre-review rebase: fetch latest base branch, rebase, run tests, resume agent on failure.
- `search.py` — Keyword-based related issue search across any tracker.
- `upgrade.py` — Template versioning and upgrade logic: version comparison, prompt-section diffing, and apply-upgrade for WORKFLOW.md.

**`adapters/`** — Concrete implementations organized by concern:
- `agents/` — `ClaudeCodeAgent` (fire-and-forget `-p` mode, `--resume` for multi-turn, parses `--output-format stream-json`), `CodexAgent` (stub)
- `trackers/` — `GitBugTracker` (shells out to `git-bug` CLI, v0.10.1 syntax), `StaticTracker` (file-backed tracker for containers — reads pre-dumped JSON, supports `reload()` for mtime-based re-read, appends write operations to `tracker-outbox.jsonl` for host processing)
- `notifiers/` — `TelegramNotifier` (force_reply Q&A with polling thread), `WebhookNotifier`, `CompositeNotifier` (broadcasts to all, Q&A through primary)
- `workspaces/` — `GitWorktreeManager` (creates git worktrees per issue, host-side)

**`host/`** — Host-side scripts (run outside Docker):
- `cli.py` — User-facing CLI: init, start, resume, answer, status, logs, history, accept, reject, cleanup, upgrade, watcher, issue.
- `launch.py` — Orchestrates workspace setup, issue data dumping, and container launch. Delegates to `workspace_setup.py`, `issue_dump.py`, and `docker_cmd.py`.
- `workspace_setup.py` — Worktree creation, branch management, review session preparation.
- `issue_dump.py` — Dumps `issue.json` and `issues.json` to the session dir for the container's `StaticTracker`. Also provides `redump_issue()` for live sync (watcher re-dumps periodically so the container sees new comments).
- `docker_cmd.py` — Builds the `docker run` command with all mounts, env vars, and auth credentials.
- `watcher/` — Package split by concern: `host_watcher.py` (main loop), `telegram_relay.py` (Telegram polling), `qa_handler.py` (Q&A flow), `review_orchestrator.py` (auto-review launch/verdict), `session_monitor.py` (orphan detection, cleanup), `command_executor.py` (CLI command dispatch), `verdict_handler.py` (approve/revise handling), `issue_sync.py` (bidirectional file-based sync: outbox processing + issue.json re-dump), `main.py` (entry point). Run via `python -m host.watcher`.
- `session_utils.py` — Shared session state I/O (read/write state.json), path helpers, worktree cleanup.
- `constants.py` — Named constants for timeouts, thresholds, polling intervals (replaces magic numbers).
- `git_utils.py` — Git command wrappers (branch detection, merge, diff).
- `docker_utils.py` — Docker container management (pause/unpause/stop/status).
- `merge.py` — Merge execution and conflict validation logic extracted from cli.py.
- `env.py` — Shared `.env` file loader used by cli.py, launch.py, and watcher.
- `config_discovery.py` — Workflow file discovery: CLI flag > `.nightshift.yaml` pointer > `WORKFLOW.md` default. Also writes `.nightshift.yaml` for `init --workflow-path`.

**`entrypoint.py`** — Container entrypoint. Reads WORKFLOW.md, uses `StaticTracker` for issue data, instantiates other adapters via config factories, runs `SessionRunner`.

## Coding Rules

- **CRITICAL — Never silently catch exceptions.** This is the #1 rule. `except Exception: pass`, `except: continue`, and any catch block that discards the error without logging is FORBIDDEN. Every `except` block MUST log or print the error with enough context to diagnose (the operation, the input, the exception). Use `logging.warning`/`logging.error` or `print(..., file=sys.stderr)`. Code that violates this rule will be rejected in review. No exceptions to this rule.
- **Rebuild Docker image after code changes.** Any change to files that run inside the container (`core/`, `adapters/`, `entrypoint.py`, `docker-entrypoint.sh`, `Dockerfile`) requires rebuilding: `sg docker "docker build -t nightshift:latest ."`
- **Test coverage target: 80%.** Run `coverage run -m pytest tests/ && coverage report` to check. Every new code path must have tests. Current: 93% (2025-03-12).
- **No magic numbers.** Timeouts, thresholds, and retry counts must be named constants, not bare literals.
- **Imports at module top.** No `import shutil` inside functions. No late imports except to break circular dependencies (and those must have a comment explaining why).
- **Functions under 50 lines.** If a function exceeds 50 lines, extract helpers. Long functions are a review blocker.
- **No God objects.** A class should have one responsibility. If it has 5+ unrelated methods, split it.
- **DRY.** If the same pattern appears in 2+ places, extract a shared helper. Duplicated session-state I/O, subprocess calls, and path construction are the most common violations.
- **Requirements traceability.** Every code change and git-bug issue must map to a requirement in `docs/requirements.md`. Before starting work, identify which REQ-xxx it falls under. If no existing requirement covers the change, ask the human whether to add a new REQ-xxx. New tests must be referenced in the traceability matrix. When a change modifies behavior covered by an existing requirement, update the requirement's test list and status if needed. Do not add, remove, or modify requirements without human approval.

## Key Design Patterns

- **Adapter registration**: `core/config/factories.py` has `AGENT_REGISTRY`, `TRACKER_REGISTRY`, etc. mapping `kind` strings to `(module_path, class_name)` tuples. New adapters: add entry to registry + implement the Protocol.
- **Tracker CLI passthrough**: `nightshift issue <args...>` passes all arguments directly to the tracker's `run_raw(*args)` method. For git-bug, this routes through `_run()` to get lock retry for free. Trackers without a CLI (StaticTracker, GitHubIssuesTracker) raise `NotImplementedError`.
- **WORKFLOW.md**: YAML front matter configures adapters, merge policy, hooks. The markdown body after `---` is the Jinja2 prompt template. `$VAR` references in YAML are resolved from environment variables. `.env` is loaded BEFORE WORKFLOW.md parsing.
- **StaticTracker pattern**: Host dumps issue data to `issue.json` and `issues.json` in the session dir. Container reads them via `StaticTracker`. Write operations (comments, labels, status) are appended to `tracker-outbox.jsonl` for host processing. `StaticTracker.reload()` re-reads `issue.json` when mtime changes, returning new comments for injection into the agent's next prompt.
- **Container-host communication**: Exclusively via shared files in the session directory (`/session/` inside container, `.nightshift/sessions/<id>/` on host). No network calls between container and host.
- **Q&A flow**: Agent outputs `@@QUESTION@@` then `@@WAITING@@` (or exits — 30s timeout auto-triggers waiting). Container writes `waiting.json`. Host watcher pauses the container. Answer arrives via Telegram reply, tracker comment, or `cli.py answer`. Written to `answer.txt`. Container unpaused. Agent restarted with `--resume` and answer as prompt (no stdin — `-p` mode is fire-and-forget).
- **Auto-resume**: On context limit, stall, or max-turns (coder sessions only), `SessionRunner` builds a resume prompt with checkpoint history and recent conversation, then restarts the agent in a loop (up to `MAX_RESUMES=10`). Review sessions are excluded from max-turns auto-resume — they fall back to human review instead (see Review/merge lifecycle). Auth failures are excluded from auto-resume (they set `suspended:auth-failure` instead) since the fix is external (token refresh).
- **Auth-failure detection**: `ClaudeCodeAgent._parse()` checks agent output for auth-related error patterns (invalid key, expired token, unauthorized, etc.) and emits `AgentEventType.AUTH_FAILURE`. `SessionRunner` handles this by setting `suspended:auth-failure` status, committing WIP, and notifying the user. The watcher's `SessionMonitor.check_auth_failures()` retries these sessions on a slow interval (`AUTH_RETRY_INTERVAL_S`, 5 min) up to `MAX_AUTH_RETRIES` (6 attempts). After the limit, the session transitions to `suspended:auth-failure-permanent` and requires manual `nightshift resume`.
- **Review/merge lifecycle**: Container exits after `@@DONE@@` with status `waiting:review`. Host user runs `cli.py accept <id>` to merge or `cli.py reject <id>` to discard. `cmd_accept` verifies the agent branch is not behind the base branch before merging — if it is, the accept is rejected with a message to `nightshift resume` first. `cmd_revise` supports two modes: for review sessions (`waiting:review`, `waiting:human-review`) it collects tracker comments as feedback; for running sessions (`working`, `starting`) it stops the container, writes the operator's inline message as a mid-flight course-correction prompt, and relaunches with `--resume`. When a review session hits max-turns, `post_run.py` scans the conversation for a verdict (`scan_conversation_for_verdict` using `parse_nightshift_command`): if found, the review is treated as done (`waiting:review`); if not, it sets `suspended:review-no-verdict` and the watcher's `ReviewOrchestrator._handle_review_no_verdict` transitions the coder session to `waiting:human-review`.
- **Auto-start**: Watcher polls tracker for new issues matching a configured label (default `nightshift`), auto-launches `nightshift start` for each. Configured via `auto_start` section in WORKFLOW.md (`enabled`, `label`, `poll_interval_s`, `max_concurrent`). Tracks already-started issues via in-memory set + session dir check. Respects `max_concurrent` to avoid resource exhaustion.
- **Graceful shutdown**: `host/watcher/main.py` installs SIGTERM/SIGINT handlers that set a module-level `threading.Event`. This event is passed to `HostWatcher.run()` and propagated to the `GitBugTracker._shutdown` event. The main loop uses `event.wait(timeout=...)` instead of `time.sleep()` so it can exit immediately. `GitBugTracker._run_interruptible()` polls the subprocess with a short sleep loop, checking `_shutdown` each iteration — this allows signals to interrupt blocking git-bug calls. On loop exit, `terminate_current()` kills any in-flight tracker subprocess via the shared `_graceful_kill()` helper (terminate → wait → escalate to kill). `cmd_watcher` in `cli.py` uses `os.execvpe()` to replace the CLI process with the watcher process, so `kill <pid>` delivers signals directly — no orphan child.
- **Pre-review rebase**: When the agent outputs `@@DONE@@`, `post_run.py` calls `core/rebase.py` to rebase the agent branch onto the latest base branch (via `WorkspaceManager.rebase()`). If a `test_command` is configured in WORKFLOW.md's `workspace` section, the test suite is re-run after rebase. On conflict or test failure, the agent is resumed with a descriptive prompt so it can fix the issues and re-submit. On success, the session transitions to `waiting:review` as normal.
- **Notification level filtering**: Each notifier supports a `level` setting (`questions`, `actions`, `all`) parsed from WORKFLOW.md. The `NotificationLevel` enum in `core/protocols.py` orders levels by increasing verbosity. The `should_notify(configured, message)` helper gates delivery — adapters check it in `notify()`. Every `notify()` call site tags its message with an explicit level. `send_question()` always bypasses the filter (it's the Q&A round-trip, not just a notification).
- **Merge-needed handoff**: When the host resumes a session (`workspace_setup.merge_base_into_worktree`), it fetches and merges the latest base branch into the agent worktree. If the merge conflicts, it aborts, writes `merge-needed.txt` to the session dir (fields: `merge_target`, `base_branch`, separator `---`, then conflict output). On container startup, `entrypoint._read_merge_instructions()` reads and deletes the file, prepending merge instructions to the agent prompt so the agent resolves conflicts, runs tests, and continues. The filename constant lives in `core/constants.MERGE_NEEDED_FILENAME`.
- **Hot-reload config (SIGHUP)**: `host/watcher/main.py` installs a SIGHUP handler that sets a module-level `reload_event`. The main loop in `HostWatcher.run()` checks this event each iteration and calls `reload_config()`. `reload_config()` re-parses the workflow file via `load_workflow()`, diffs old vs new config with `_diff_config()`, updates the TelegramRelay notification level (via `set_level()`), toggles auto-start, recreates the tracker, propagates the shutdown event to the new tracker, and logs which sections changed. On parse error, the previous config is kept intact and an error is logged.
- **Live issue sync**: Bidirectional file-based sync between host and container, run by `host/watcher/issue_sync.py` each watcher loop. **Reads (host→container):** `redump_issue()` in `host/issue_dump.py` atomically re-writes `issue.json` (including comments) for active sessions. `StaticTracker.reload()` detects mtime changes and returns new comments. `SessionRunner._try_reload_tracker()` accumulates them and `_inject_new_comments()` prepends them to the next resume prompt. **Writes (container→host):** `StaticTracker._append_outbox()` appends JSON-lines entries to `tracker-outbox.jsonl`. The watcher's `process_outbox()` atomically renames the outbox to `.processing`, applies entries via the real tracker, then deletes the file. Atomic rename prevents losing entries the container appends concurrently. If `.processing` exists on the next cycle (crash recovery), it is processed first.
- **Template versioning**: WORKFLOW.md includes a `template_version` field in its YAML front matter. The canonical template lives in `templates/WORKFLOW.md` and is the single source of truth — `nightshift init` reads it via `core/upgrade.load_canonical_template()`. `nightshift upgrade` compares the project's version against the canonical version, shows a diff of the prompt section only (YAML config is never touched), and with `--apply` patches the prompt and bumps `template_version`. `core/upgrade.py` contains all versioning logic: splitting front matter, diffing prompt sections, and applying upgrades.
- **Lifecycle comments**: `host/watcher/lifecycle_comments.py` posts brief summary comments to the issue tracker at key session transitions (start, resume, question, done, revise). Only the watcher posts — the container's StaticTracker stays read-only. Each calling module tracks which events have been posted via `_posted_*` sets to avoid duplicates. Comments are posted via `_safe_post()` which logs failures without raising. Integrated into `qa_handler.py` (question), `review_orchestrator.py` (done), `session_monitor.py` (start, resume), and `verdict_handler.py` (revise).

## Testing

**Target: 80% line coverage.** Check with: `.venv/bin/python -m coverage run -m pytest tests/ && .venv/bin/python -m coverage report`

Tests use mock implementations from `tests/conftest.py` (`MockAgent`, `MockTracker`, `MockNotifier`, `MockWorkspaceManager`). 675 tests across `tests/` and `tests/watcher/`.

Key test files: `test_upgrade.py`, `test_stream_parser.py`, `test_marker_reliability.py`, `oq1_stdin_test.py`, `test_static_tracker.py`, `test_dotenv.py`, `test_cli_env.py`, `test_cli_commands.py`, `test_cli_helpers.py`, `test_cli_issue.py`, `test_accept_reject.py`, `test_worktree_git_fix.py`, `test_review_step.py`, `test_review.py`, `test_auto_start.py`, `test_session_runner.py`, `test_hooks.py`, `test_post_run.py`, `test_qa_flow.py`, `test_prompts.py`, `test_config_factories.py`, `test_config_discovery.py`, `test_docker_utils.py`, `test_git_utils.py`, `test_composite_notifier.py`, `test_notifier_prefix.py`, `test_notification_level.py`, `test_rebase.py`, `test_search.py`, `test_session_utils_host.py`, `test_launch.py`, `test_post_container.py`, `test_assistant_text_logging.py`, `test_workspace_setup.py`, `test_entrypoint_merge.py`, `test_issue_redump.py`, `watcher/test_qa_handler.py`, `watcher/test_host_watcher.py`, `watcher/test_review_orchestrator.py`, `watcher/test_telegram_relay.py`, `watcher/test_session_monitor.py`, `watcher/test_graceful_shutdown.py`, `watcher/test_lifecycle_comments.py`, `watcher/test_issue_sync.py`.

Remaining coverage gaps:
- `adapters/trackers/git_bug.py` — git-bug CLI interaction (hard to test without git-bug binary)
- `adapters/notifiers/telegram.py` — Telegram API calls (requires mocking HTTP)

## Docker

The container runs with `--user $(id -u):$(id -g)` matching the host UID so it can read `600`-permission credential files. Auth credentials are mounted read-only at `/claude-auth` and copied to a writable HOME by `docker-entrypoint.sh`.

Key Docker run pattern (no `-it` flag — containers are fire-and-forget):
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

Container naming: coder containers are `nightshift-<short-id>`, review containers are `nightshift-review-<short-id>`. The watcher detects review sessions by the `review-` prefix in session IDs and passes `--step review --workflow REVIEW.md` when auto-resuming them.

`docker-entrypoint.sh` rewrites the worktree `.git` pointer to use container paths (`/repo-git/worktrees/agent-<short-id>`), enabling git operations inside the container.

When launching the watcher from a different repo (e.g. `nightshift watcher` from `jessica-ng/`), `cli.py` injects `PYTHONPATH` pointing to the agent-worker root so `python -m host.watcher` resolves correctly.

## git-bug

The adapter targets git-bug v0.10.1. Commands are under the `bug` subcommand: `git-bug bug show`, `git-bug bug comment new`, `git-bug bug label new/rm`, `git-bug bug status close/open`. The top-level `git-bug pull`/`push` are used for sync.

## Dependencies

Python 3.12+. Runtime: `requests`, `pyyaml`, `jinja2`. The Docker image also installs `git-bug` v0.10.1 and `@anthropic-ai/claude-code` (npm).
