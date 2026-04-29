# Business Requirements — Nightshift

Executive summary of what nightshift must do, traced to tests.

---

### REQ-001: Launch agent in isolated workspace
The system creates a git worktree per issue and runs the coding agent inside a Docker container against that worktree. Git operations work correctly inside the container.

- **Tests:** test_worktree_git_fix.py, test_cli_helpers.py, test_git_utils.py, test_launch.py, test_session_utils_host.py
- **Status:** covered

### REQ-002: Session lifecycle management
The system tracks session state (working, suspended, waiting, done) with atomic JSON persistence and exposes status via CLI. Auth failures are detected and set a distinct `suspended:auth-failure` status instead of burning through resume attempts. Successfully completed review sessions set a `completed_at` timestamp to prevent misclassification as orphans by the watcher.

- **Tests:** test_marker_reliability.py, test_cli_commands.py, test_session_runner.py (TestAuthFailure), test_session_utils_host.py, watcher/test_graceful_shutdown.py, watcher/test_lifecycle_comments.py, watcher/test_session_monitor.py (TestCheckAuthFailures, TestCheckOrphanedSessions: test_review_session_completed_at_not_orphan), test_post_run.py (TestNotifyDone: test_sets_completed_at)
- **Status:** covered

### REQ-003: Human Q&A
The agent can ask blocking questions to a human. The system pauses the container, collects answers via Telegram or CLI, and resumes the agent with the answer.

- **Tests:** test_marker_reliability.py, oq1_stdin_test.py, test_session_runner.py
- **Status:** covered

### REQ-004: Checkpoint and resume on context limits
When the agent hits context limits, max turns, or stalls, the system saves progress, builds a resume prompt with checkpoint history, and auto-restarts the agent. Auth failures are excluded from auto-resume to avoid burning through MAX_RESUMES with a bad token; the watcher retries on a slow interval instead.

- **Tests:** test_marker_reliability.py, test_prompts.py, test_session_runner.py (TestAuthFailure), test_post_run.py (test_auth_failure_returns_none), test_stream_parser.py (TestAuthFailureDetection)
- **Status:** covered

### REQ-005: Review gate before merge
Agent work is not merged automatically. On completion, the system posts a proof-of-work summary and waits for human review before merging.

- **Tests:** test_marker_reliability.py, test_review.py, test_review_step.py, test_post_container.py, test_cli_helpers.py, test_session_runner.py
- **Status:** covered

### REQ-006: Merge and conflict handling
A human can accept agent work via CLI, which merges the agent branch into the base branch, detects unresolved conflict markers, handles conflicts, cleans up worktrees, and closes the issue. Before submitting for review, the agent automatically rebases onto the latest base branch and re-runs tests; if conflicts or test failures occur, the agent is resumed to fix them.

- **Tests:** test_accept_reject.py, test_cli_helpers.py, test_git_utils.py, test_rebase.py, test_post_run.py (TestPostRunRebase)
- **Status:** covered

### REQ-007: Reject agent work
A human can reject agent work via CLI, which discards the worktree, branch, and session, and closes the issue.

- **Tests:** test_accept_reject.py, test_session_utils_host.py
- **Status:** covered

### REQ-008: Revise agent work after review or mid-flight
A human (or automated reviewer) can request revisions. For sessions in review (`waiting:review`, `waiting:human-review`), the system collects review feedback from tracker comments and restarts the agent with a revise prompt. For running sessions (`working`, `starting`), the system stops the container, writes the operator's inline message as a course-correction prompt, and relaunches with `--resume`.

- **Tests:** test_review.py, test_review_step.py, test_assistant_text_logging.py, test_cli_commands.py (mid-flight revise tests)
- **Status:** covered

### REQ-009: Automated code review
When a REVIEW.md template exists, the system automatically launches a reviewer agent against completed work. The reviewer can approve or request revisions, with configurable max review rounds. When a review session hits max-turns, the system checks if a verdict was already emitted: if yes, it treats the review as successfully completed; if no, it falls back to human review on the coder session. Background launch failures are detected by polling Popen exit codes; on failure, the coder session is reverted from "reviewing" to "waiting:review" for retry. The orphan detector also recovers coder sessions stuck in "reviewing" with no review container.

