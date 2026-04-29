# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Nightshift is an autonomous coding agent runner. It launches a coding agent (e.g. Claude Code) inside a Docker container against an issue from a tracker (e.g. git-bug), manages the session lifecycle (checkpoints, Q&A, resume on context limits), and handles review/merge via the host CLI.

## Commands

```bash
# Build Docker image
docker build -t nightshift:latest .

# CLI commands (use `nightshift` if installed, otherwise `python host/cli.py`)
nightshift init                          # scaffold WORKFLOW.md, .env.example, .nightshift/, pre-commit hook
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
nightshift upstream                      # propose local prompt improvements to canonical (--dry-run to preview)
nightshift issue <args...>               # pass args to tracker CLI with lock retry
nightshift-client daemon start           # start client-side tracker daemon in background
nightshift-client daemon stop            # stop client-side tracker daemon
nightshift-client daemon status          # show client-side tracker daemon status
nightshift watcher                       # start host watcher (pause/unpause, Telegram)
nightshift watchdog                       # start global watchdog daemon
nightshift watchdog --list               # list registered watchers and status
nightshift watchdog --check              # one-shot health check (--no-alerts to suppress)
nightshift watchdog --config <path>      # use custom watchdog.yaml config
nightshift watchdog -v                   # verbose output
nightshift usage [issue-id]               # show token usage and cost per session
nightshift export-training-data          # export finetuning data from session logs
nightshift blocked                       # list issues blocked by dependencies
nightshift force-status <issue-id> <status>  # force-set session status (bypasses SSM validation)

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

## Destructive Commands Requiring Confirmation

Never run these without explicit user approval:
- `nightshift reject` — discards all agent work (commits, code changes)
- `git checkout` on WORKFLOW.md/REVIEW.md — loses local config customizations
- `git reset --hard` — loses uncommitted changes

## Architecture

The system has a strict three-layer split:

**`core/`** — Protocol-based core, no concrete adapter imports. All external boundaries defined as `Protocol` classes in `core/protocols.py`:
- `CodingAgent` — start/stream/send_input/terminate lifecycle
- `IssueTracker` — CRUD for issues, comments, labels, sync
- `WorkspaceManager` — create/commit/finalize workspaces
- `Notifier` — notify + round-trip Q&A (send_question/check_answer)
- `UsageData` — dataclass for accumulated token usage and cost per session (input_tokens, output_tokens, cost_usd, model)

Key core modules:
- `agent_events.py` — Single source of truth for `AgentEventType` enum and `AgentEvent` dataclass. Defines the unified event stream for all agent adapters (REQ-030). Event types: STARTED, TEXT, TOOL_CALL, TOOL_RESULT, QUESTION, CHECKPOINT, DONE, ERROR, AUTH_FAILURE, SYSTEM, PROCESS_EXIT, STALL, PROVIDER_OVERLOAD, UNKNOWN. Provides `to_dict()` and `from_dict()` serialization methods. Re-exported by `protocols.py` for backward compatibility.
- `config/` — Package: `models.py` (typed dataclasses), `loader.py` (YAML front matter parsing, env var resolution), `factories.py` (adapter registries and dynamic instantiation). `__init__.py` re-exports all public symbols for backward compatibility (`from core.config import load_workflow` still works).
- `session.py` — `SessionRunner`: the main event loop. Streams agent events, handles markers (`@@LOG@@`, `@@CHECKPOINT@@`, `@@QUESTION@@`, `@@WAITING@@`, `@@DONE@@`), manages auto-resume on context limits/stalls. Delegates Q&A to `qa_flow.py`, post-run lifecycle to `post_run.py`, and hook execution to `hooks.py`.
- `hooks.py` — Hook execution for workspace lifecycle events (after_create, before_run, after_run). Extracted from SessionRunner.
- `post_run.py` — Post-run lifecycle: resume logic, done notification (proof-of-work summary with cost line), checkpoint summarization. Exports `format_cost_line()` used by both container and host proof-of-work comments. Extracted from SessionRunner.
- `qa_flow.py` — Question/answer flow handling: question raised -> notifier/tracker updated -> answer collected -> delivered. Extracted from SessionRunner.
- `state.py` — `StateManager`: atomic JSON state persistence in `/session/`. Files: `state.json`, `conversation.jsonl`, `raw-output.log`, `resume-prompt.md`, `waiting.json`, `answer.txt`. `SessionState.completed_at` is set by `mark_completed()` when a session finishes normally (@@DONE@@ → `notify_done()`), used by the orphan detector to distinguish successful exits from crashes. `SessionState.usage` (`UsageData`) accumulates token counts/cost across resumes via `update_usage()`. **File locking**: All read-modify-write operations on `state.json` use `state_lock()` (fcntl.flock on `state.json.lock`) to prevent race conditions between container and host. Host-side helpers in `host/session_utils.py` (`update_status`, `increment_orphan_resumes`, `update_state_fields`, etc.) also use the same lock. **SSM validation**: `StateManager.update_status()` and `mark_done()` validate all status transitions through the `SessionStateMachine` before writing; invalid transitions raise `InvalidTransition`.
- `prompts.py` — Jinja2 template rendering for WORKFLOW.md prompt body, plus fallback prompt builder.
- `rebase.py` — Pre-review rebase logic (legacy, no longer called from container — see `host/rebase.py` for host-side implementation).
- `search.py` — Keyword-based related issue search across any tracker.
- `upgrade.py` — Template versioning and upgrade logic: `TemplateVersion` (major.minor format, backward-compatible with plain int), version comparison, prompt-section diffing, and apply-upgrade for WORKFLOW.md and REVIEW.md. Both canonical templates live in `templates/` with `template_version` in YAML front matter. Minor bumps for additive changes, major bumps for behavioral changes (replacements, consolidations). Major bumps require `--force` on `nightshift upgrade --apply`.
- `upstream.py` — Upstream proposal logic: reverse diff (project→canonical), operation type detection (add/replace/consolidate), client-side validation (blocklist terms, Jinja2 variable whitelist, line count caps), and `UpstreamProposal` formatting. Used by `nightshift upstream`.
- `tracker_ipc.py` — IPC protocol for single-writer tracker architecture: `TrackerRequest`/`TrackerResponse` dataclasses (JSON-lines encoding), serialization helpers, `execute_tracker_method()` dispatcher, `recv_json_line()` shared socket utility, and `TrackerIPCBase` (base class providing all IssueTracker method implementations for IPC-backed clients — subclasses only override `_call()`).

**`adapters/`** — Concrete implementations organized by concern:
- `agents/` — `HeadlessAgentBase` in `base.py` (shared process lifecycle: stream with select, stall detection, terminate, `_is_auth_failure()` classmethod with per-subclass `AUTH_FAILURE_PATTERNS`, `_is_transient_error()` for 500/502/503/504/429/rate-limit detection with automatic retry via `_maybe_retry_transient()` using exponential backoff delays `TRANSIENT_RETRY_DELAYS`). `ClaudeCodeAgent` (fire-and-forget `-p` mode, `--resume` for multi-turn, parses `--output-format stream-json`), `OpenHandsAgent` (headless `--json` mode, `--resume` for multi-turn, parses `--JSON Event--`-separated JSON events), `CodexAgent` (headless `exec --json` mode, `exec resume <thread_id>` for multi-turn, parses JSONL events keyed by `type` field)
- `trackers/` — `GitBugTracker` (shells out to `git-bug` CLI, v0.10.1 syntax), `GitBugGraphQLTracker` (spawns `git-bug webui` subprocess on init at `adapters/trackers/git_bug_graphql.py:56`, routes operations through GraphQL API — used when `tracker.kind: git-bug-graphql`), `StaticTracker` (file-backed tracker for containers — reads pre-dumped JSON, supports `reload()` for mtime-based re-read, appends write operations to `tracker-outbox.jsonl` for host processing), `SocketTrackerClient` (connects to watcher's Unix socket for zero-contention tracker access; extends `TrackerIPCBase` from `core/tracker_ipc`)
- `notifiers/` — `TelegramNotifier` (force_reply Q&A with polling thread), `WebhookNotifier`, `CompositeNotifier` (broadcasts to all, Q&A through primary)
- `workspaces/` — `GitWorktreeManager` (creates git worktrees per issue, host-side)

**`host/`** — Host-side scripts (run outside Docker):
- `cli.py` — User-facing CLI: init, start, resume, answer, status, logs, history, accept, reject, cleanup, upgrade, usage, watcher, watchdog, issue.
- `launch.py` — Orchestrates workspace setup, issue data dumping, and container launch. Delegates to `workspace_setup.py`, `issue_dump.py`, and `docker_cmd.py`.
- `workspace_setup.py` — Worktree creation, branch management, review session preparation.
- `issue_dump.py` — Dumps `issue.json` and `issues.json` to the session dir for the container's `StaticTracker`. Also provides `redump_issue()` for live sync (watcher re-dumps periodically so the container sees new comments).
- `docker_cmd.py` — Builds the `docker run` command with all mounts, env vars, and auth credentials. When `agent.kind` is `openhands`, passes through `LLM_API_KEY`, `LLM_MODEL`, and `LLM_BASE_URL` env vars (OpenHands uses litellm under the hood for multi-provider LLM support).
- `tracker_client.py` — `get_tracker_with_fallback()`: probes the watcher's Unix socket; returns `SocketTrackerClient` when available, otherwise falls back to `create_tracker()` (direct GitBugTracker with lock retry). Used by CLI commands and launch.py instead of `create_tracker()` directly.
- `watcher/` — Package split by concern: `host_watcher.py` (main loop), `telegram_relay.py` (Telegram polling), `qa_handler.py` (Q&A flow), `review_orchestrator.py` (auto-review launch/verdict), `session_monitor.py` (orphan detection, cleanup), `command_executor.py` (CLI command dispatch), `verdict_handler.py` (approve/revise handling), `issue_sync.py` (bidirectional file-based sync: outbox processing + issue.json re-dump), `tracker_writer.py` (single-writer thread, Unix socket server, queue proxy — see Single-writer pattern below), `main.py` (entry point). Run via `python -m host.watcher`.
- `watchdog/` — Global watchdog for monitoring multiple watcher instances. New pipeline: `scanner.py` discovers `~/.nightshift/projects.d/*.yaml` registrations and, by default, yields only live registrations while removing dead ones from the new watchdog path; `rules.py` detects stale logs / repeated errors / error thresholds; `llm.py` optionally runs Ollama or OpenRouter analysis; `notify.py` sends Telegram/webhook/file alerts; `config.py` loads `~/.nightshift/watchdog.yaml`; and `main.py` ties it together with `--list`, `--check`, and daemon mode. The legacy `scan_registrations()` path remains for older status checks. Run via `nightshift watchdog`.
- `session_utils.py` — Shared session state I/O (read/write state.json), path helpers, worktree cleanup. **SSM validation**: `update_status()` and `update_state_fields()` validate status transitions through `SessionStateMachine`; invalid transitions raise `InvalidTransition`.
- `constants.py` — Named constants for timeouts, thresholds, polling intervals (replaces magic numbers).
- `git_utils.py` — Git command wrappers (branch detection, merge, diff).
- `docker_utils.py` — Docker container management (pause/unpause/stop/status).
- `merge.py` — Merge execution and conflict validation logic extracted from cli.py. Rebases in the worktree (not main repo) to avoid polluting main repo with conflict markers.
- `rebase.py` — Host-side pre-review rebase: fetch latest base branch, rebase agent worktree, run tests. Called by `review_orchestrator.py` before launching review. Runs on host (not in container) to avoid bind-mount issues with git operations.
- `env.py` — Shared `.env` file loader used by cli.py, launch.py, and watcher.
- `config_discovery.py` — Workflow file discovery: CLI flag > `.nightshift.yaml` pointer > `WORKFLOW.md` default. Also writes `.nightshift.yaml` for `init --workflow-path`.

**`nightshift-client/`** — Client-side tracker daemon and CLI used to serialize git-bug operations over `.nightshift-client/tracker.sock`. `_daemon.py` owns the writer queue, Unix socket server, and PID-file helpers; `cli.py` exposes `daemon start|stop|status|run`.

**`entrypoint.py`** — Container entrypoint. Reads WORKFLOW.md, uses `StaticTracker` for issue data, instantiates other adapters via config factories, wraps the run in `WorkspaceTransaction`, and exposes `--cleanup` for EXIT-trap pointer restoration plus `core.worktree` sanitization.

**`hooks/`** — Git hooks installed by `nightshift init`:
- `pre-commit` — Rejects commits containing conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`). Safety net to prevent accidental commits of unresolved conflicts.

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
- **All code changes go through nightshift.** Do not implement features directly — file git-bug issues, label them `nightshift`, and let the agent work on them. This ensures provenance: every change has an issue, a session trace, and a review. The only exception is if the human explicitly says to implement directly.

## Issue Filing Guidelines

When filing git-bug issues for nightshift to work on, follow TDD: every issue must specify tests first, implementation second.

### Issue body structure

All issues must include these sections in order:

1. **Tests** — list test file, test names, and what each test asserts. These are written first and must fail before implementation.
2. **Files** — which files to create or modify.
3. **Implementation** — step-by-step instructions with exact file paths, function signatures, and behavior.
4. **Patterns to follow** — reference existing code the agent should imitate.
5. **REQ** — which REQ-xxx this maps to.

### Granularity by agent kind

**For `agent.kind: openhands` or `overflow.agent_kind: openhands`** — decompose into micro-issues:
- Each issue touches at most 2-3 files.
- One clear deliverable per issue (not "add feature X" but "add X to file Y with Z behavior").
- Include exact file paths, function signatures, and expected test names.
- Spell out implementation steps, not just the goal.
- Include code snippets for patterns to follow (copy from existing code).
- State dependencies explicitly: `Depends on: <issue-id-prefix>`. Don't label dependent issues with `nightshift` until the parent is accepted.

**For `agent.kind: claude-code`** — high-level issues are fine. Claude Code can read the codebase and infer details. Still include the test section with expected test names.

### Example issue body (OpenHands-level detail)

```
## Tests (write first, must fail before implementation)
- tests/test_foo.py::test_foo_returns_true_for_valid_input
  Assert: `foo("hello")` returns `True`
- tests/test_foo.py::test_foo_raises_on_empty
  Assert: `foo("")` raises `ValueError`
- tests/test_config_factories.py::test_foo_registered
  Assert: `"foo"` in `FOO_REGISTRY`

## Files
- adapters/foo.py (create)
- core/config/factories.py (modify — add to FOO_REGISTRY)

## Implementation
1. Create `adapters/foo.py` with class `Foo` implementing `FooProtocol`
2. Constructor accepts `bar: str`, `timeout_s: float = 30.0`
3. Method `process()` does X, returns Y
4. In `core/config/factories.py`, add `"foo": ("adapters.foo", "Foo")` to `FOO_REGISTRY`

## Patterns to follow
See `adapters/agents/claude_code.py` for the existing adapter pattern.

REQ: REQ-031
```

## Key Design Patterns

- **Adapter registration**: `core/config/factories.py` has `AGENT_REGISTRY`, `TRACKER_REGISTRY`, etc. mapping `kind` strings to `(module_path, class_name)` tuples. New adapters: add entry to registry + implement the Protocol.
- **Tracker CLI passthrough**: `nightshift issue <args...>` passes all arguments directly to the tracker's `run_raw(*args)` method. For git-bug, this routes through `_run()` to get lock retry for free. Trackers without a CLI (StaticTracker, GitHubIssuesTracker) raise `NotImplementedError`.
- **WORKFLOW.md**: YAML front matter configures adapters, merge policy, hooks. The markdown body after `---` is the Jinja2 prompt template. `$VAR` references in YAML are resolved from environment variables. `.env` is loaded BEFORE WORKFLOW.md parsing.
- **StaticTracker pattern**: Host dumps issue data to `issue.json` and `issues.json` in the session dir. Container reads them via `StaticTracker`. Write operations (comments, labels, status) are appended to `tracker-outbox.jsonl` for host processing. `StaticTracker.reload()` re-reads `issue.json` when mtime changes, returning new comments for injection into the agent's next prompt.
- **Container-host communication**: Exclusively via shared files in the session directory (`/session/` inside container, `.nightshift/sessions/<id>/` on host). No network calls between container and host.
- **File signal fallback**: For agents that cannot use MCP tools (e.g., OpenHands, weak models), `SessionRunner` polls `/session/signal/` for file-based signals each event loop iteration. Signal files: `done` (empty or ignored), `checkpoint` (plain text description), `question.json` (`{"question": "..."}` JSON). Files are unlinked after detection to prevent re-triggering. `SessionRunner._check_file_signals()` performs cheap stat() checks — no inotify needed. The signal directory is created by `SessionRunner.__init__()`. This is Phase 3 of the signal protocol, complementing MCP tool signals (Phase 1) and text markers (Phase 2).
- **Signal method configuration**: The `signal_method` config in WORKFLOW.md's `agent` section controls which signal detection mechanisms `SessionRunner` checks and which format adapters emit: `auto` (default, emits file + text + DONE), `mcp` (DONE event only), `text` (text markers like `@@DONE@@` only), `file` (writes `/session/signal/done` only). This allows per-agent optimization — e.g., `signal_method: mcp` for Claude Code (which reliably uses MCP tools), `signal_method: file` for OpenHands (which cannot use MCP). Configured in `AgentConfig.signal_method`, validated by `loader._parse_agent_config()` against allowed values `{"auto", "mcp", "text", "file"}`.
- **Q&A flow**: Agent outputs `@@QUESTION@@` then `@@WAITING@@` (or exits — 30s timeout auto-triggers waiting). Container writes `waiting.json`. Host watcher pauses the container. Answer arrives via Telegram reply, tracker comment, or `cli.py answer`. Written to `answer.txt`. Container unpaused. Agent restarted with `--resume` and answer as prompt (no stdin — `-p` mode is fire-and-forget).
- **Auto-resume**: On context limit, stall, or max-turns (coder sessions only), `SessionRunner` builds a resume prompt with checkpoint history and recent conversation, then restarts the agent in a loop (up to `MAX_RESUMES=10`). Review sessions are excluded from max-turns auto-resume — they fall back to human review instead (see Review/merge lifecycle). Auth failures are excluded from auto-resume (they set `suspended:auth-failure` instead) since the fix is external (token refresh).
- **Auth-failure detection**: `HeadlessAgentBase._is_auth_failure()` (classmethod in `adapters/agents/base.py`) checks text against the subclass's `AUTH_FAILURE_PATTERNS` tuple for permanent auth failures (401, invalid key, expired token, unauthorized). Transient errors (429, 500-504, rate limits, overloaded) are handled separately by `_is_transient_error()` with automatic retry and backoff in the base class — these never surface as AUTH_FAILURE. `ClaudeCodeAgent` detects Claude-specific auth patterns in `result`, `error`, and `system` events. `OpenHandsAgent` detects litellm/LLM API auth patterns (HTTP 401/404, invalid key, litellm errors) in `ObservationEvent` with `is_error=true`. Both emit `AgentEventType.AUTH_FAILURE` for permanent failures only. `SessionRunner` handles AUTH_FAILURE by setting `suspended:auth-failure` status, committing WIP, and notifying the user. The watcher's `SessionMonitor.check_auth_failures()` retries these sessions on a slow interval (`AUTH_RETRY_INTERVAL_S`, 5 min) up to `MAX_AUTH_RETRIES` (6 attempts). After the limit, the session transitions to `suspended:auth-failure-permanent` and requires manual `nightshift resume`.
- **Review/merge lifecycle**: Container exits after `@@DONE@@` with status `waiting:review` and `completed_at` timestamp set. Host user runs `cli.py accept <id>` to merge or `cli.py reject <id>` to discard. `cmd_accept` verifies the agent branch is not behind the base branch before merging — if it is, the accept is rejected with a message to `nightshift resume` first. `cmd_revise` supports two modes: for review sessions (`waiting:review`, `waiting:human-review`) it collects tracker comments as feedback; for running sessions (`working`, `starting`) it stops the container, writes the operator's inline message as a mid-flight course-correction prompt, and relaunches with `--resume`. When a review session hits max-turns, `post_run.py` scans the conversation for a verdict (`scan_conversation_for_verdict` using `parse_nightshift_command`): if found, the review is treated as done (`waiting:review`); if not, it sets `suspended:review-no-verdict` and the watcher's `ReviewOrchestrator._handle_review_no_verdict` transitions the coder session to `waiting:human-review`. Background review launches are tracked via `HostWatcher._background_procs` (Popen handle + log file handle + timestamp). `check_background_launches()` polls these each watcher loop iteration; on non-zero exit, `_revert_failed_launch()` reverts the coder session from `reviewing` back to `waiting:review` for retry. Log file handles are closed after the grace period (`BACKGROUND_LAUNCH_CHECK_S`) or on process exit.
- **Orphan detection vs successful completion**: The orphan detector (`SessionMonitor.check_orphaned_sessions`) treats review sessions in `waiting:review` with no container as orphaned — except when `completed_at` is set in state.json. `notify_done()` sets `completed_at` via `StateManager.mark_completed()` before the container exits, so the orphan detector can distinguish a successful review exit from a container crash. Without this, a review that completes normally would be misclassified as an orphan and restarted up to `MAX_ORPHAN_RESUMES` times. The orphan detector also recovers coder sessions stuck in `reviewing` status: if the corresponding review container is not running (and not within the recently-launched grace period), `_try_recover_review_verdict()` first checks whether the review session completed with an unprocessed verdict (e.g. watcher restarted before processing the outbox) — if so, the outbox is processed and the verdict is applied (approve → `waiting:human-review`, revise → resumed). If no verdict is found, the coder session is reverted to `waiting:review` so auto-review can retry.
- **Zombie container detection**: `SessionMonitor.check_zombie_containers()` detects running containers that are stuck — no agent events emitted for `stall_timeout * 2` (double the configured stall timeout). Unlike orphans (no container running), zombies have a running container but produce no output, indicating the agent process is hung. When detected, a warning is logged and a Telegram alert is sent so the operator can investigate — the container is NOT automatically killed. This catches scenarios where the container is alive but the agent inside has deadlocked or is spinning without producing events.
- **Auto-start**: Watcher polls tracker for new issues matching a configured label (default `nightshift`), auto-launches `nightshift start` for each. Configured via `auto_start` section in WORKFLOW.md (`enabled`, `label`, `poll_interval_s`, `max_concurrent`). Tracks already-started issues via in-memory set + session dir check. Respects `max_concurrent` to avoid resource exhaustion.
- **Graceful shutdown**: `host/watcher/main.py` installs SIGTERM/SIGINT handlers that set a module-level `threading.Event`. This event is passed to `HostWatcher.run()` and propagated to the `GitBugTracker._shutdown` event. The main loop uses `event.wait(timeout=...)` instead of `time.sleep()` so it can exit immediately. `GitBugTracker._run_interruptible()` polls the subprocess with a short sleep loop, checking `_shutdown` each iteration — this allows signals to interrupt blocking git-bug calls. On loop exit, `terminate_current()` kills any in-flight tracker subprocess via the shared `_graceful_kill()` helper (terminate → wait → escalate to kill). `cmd_watcher` in `cli.py` uses `os.execvpe()` to replace the CLI process with the watcher process, so `kill <pid>` delivers signals directly — no orphan child.
- **Pre-review rebase**: When the agent outputs `@@DONE@@`, the container transitions to `waiting:review`. The host-side `review_orchestrator.maybe_launch_review()` then calls `host/rebase.py:attempt_pre_review_rebase()` **before** launching the review session. Rebase runs on the host (not in the container) to avoid bind-mount issues where git cannot unlink mounted files like WORKFLOW.md. Rebase uses `WorkspaceTransaction` so conflicts auto-abort cleanly and return a conflict prompt to the coder session. If the rebase succeeds, tests run (if `test_command` is configured) and the review session is launched. On test failure after a successful rebase, the coder session is resumed with a descriptive prompt.
- **Worktree integrity guardrail**: Host-side rebase and merge operations call `core.workspace_transaction.check_worktree_integrity()` before touching git so missing `.git/worktrees/<name>/` metadata is detected early and can be auto-repaired with `git worktree repair`.
- **Notification level filtering**: Each notifier supports a `level` setting (`questions`, `actions`, `all`) parsed from WORKFLOW.md. The `NotificationLevel` enum in `core/protocols.py` orders levels by increasing verbosity. The `should_notify(configured, message)` helper gates delivery — adapters check it in `notify()`. Every `notify()` call site tags its message with an explicit level. `send_question()` always bypasses the filter (it's the Q&A round-trip, not just a notification).
- **Merge-needed handoff**: When the host resumes a session (`workspace_setup.merge_base_into_worktree`), it fetches and merges the latest base branch into the agent worktree. If the merge conflicts, it aborts, writes `merge-needed.txt` to the session dir (fields: `merge_target`, `base_branch`, separator `---`, then conflict output). On container startup, `entrypoint._read_merge_instructions()` reads and deletes the file, prepending merge instructions to the agent prompt so the agent resolves conflicts, runs tests, and continues. The filename constant lives in `core/constants.MERGE_NEEDED_FILENAME`.
- **Hot-reload config (SIGHUP)**: `host/watcher/main.py` installs a SIGHUP handler that sets a module-level `reload_event`. The main loop in `HostWatcher.run()` checks this event each iteration and calls `reload_config()`. `reload_config()` re-parses the workflow file via `load_workflow()`, diffs old vs new config with `_diff_config()`, updates the TelegramRelay notification level (via `set_level()`), toggles auto-start, recreates the tracker, propagates the shutdown event to the new tracker, and logs which sections changed. On parse error, the previous config is kept intact and an error is logged.
- **Live issue sync**: Bidirectional file-based sync between host and container, run by `host/watcher/issue_sync.py` each watcher loop. **Reads (host→container):** `redump_issue()` in `host/issue_dump.py` atomically re-writes `issue.json` (including comments) for active sessions, throttled to once per `ISSUE_REDUMP_INTERVAL_S` (30s) per session to reduce git-bug lock contention. `StaticTracker.reload()` detects mtime changes and returns new comments. `SessionRunner._try_reload_tracker()` accumulates them and `_inject_new_comments()` prepends them to the next resume prompt. **Writes (container→host):** `StaticTracker._append_outbox()` appends JSON-lines entries to `tracker-outbox.jsonl`. The watcher's `process_outbox()` atomically renames the outbox to `.processing`, applies entries via the real tracker, then deletes the file. Atomic rename prevents losing entries the container appends concurrently. If `.processing` exists on the next cycle (crash recovery), it is processed first.
- **Template versioning**: Both WORKFLOW.md and REVIEW.md include a `template_version` field in their YAML front matter, using `TemplateVersion` (major.minor format, e.g., `1.0`, `2.1`; plain integers like `1` are treated as `1.0` for backward compatibility). Minor bumps (e.g., 1.0→1.1) are for additive changes; major bumps (e.g., 1.x→2.0) are for behavioral changes (replacements, consolidations). Canonical templates live in `templates/WORKFLOW.md` and `templates/REVIEW.md` — the single source of truth. `nightshift init` reads them via `core/upgrade.load_canonical_template()` and `load_canonical_review_template()`. `nightshift upgrade` compares each file's version against its canonical, shows a diff of the prompt section only (YAML config is never touched), and with `--apply` patches the prompt and bumps `template_version`. Major version bumps show a WARNING and require `--apply --force` to apply. When the template exceeds the soft cap (100 lines), a consolidation warning is shown. REVIEW.md is upgraded alongside WORKFLOW.md if it exists; if absent, it is silently skipped. `core/upgrade.py` contains all versioning logic: splitting front matter, diffing prompt sections, and applying upgrades.
- **Upstream proposals (REQ-027)**: `nightshift upstream` proposes local prompt improvements back to canonical templates — the reverse of `nightshift upgrade`. `core/upstream.py` provides reverse diffing (`diff_reverse`), operation type detection (add/replace/consolidate), and client-side validation filters: blocklist term scanning (`templates/blocklist.txt`), Jinja2 variable whitelist (`KNOWN_JINJA2_VARS`), and prompt line count caps (soft: 100, hard: 150). `cmd_upstream` in `host/cli.py` orchestrates: diff, validate, show to user, and file a git-bug issue via `tracker.run_raw()`. `--dry-run` previews without filing. `--project-name` sets provenance. `tests/test_template_lint.py` gates canonical template changes (no project-specific refs, no absolute paths, known Jinja2 vars, line count limits).
- **Single-writer tracker architecture (REQ-026)**: All git-bug operations are serialized through a single `TrackerWriter` thread in the watcher process, eliminating lock contention. The writer processes a `queue.Queue` of `TrackerRequest` objects one at a time. Internal watcher code uses `QueueTrackerProxy` (submits directly to the queue, no socket overhead). External CLI processes connect via `TrackerSocketServer` (Unix domain socket at `.nightshift/tracker.sock`, JSON-lines protocol). CLI and launch scripts use `get_tracker_with_fallback()` from `host/tracker_client.py`: probes the socket, returns `SocketTrackerClient` when the watcher is running, falls back to direct `GitBugTracker` with lock retry (REQ-025) when it's not. Both `SocketTrackerClient` and `QueueTrackerProxy` extend `TrackerIPCBase` from `core/tracker_ipc.py` which provides all IssueTracker method implementations — subclasses only override `_call()`. Constants: `TRACKER_SOCKET_FILENAME` (in `host/constants`), `TRACKER_IPC_TIMEOUT_S` (in `core/constants`), `TRACKER_WRITER_QUEUE_SIZE`, `TRACKER_SOCKET_MAX_WORKERS` (in `host/constants`).
- **Lifecycle comments**: `host/watcher/lifecycle_comments.py` posts brief summary comments to the issue tracker at key session transitions (start, resume, question, done, revise). Only the watcher posts — the container's StaticTracker stays read-only. Each calling module tracks which events have been posted via `_posted_*` sets to avoid duplicates. Comments are posted via `_safe_post()` which logs failures without raising — `_safe_post()` does NOT call `tracker.sync()` (the watcher syncs periodically via `_maybe_sync_tracker()`). Integrated into `qa_handler.py` (question), `review_orchestrator.py` (done), `session_monitor.py` (start, resume), and `verdict_handler.py` (revise).
- **Litellm proxy (overflow)**: When `overflow.litellm_config` is set in WORKFLOW.md, the container runs a litellm proxy on localhost:4000 for model name remapping. `docker-entrypoint.sh` starts `litellm --config /session/litellm-config.yaml --port 4000` before the agent and waits for a health check. `host/docker_cmd.py` mounts the config file read-only at `LITELLM_CONFIG_CONTAINER_PATH` and sets `ANTHROPIC_BASE_URL=http://localhost:4000`. Claude Code sends standard Anthropic model names; litellm rewrites them and routes to the configured provider. Config example: `overflow: { litellm_config: litellm-config.yaml, env: { OVERFLOW_API_KEY: $OVERFLOW_API_KEY } }`. Overflow is skipped for review sessions (`step=review`) so the review agent always uses its own provider. Constants: `LITELLM_PROXY_PORT`, `LITELLM_CONFIG_CONTAINER_PATH`, `LITELLM_HEALTH_TIMEOUT_S` (in `core/constants`).
- **Dual-agent workflow (REQ-031)**: WORKFLOW.md and REVIEW.md each have independent `agent` sections in their YAML front matter. `agent.kind`, `max_turns`, `stall_timeout_s`, and `extra_args` are configured per-step. Example: `agent.kind: openhands` in WORKFLOW.md (cheap coder) with `agent.kind: claude-code` in REVIEW.md (expensive reviewer). The review orchestrator loads REVIEW.md via `load_workflow()` and passes it to `launch.py --workflow REVIEW.md --step review`, so the review container uses its own agent config. `nightshift export-training-data` extracts (prompt, agent_output, review_feedback) tuples from paired coder+review sessions as JSONL for finetuning. `core/training_export.py` contains the extraction logic; it pairs each coder session with its `review-<sid>` counterpart, extracts the initial prompt, agent output, review verdict, and reviewer feedback.
- **Session archival (REQ-034)**: On `accept`, `reject`, and `cleanup`, session files are archived to `.nightshift/archive/<session-id>/` before the session directory is deleted. `archive_session()` in `host/session_utils.py` copies the files listed in `ARCHIVE_FILES` (`conversation.jsonl`, `state.json`, `raw-output.log`) to the archive directory. The `ARCHIVE_DIR` constant (`"archive"`) is defined in `host/constants.py`. This preserves provenance data (conversation history, final state, raw agent output) for post-hoc analysis after worktree and session cleanup.
- **Usage tracking (REQ-032)**: Token usage and cost are tracked per session and persisted to `.nightshift/usage.jsonl` (outside session dirs, survives cleanup). `ClaudeCodeAgent._parse()` extracts `cost_usd`, `input_tokens`, `output_tokens` from `result` events into `AgentEvent.metadata["usage"]`. `SessionRunner._maybe_record_usage()` accumulates these via `StateManager.update_usage()` (additive across resumes). On session completion, `host/launch.py:_post_container()` appends a JSON-line entry to `usage.jsonl` and includes a cost summary in the proof-of-work comment (via `core/post_run.format_cost_line()`). `nightshift usage [issue-id]` queries and aggregates the log. Constants: `USAGE_LOG_FILENAME` (in `host/constants`).
- **SSM as single source of truth (REQ-035)**: All session status changes must go through `SessionStateMachine` validation. Direct writes like `st.status = "foo"` or `state["status"] = "foo"` are forbidden unless they use `ssm.state` (the validated result). Container-side: `StateManager.update_status()` and `mark_done()` validate transitions. Host-side: `update_status()` and `update_state_fields()` in `host/session_utils.py` validate transitions. `tests/test_codebase_audit.py::test_no_direct_status_writes` enforces this invariant by scanning for direct status writes that bypass SSM. **Exception:** `nightshift force-status` uses `force_update_status()` which validates that the target status is known but bypasses transition validation — intended for manual recovery when sessions are stuck in invalid states.

## Testing

**Target: 80% line coverage.** Check with: `.venv/bin/python -m coverage run -m pytest tests/ && .venv/bin/python -m coverage report`

Tests use mock implementations from `tests/conftest.py` (`MockAgent`, `MockTracker`, `MockNotifier`, `MockWorkspaceManager`). ~1500 tests across `tests/`, `tests/watcher/`, and `tests/watchdog/`.

Key test files: `test_upstream.py`, `test_template_lint.py`, `test_upgrade.py`, `test_stream_parser.py`, `test_marker_reliability.py`, `oq1_stdin_test.py`, `test_static_tracker.py`, `test_dotenv.py`, `test_cli_env.py`, `test_cli_commands.py`, `test_cli_helpers.py`, `test_cli_issue.py`, `test_accept_reject.py`, `test_worktree_git_fix.py`, `test_review_step.py`, `test_review.py`, `test_auto_start.py`, `test_session_runner.py`, `test_hooks.py`, `test_post_run.py`, `test_qa_flow.py`, `test_prompts.py`, `test_config_factories.py`, `test_config_discovery.py`, `test_docker_utils.py`, `test_git_utils.py`, `test_composite_notifier.py`, `test_notifier_prefix.py`, `test_notification_level.py`, `test_rebase.py`, `test_host_rebase.py`, `test_search.py`, `test_session_utils_host.py`, `test_launch.py`, `test_post_container.py`, `test_assistant_text_logging.py`, `test_workspace_setup.py`, `test_entrypoint_merge.py`, `test_entrypoint_codex_config.py`, `test_issue_redump.py`, `watcher/test_qa_handler.py`, `watcher/test_host_watcher.py`, `watcher/test_review_orchestrator.py`, `watcher/test_telegram_relay.py`, `watcher/test_session_monitor.py`, `watcher/test_graceful_shutdown.py`, `watcher/test_lifecycle_comments.py`, `watcher/test_issue_sync.py`, `test_training_export.py`, `watchdog/test_scanner.py`, `watchdog/test_log_monitor.py`, `watchdog/test_alerter.py`, `watchdog/test_main.py`, `watchdog/test_session_checker.py`.

Remaining coverage gaps:
- `adapters/trackers/git_bug.py` — git-bug CLI interaction (hard to test without git-bug binary)
- `adapters/notifiers/telegram.py` — Telegram API calls (requires mocking HTTP)

## Docker

The container runs with `--user $(id -u):$(id -g)` matching the host UID so it can read `600`-permission credential files. Auth credentials are mounted read-only at `/claude-auth` and copied to a writable HOME by `docker-entrypoint.sh`.

Key Docker run pattern (no `-it` flag — containers are fire-and-forget):
```bash
docker run --rm --user $(id -u):$(id -g) \
  -v <worktree>:/workspace:rw \
  -v <session-dir>/git-merged:/repo-git:rw  # overlay mount
  -v <session-dir>:/session:rw \
  -v ~/.claude:/claude-auth:ro \
  -v <repo>/WORKFLOW.md:/workspace/WORKFLOW.md:ro \
  -e ISSUE_ID=<id> -e SHORT_ID=<short-id> \
  nightshift:latest
```

`host/launch.py` creates a session-local git overlay under `.nightshift/sessions/<session-id>/`. When `fuse-overlayfs` is available, it mounts `repo/.git` as the lower layer and uses `git-merged` as the container mount source, with `git-upper` and `git-work` holding writes. If overlay support is unavailable, it falls back to copying `.git` into `git-copy`. The container always mounts the session-local path at `/repo-git`, so it reads and mutates isolated git state without touching the repo's live `.git` directory. After the container exits, the launcher tears down the mount and copies any new objects and refs back to the real repo. Container naming: coder containers are `nightshift-<short-id>`, review containers are `nightshift-review-<short-id>`. The watcher detects review sessions by the `review-` prefix in session IDs and passes `--step review --workflow REVIEW.md` when auto-resuming them. When `agent.kind` is `openhands`, the container runs OpenHands instead of Claude Code; `docker_cmd.py` injects the `LLM_*` env vars and any agent-specific extra_args.

`docker-entrypoint.sh` sets `GIT_DIR` and `GIT_WORK_TREE` environment variables to point to the mounted `/repo-git/worktrees/agent-<short-id>` and `/workspace`, so git operations inside the container use the session-local overlay/copy rather than the repo's live `.git` directory. It also generates `~/.codex/config.toml` for Codex agent sessions: when overflow is active (`CODEX_BASE_URL` or `CODEX_MODEL` set), config.toml is generated regardless of OAuth presence — `CODEX_BASE_URL` produces a custom provider config, while `CODEX_MODEL` alone produces an openai provider config. OAuth and model config are independent: OAuth presence normally skips API key export (not config generation), but overflow profiles can opt out with `skip_oauth: true`. Without overflow, the fallback chain `CODEX_API_KEY` → `OPENAI_API_KEY` provides the key. `AGENT_KIND` is passed from `host/docker_cmd.py` based on the workflow's `agent.kind` setting.

**Codex OAuth exclusion**: `docker_cmd.py`'s `_codex_oauth_present()` checks for `~/.codex/auth.json` with valid tokens. When OAuth is detected for Codex agent sessions, `CODEX_API_KEY` and `OPENAI_API_KEY` are excluded from environment variable passthrough to the container unless the active overflow profile sets `skip_oauth: true` — this prevents API keys from overriding OAuth authentication by default.

When launching the watcher from a different repo (e.g. `nightshift watcher` from `jessica-ng/`), `cli.py` injects `PYTHONPATH` pointing to the agent-worker root so `python -m host.watcher` resolves correctly.

## git-bug

The adapter targets git-bug v0.10.1. Commands are under the `bug` subcommand: `git-bug bug show`, `git-bug bug comment new`, `git-bug bug label new/rm`, `git-bug bug status close/open`. The top-level `git-bug pull`/`push` are used for sync.

## Dependencies

Python 3.12+. Runtime: `requests`, `pyyaml`, `jinja2`. The Docker image also installs `git-bug` v0.10.1 and `@anthropic-ai/claude-code` (npm).
