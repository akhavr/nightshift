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

- **Tests:** test_review_step.py, test_assistant_text_logging.py, test_post_run.py (TestReviewMaxTurns, TestScanConversationForVerdict), watcher/test_review_orchestrator.py (TestReviewNoVerdict), watcher/test_host_watcher.py (TestLaunchBackground, TestCheckBackgroundLaunches), watcher/test_session_monitor.py (TestReviewingStatusRecovery)
- **Status:** covered

### REQ-010: Notifications via pluggable channels
The system sends notifications (status updates, questions, completion) through configurable channels (Telegram, webhook, Slack). Notifications include a project name prefix so multi-project setups are distinguishable. Each notifier supports a `level` setting (`questions`, `actions`, `all`) to filter notification verbosity — `questions` delivers only human-input-needed alerts, `actions` adds done/error/escalation, `all` (default) sends everything.

- **Tests:** test_notifier_prefix.py, test_composite_notifier.py, test_notification_level.py
- **Status:** covered

### REQ-011: Configuration via workflow file with per-repo discovery
All adapter selection, runtime settings, hooks, and prompt templates are configured through a YAML-front-matter workflow file. Environment variables are resolved with `$VAR` syntax. Workflow file discovery follows a priority order: CLI `--workflow` flag > `.nightshift.yaml` pointer in repo root > `WORKFLOW.md` in repo root. `nightshift init --workflow-path` scaffolds workflow files at custom locations and writes the `.nightshift.yaml` pointer.

- **Tests:** test_auto_start.py (config parsing), test_review_step.py (config parsing), test_config_factories.py, test_prompts.py, test_search.py, test_config_discovery.py (discovery order, pointer read/write, init --workflow-path, error messages)
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

- **Tests:** test_tracker_ipc.py, test_socket_tracker_client.py, test_tracker_fallback.py, watcher/test_tracker_writer.py
- **Status:** covered

### REQ-027: Upstream template proposals
Downstream projects can propose prompt improvements back to the canonical templates via `nightshift upstream`. The command diffs local prompt sections against canonical (reverse of `nightshift upgrade`), runs client-side validation filters (blocklist terms, Jinja2 variable whitelist, line count caps), shows the diff for confirmation, and files a git-bug issue in the upstream repo. Each proposal declares an operation type (add, replace, consolidate). A template lint suite gates canonical template changes. Extends REQ-024.

- **Tests:** test_upstream.py, test_template_lint.py
- **Status:** covered

### REQ-028: Manual overflow to alternate LLM provider
Users can manually switch new container launches to an alternate Anthropic-compatible LLM provider via `nightshift overflow on/off`. The overflow config (extra_args, env vars) is defined in WORKFLOW.md's `overflow` section with `$VAR` references resolved from `.env`. A flag file (`.nightshift/overflow`) controls activation. When active, overflow env vars and extra_args are injected into new docker commands. Already-running containers are unaffected. `nightshift status` shows overflow state. No watcher restart needed.

- **Tests:** test_overflow.py
- **Status:** covered

---

## Traceability Matrix

| Test File | Requirements |
|---|---|
| test_accept_reject.py | REQ-006, REQ-007, REQ-012 |
| test_assistant_text_logging.py | REQ-008, REQ-009 |
| test_auto_start.py | REQ-011, REQ-017 |
| test_cli_commands.py | REQ-002, REQ-012 |
| test_cli_env.py | REQ-012, REQ-013 |
| test_cli_helpers.py | REQ-001, REQ-005, REQ-006, REQ-012 |
| test_composite_notifier.py | REQ-010 |
| test_config_factories.py | REQ-011, REQ-022 |
| test_docker_utils.py | REQ-019 |
| test_dotenv.py | REQ-013 |
| test_git_bug_lock_retry.py | REQ-025 |
| test_git_utils.py | REQ-001, REQ-006 |
| test_launch.py | REQ-001, REQ-018 |
| test_marker_reliability.py | REQ-002, REQ-003, REQ-004, REQ-005, REQ-015 |
| test_notification_level.py | REQ-010 |
| test_notifier_prefix.py | REQ-010 |
| test_post_container.py | REQ-005, REQ-018 |
| test_post_run.py | REQ-004, REQ-005, REQ-006 |
| test_prompts.py | REQ-004, REQ-011 |
| test_rebase.py | REQ-006 |
| test_review.py | REQ-005, REQ-008 |
| test_review_step.py | REQ-005, REQ-008, REQ-009, REQ-011 |
| test_search.py | REQ-011 |
| test_session_runner.py | REQ-002, REQ-003, REQ-004, REQ-015, REQ-016b, REQ-021 |
| test_session_utils_host.py | REQ-001, REQ-002, REQ-007, REQ-012 |
| test_static_tracker.py | REQ-016, REQ-016b |
| test_issue_redump.py | REQ-016b |
| watcher/test_issue_sync.py | REQ-016b |
| test_stream_parser.py | REQ-004, REQ-014 |
| test_upstream.py | REQ-027 |
| test_template_lint.py | REQ-027 |
| test_upgrade.py | REQ-024 |
| test_watcher.py | REQ-017, REQ-019, REQ-020 |
| test_worktree_git_fix.py | REQ-001 |
| oq1_stdin_test.py | REQ-003 |
| watcher/test_graceful_shutdown.py | REQ-002 |
| watcher/test_lifecycle_comments.py | REQ-002, REQ-008 |
| watcher/test_config_reload.py | REQ-023 |
| watcher/test_session_monitor.py | REQ-002, REQ-004, REQ-009, REQ-011, REQ-017 |
| watcher/test_host_watcher.py | REQ-009 |
| test_tracker_ipc.py | REQ-026 |
| test_socket_tracker_client.py | REQ-026 |
| test_tracker_fallback.py | REQ-026 |
| watcher/test_tracker_writer.py | REQ-026 |
| test_overflow.py | REQ-028 |

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