- **Tests:** test_review_step.py, test_assistant_text_logging.py, test_post_run.py (TestReviewMaxTurns, TestScanConversationForVerdict), watcher/test_review_orchestrator.py (TestReviewNoVerdict, TestNoArchiveWithoutVerdictProcessing), watcher/test_host_watcher.py (TestLaunchBackground, TestCheckBackgroundLaunches), watcher/test_session_monitor.py (TestReviewingStatusRecovery, TestCleanupCompletedReviewSessionBlocking)
- **Status:** covered

### REQ-010: Notifications via pluggable channels
The system sends notifications (status updates, questions, completion) through configurable channels (Telegram, webhook, Slack). Notifications include a project name prefix so multi-project setups are distinguishable. Each notifier supports a `level` setting (`questions`, `actions`, `all`) to filter notification verbosity — `questions` delivers only human-input-needed alerts, `actions` adds done/error/escalation, `all` (default) sends everything.

- **Tests:** test_notifier_prefix.py, test_composite_notifier.py, test_notification_level.py
- **Status:** covered

### REQ-011: Configuration via workflow file with per-repo discovery
All adapter selection, runtime settings, hooks, and prompt templates are configured through a YAML-front-matter workflow file. Environment variables are resolved with `$VAR` syntax. Overflow profiles can also be loaded from `.nightshift/profiles.yaml`, with workflow-local `overflow_profiles` entries acting as overrides. Workflow file discovery follows a priority order: CLI `--workflow` flag > `.nightshift.yaml` pointer in repo root > `WORKFLOW.md` in repo root. `nightshift init --workflow-path` scaffolds workflow files at custom locations and writes the `.nightshift.yaml` pointer.

- **Tests:** test_auto_start.py (config parsing), test_review_step.py (config parsing), test_config_factories.py, test_config_loader.py, test_entrypoint_overflow.py, test_prompts.py, test_search.py, test_config_discovery.py (discovery order, pointer read/write, init --workflow-path, error messages)
- **Status:** covered

### REQ-012: CLI for all operations
Users manage nightshift through a CLI: init, start, resume, answer, status, logs, history, accept, reject, revise, cleanup, watcher.

- **Tests:** test_accept_reject.py, test_cli_env.py, test_cli_commands.py, test_cli_helpers.py, test_session_utils_host.py
- **Status:** covered

### REQ-013: Environment variable loading
The system loads `.env` files before parsing WORKFLOW.md so that `$VAR` references resolve correctly.

- **Tests:** test_dotenv.py, test_cli_env.py
- **Status:** covered

### REQ-014: Stream parsing of agent output
The system parses the agent's stream-json output into typed events (text, tool calls, tool results, system messages) for processing by the session runner.

- **Tests:** test_stream_parser.py
- **Status:** covered

### REQ-015: Marker protocol for agent communication
The agent communicates progress via text markers (@@LOG@@, @@CHECKPOINT@@, @@QUESTION@@, @@WAITING@@, @@DONE@@). Markers in non-text events (tool results) are ignored to prevent injection.

- **Tests:** test_marker_reliability.py, test_session_runner.py
- **Status:** covered

### REQ-016: Static issue data inside containers
Inside the container, issue data is read from pre-dumped JSON files (no network access to the tracker). Write operations are appended to an outbox file for host processing.

- **Tests:** test_static_tracker.py
- **Status:** covered

### REQ-016b: Live issue sync between host and container
The host watcher re-dumps issue.json (including comments) to session dirs for active sessions. Re-dumps are throttled to once per `ISSUE_REDUMP_INTERVAL_S` (30s) per session to reduce git-bug lock contention (each redump acquires the lock twice: get_issue + get_comments). The container's StaticTracker detects mtime changes via reload() and returns new comments. The SessionRunner injects new comments into resume prompts. Container write operations (comments, labels, status changes) are appended to tracker-outbox.jsonl, which the host watcher processes via the real tracker. Lifecycle comments do not trigger immediate tracker sync — the watcher syncs periodically. The sync is file-based with polling (~30s acceptable delay), preserving container sandboxing.

- **Tests:** test_static_tracker.py (reload, outbox), test_issue_redump.py, watcher/test_issue_sync.py (including throttle tests), test_session_runner.py (TestTrackerReload)
- **Status:** covered

### REQ-017: Auto-start new issues
The host watcher can poll the tracker for new issues matching a configured label and auto-launch agent sessions, respecting a max concurrency limit.

- **Tests:** test_auto_start.py, test_watcher.py
- **Status:** covered

### REQ-018: Proof-of-work summary on completion
When the agent finishes, the system posts a summary of checkpoints, Q&A exchanges, and a diff stat to the issue tracker.

- **Tests:** test_post_container.py, test_launch.py
- **Status:** covered

### REQ-019: Host watcher pause/unpause
The host watcher monitors session directories, pauses idle containers (zero CPU), and unpauses them when answers arrive.

- **Tests:** test_docker_utils.py, test_watcher.py
- **Status:** covered

### REQ-020: External cancellation
If the tracked issue is closed externally while the agent is running, the session is cancelled gracefully.

- **Tests:** test_watcher.py
- **Status:** partial

### REQ-021: Workspace hooks
Users can configure shell hooks (after_create, before_run, after_run) that run at lifecycle points. A fatal hook failure stops the session.

- **Tests:** test_session_runner.py
- **Status:** partial

### REQ-022: Pluggable adapters
The system supports swappable adapters for agents, trackers, workspaces, and notifiers via a registry + dynamic import pattern. New adapters require only a registry entry and protocol implementation.

- **Tests:** test_config_factories.py
- **Status:** covered

### REQ-023: Hot-reload config on SIGHUP
On receiving SIGHUP, the host watcher re-reads and re-parses the workflow file, updates in-memory config (notification levels, auto_start, merge policy, review config), recreates adapters that depend on config (tracker, notifiers), and logs what changed. A parse error keeps the previous config intact.

- **Tests:** watcher/test_config_reload.py
- **Status:** covered

### REQ-024: Template versioning and upgrade
Both WORKFLOW.md and REVIEW.md include a `template_version` field in their YAML front matter. Canonical templates ship in `templates/WORKFLOW.md` and `templates/REVIEW.md`. `nightshift upgrade` compares versions for both files, shows a diff of the prompt section, and with `--apply` patches the prompt while preserving the user's YAML config. REVIEW.md is upgraded alongside WORKFLOW.md if it exists; if absent, it is silently skipped. `nightshift init` scaffolds both from canonical templates and warns when existing files are behind.

- **Tests:** test_upgrade.py
- **Status:** covered

### REQ-025: git-bug lock retry and stale lock detection
All git-bug CLI operations route through `GitBugTracker._run()` which retries on lock errors with exponential backoff (`LOCK_RETRY_ATTEMPTS` attempts, base delay `LOCK_RETRY_BASE_DELAY_S` doubling each retry). If the locking PID is dead (stale lock), the lock files are cleared and the command retries immediately without backoff. Live PID locks are never forcibly removed. Shutdown events interrupt retry waits.

- **Tests:** test_git_bug_lock_retry.py
- **Status:** covered

### REQ-026: Single-writer git-bug access
All git-bug operations are serialized through a single writer thread in the watcher process, eliminating lock contention. The writer thread processes a queue of tracker operations one at a time. A Unix domain socket server accepts JSON-lines requests from CLI processes and routes them through the same queue. The watcher's own internal tracker calls use a queue proxy (no socket overhead). CLI and launch scripts connect via `get_tracker_with_fallback()`: when the watcher socket is available, a `SocketTrackerClient` is used; when unavailable, falls back to direct `GitBugTracker` with the existing lock retry mechanism (REQ-025).
The git-bug GraphQL watcher also includes stale-cache recovery for `.git/git-bug/cache/bugs`: on `list_issues()` errors containing "bug doesn't exist" it clears the cache, restarts `git-bug webui`, retries once, and periodically health-checks refs vs cached issue count to rebuild mismatches. SIGHUP-triggered config reloads clear the cache before recreating the tracker.

- **Tests:** test_tracker_ipc.py, test_socket_tracker_client.py, test_tracker_fallback.py, watcher/test_tracker_writer.py, test_gitbug_graphql.py, watcher/test_host_watcher.py, watcher/test_config_reload.py
- **Status:** covered

### REQ-027: Upstream template proposals
Downstream projects can propose prompt improvements back to the canonical templates via `nightshift upstream`. The command diffs local prompt sections against canonical (reverse of `nightshift upgrade`), runs client-side validation filters (blocklist terms, Jinja2 variable whitelist, line count caps), shows the diff for confirmation, and files a git-bug issue in the upstream repo. Each proposal declares an operation type (add, replace, consolidate). A template lint suite gates canonical template changes. Extends REQ-024.

- **Tests:** test_upstream.py, test_template_lint.py
- **Status:** covered

### REQ-028: Manual overflow to alternate LLM provider
Users can manually switch new container launches to an alternate Anthropic-compatible LLM provider via `nightshift overflow on/off`. The overflow config (extra_args, env vars, litellm_config) is defined in WORKFLOW.md's `overflow` section with `$VAR` references resolved from `.env`. A flag file (`.nightshift/overflow`) controls activation. When active, overflow env vars and extra_args are injected into new docker commands. Already-running containers are unaffected. `nightshift status` shows overflow state. No watcher restart needed. When `litellm_config` is set, the container starts a litellm proxy on localhost:4000 for model name remapping — `ANTHROPIC_BASE_URL` is automatically set to point to the proxy, and the config file is mounted read-only into the container.

- **Tests:** test_overflow.py
- **Status:** covered

### REQ-030: OpenHands agent adapter
The system supports OpenHands as an alternative coding agent. The adapter translates OpenHands JSON events (separated by "--JSON Event--" lines) into nightshift markers: FileEditorAction→@@CHECKPOINT@@, FinishAction→@@DONE@@, reasoning_content→@@LOG@@, TerminalAction→tool_call, ObservationEvent→tool_result. Session IDs are extracted from stderr for resume support.

- **Tests:** test_openhands_agent.py, test_config_factories.py
- **Status:** covered

### REQ-031: Dual-agent workflow with finetuning loop
The system supports using different agent kinds for development (coder) and review steps. WORKFLOW.md `agent.kind` controls the coder agent; REVIEW.md `agent.kind` controls the reviewer independently. Per-step `max_turns` and `stall_timeout_s` are configured in each file's YAML front matter. Review verdicts (approve/revise) and session conversation logs are exportable as structured training data via `nightshift export-training-data`, producing JSONL files with (prompt, agent_output, review_feedback) tuples suitable for finetuning cheaper agents.

- **Tests:** test_training_export.py
- **Status:** covered

### REQ-033: Codex agent adapter
The system supports OpenAI Codex CLI as a coding agent via `agent.kind: codex`. The adapter inherits `HeadlessAgentBase` and runs `codex exec --json --full-auto` in fire-and-forget mode. JSONL events are parsed by `type` field: `thread.started` (session ID extraction), `item.completed` (agent messages and command executions), `turn.completed` (@@DONE@@ + token usage), `error`/`turn.failed` (auth failure detection). Resume uses `codex exec resume <thread_id> "prompt"`. Configured via `~/.codex/config.toml` with custom model providers (e.g., OpenRouter).

- **Tests:** test_codex_agent.py
- **Status:** covered

### REQ-032: Token usage tracking
Token usage and cost are tracked per session and persisted to `.nightshift/usage.jsonl` (outside session dirs, survives cleanup). `ClaudeCodeAgent._parse()` extracts `cost_usd`, `input_tokens`, `output_tokens` from `result` events. `SessionRunner` accumulates usage via `StateManager.update_usage()` (additive across resumes). On session completion, `host/launch.py:_post_container()` appends a JSON-line entry to `usage.jsonl` and includes a cost summary in the proof-of-work comment. `nightshift usage [issue-id]` queries and aggregates the log.

- **Tests:** test_session_runner.py (TestUsageTracking), test_post_run.py (TestUsageInNotifyDone), test_launch.py (test_post_container_includes_cost_in_comment, test_usage_appended_to_jsonl_on_done, test_usage_jsonl_survives_cleanup), test_stream_parser.py (test_result_event_extracts_usage, test_result_event_no_usage_when_absent), test_cli_commands.py (test_cmd_usage_*)
- **Status:** covered

### REQ-033: Codex CLI Docker support
The Docker image includes the OpenAI Codex CLI (`@openai/codex`) so `agent.kind: codex` works inside containers. Codex has independent provider configuration via `CODEX_API_KEY` (fallback to `OPENAI_API_KEY`), `CODEX_BASE_URL`, and `CODEX_MODEL`. If `CODEX_BASE_URL` is set, the entrypoint generates `~/.codex/config.toml` with a custom provider; if not, it exports `OPENAI_API_KEY` so Codex uses OpenAI natively. All three `CODEX_*` vars plus `OPENAI_API_KEY` are in `_PASSTHROUGH_ENV_VARS`.

- **Tests:** test_launch.py (TestCodexDockerSupport), test_overflow.py (test_codex_env_passthrough), test_entrypoint_codex_config.py, test_codex_agent.py (TestCodexAgentStart, TestCodexAgentParse, TestInDocker)
- **Status:** covered

### REQ-034: Session history preservation
Session files (conversation.jsonl, state.json, raw-output.log) are archived to `.nightshift/archive/<session-id>/` before the session directory is deleted during accept, reject, or cleanup. This preserves provenance data for post-hoc analysis. The `archive_session()` function in `host/session_utils.py` copies only the files listed in `ARCHIVE_FILES`. Missing files are skipped gracefully. Archiving is idempotent (re-archiving overwrites).

- **Tests:** test_session_utils_host.py (TestArchiveSession: test_cleanup_archives_conversation, test_cleanup_archives_state, test_accept_archives_before_cleanup, test_archives_raw_output_log, test_returns_none_for_missing_session, test_skips_missing_files_gracefully, test_archive_path_uses_session_id, test_does_not_archive_non_listed_files, test_idempotent_overwrites_existing_archive)
- **Status:** covered

### REQ-036: Host-container interface hardening
The host-container interface is hardened against compromised or misbehaving agents. Guardrails include: (1) Git object validation via `git fsck` before copy-back to host repo, with ref whitelist to only allow `agent-*` branches; (2) Schema validation for tracker outbox entries (valid ops, hex issue_id format); (3) Schema validation for state.json on host reads (type checking, bounds validation with graceful degradation); (4) Session directory size monitoring with alerts; (5) Symlink audit before accept to detect escapes outside worktree.

- **Tests:** test_launch.py (test_copy_git_changes_*), watcher/test_issue_sync.py (test_process_outbox_validates_*), test_session_utils_host.py (test_read_state_validates_*), watcher/test_session_monitor.py (test_check_session_size_*), test_accept_reject.py (test_accept_rejects_external_symlinks), test_git_utils.py (test_audit_symlinks_*)
- **Status:** planned
- **Reference:** docs/host-container-interface.md

---

## Traceability Matrix

| Test File | Requirements |
|---|---|
| test_accept_reject.py | REQ-006, REQ-007, REQ-012, REQ-036 |
| test_assistant_text_logging.py | REQ-008, REQ-009 |
| test_auto_start.py | REQ-011, REQ-017 |
| test_cli_commands.py | REQ-002, REQ-012, REQ-032 |
| test_cli_env.py | REQ-012, REQ-013 |
| test_cli_helpers.py | REQ-001, REQ-005, REQ-006, REQ-012 |
| test_composite_notifier.py | REQ-010 |
| test_codex_agent.py | REQ-033 |
| test_config_loader.py | REQ-011 |
| test_config_factories.py | REQ-011, REQ-022, REQ-030 |
| test_docker_utils.py | REQ-019 |
| test_dotenv.py | REQ-013 |
| test_git_bug_lock_retry.py | REQ-025 |
| test_git_utils.py | REQ-001, REQ-006, REQ-036 |
| test_launch.py | REQ-001, REQ-018, REQ-032, REQ-033, REQ-036 |
| test_marker_reliability.py | REQ-002, REQ-003, REQ-004, REQ-005, REQ-015 |
| test_notification_level.py | REQ-010 |
| test_notifier_prefix.py | REQ-010 |
| test_post_container.py | REQ-005, REQ-018 |
| test_post_run.py | REQ-004, REQ-005, REQ-006, REQ-032 |
| test_prompts.py | REQ-004, REQ-011 |
| test_rebase.py | REQ-006 |
| test_review.py | REQ-005, REQ-008 |
| test_review_step.py | REQ-005, REQ-008, REQ-009, REQ-011 |
| test_search.py | REQ-011 |
| test_session_runner.py | REQ-002, REQ-003, REQ-004, REQ-015, REQ-016b, REQ-021, REQ-032 |
| test_session_utils_host.py | REQ-001, REQ-002, REQ-007, REQ-012, REQ-034, REQ-036 |
| test_static_tracker.py | REQ-016, REQ-016b |
| test_issue_redump.py | REQ-016b |
| watcher/test_issue_sync.py | REQ-016b, REQ-036 |
| test_stream_parser.py | REQ-004, REQ-014, REQ-032 |
| test_codex_agent.py | REQ-033 |
| test_overflow.py | REQ-028, REQ-033 |
| test_upstream.py | REQ-027 |
| test_template_lint.py | REQ-027 |
| test_upgrade.py | REQ-024 |
| test_watcher.py | REQ-017, REQ-019, REQ-020 |
| test_worktree_git_fix.py | REQ-001 |
| oq1_stdin_test.py | REQ-003 |
| watcher/test_graceful_shutdown.py | REQ-002 |
| watcher/test_lifecycle_comments.py | REQ-002, REQ-008 |
| watcher/test_config_reload.py | REQ-023 |
| watcher/test_session_monitor.py | REQ-002, REQ-004, REQ-009, REQ-011, REQ-017, REQ-036 |
| watcher/test_host_watcher.py | REQ-009 |
| test_gitbug_graphql.py | REQ-026 |
| test_tracker_ipc.py | REQ-026 |
| test_socket_tracker_client.py | REQ-026 |
| test_tracker_fallback.py | REQ-026 |
| watcher/test_config_reload.py | REQ-026 |
| watcher/test_host_watcher.py | REQ-026 |
| watcher/test_tracker_writer.py | REQ-026 |
| test_openhands_agent.py | REQ-030 |
| test_overflow.py | REQ-028 |
| test_training_export.py | REQ-031 |

---

## Orphan Tests

No orphan tests — all test files map to at least one requirement.

**Note:** `oq1_stdin_test.py` maps to REQ-003 but requires manual execution with a real Claude Code installation. It is not included in automated CI runs. Consider keeping it as a manual verification script.

---

## Partial Coverage

Requirements with some test coverage but significant gaps remaining.

| Requirement | Gap |
|---|---|
| REQ-020: External cancellation | Watcher tests cover some cancellation paths, but no tests for graceful agent termination on external issue close. |
| REQ-021: Workspace hooks | Session runner tests cover hook execution, but no tests for hook timeout or all lifecycle hook points. |

---

## Untested Requirements

All requirements now have at least partial test coverage.
