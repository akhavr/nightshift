# Business Requirements — Nightshift

Executive summary of what nightshift must do, traced to tests.

---

### REQ-001: Launch agent in isolated workspace
The system creates a git worktree per issue and runs the coding agent inside a Docker container against that worktree.

- **Tests:** test_worktree_git_fix.py
- **Status:** partial

### REQ-002: Session lifecycle management
The system tracks session state (working, suspended, waiting, done) with atomic JSON persistence and exposes status via CLI.

- **Tests:** test_marker_reliability.py
- **Status:** partial

### REQ-003: Human Q&A
The agent can ask blocking questions to a human. The system pauses the container, collects answers via Telegram or CLI, and resumes the agent with the answer.

- **Tests:** test_marker_reliability.py, oq1_stdin_test.py
- **Status:** covered

### REQ-004: Checkpoint and resume on context limits
When the agent hits context limits, max turns, or stalls, the system saves progress, builds a resume prompt with checkpoint history, and auto-restarts the agent.

- **Tests:** test_marker_reliability.py
- **Status:** partial

### REQ-005: Review gate before merge
Agent work is not merged automatically. On completion, the system posts a proof-of-work summary and waits for human review before merging.

- **Tests:** test_marker_reliability.py, test_review.py, test_review_step.py, test_post_container.py
- **Status:** covered

### REQ-006: Accept (merge) agent work
A human can accept agent work via CLI, which merges the agent branch into the base branch with --no-ff, handles conflicts, cleans up worktrees, and closes the issue.

- **Tests:** test_accept_reject.py
- **Status:** covered

### REQ-007: Reject agent work
A human can reject agent work via CLI, which discards the worktree, branch, and session, and closes the issue.

- **Tests:** test_accept_reject.py
- **Status:** covered

### REQ-008: Revise agent work after review
A human (or automated reviewer) can request revisions. The system collects review feedback from tracker comments and restarts the agent with a revise prompt.

- **Tests:** test_review.py, test_review_step.py, test_assistant_text_logging.py
- **Status:** covered

### REQ-009: Automated code review
When a REVIEW.md template exists, the system automatically launches a reviewer agent against completed work. The reviewer can approve or request revisions, with configurable max review rounds.

- **Tests:** test_review_step.py, test_assistant_text_logging.py
- **Status:** covered

### REQ-010: Notifications via pluggable channels
The system sends notifications (status updates, questions, completion) through configurable channels (Telegram, webhook, Slack).

- **Tests:** test_notifier_prefix.py
- **Status:** partial

### REQ-011: Configuration via WORKFLOW.md
All adapter selection, runtime settings, hooks, and prompt templates are configured through a YAML-front-matter WORKFLOW.md file. Environment variables are resolved with `$VAR` syntax.

- **Tests:** test_auto_start.py (config parsing), test_review_step.py (config parsing)
- **Status:** partial

### REQ-012: CLI for all operations
Users manage nightshift through a CLI: init, start, resume, answer, status, logs, history, accept, reject, revise, cleanup, watcher.

- **Tests:** test_accept_reject.py, test_cli_env.py
- **Status:** partial

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

- **Tests:** test_marker_reliability.py
- **Status:** covered

### REQ-016: Static issue data inside containers
Inside the container, issue data is read from pre-dumped JSON files (no network access to the tracker). Write operations are logged but no-op.

- **Tests:** test_static_tracker.py
- **Status:** covered

### REQ-017: Container .git pointer fix
Docker worktrees have their .git pointer rewritten to use container-internal paths so git operations work inside the container.

- **Tests:** test_worktree_git_fix.py
- **Status:** covered

### REQ-018: Auto-start new issues
The host watcher can poll the tracker for new issues matching a configured label and auto-launch agent sessions, respecting a max concurrency limit.

- **Tests:** test_auto_start.py
- **Status:** covered

### REQ-019: Project name prefix in notifications
Notifications include a configurable project name prefix (from PROJECT_NAME env var) so multi-project setups are distinguishable.

- **Tests:** test_notifier_prefix.py
- **Status:** covered

### REQ-020: Conflict detection on merge
When accepting agent work, the system detects unresolved conflict markers in merged files and aborts the merge if found.

- **Tests:** test_accept_reject.py
- **Status:** covered

### REQ-021: Proof-of-work summary on completion
When the agent finishes, the system posts a summary of checkpoints, Q&A exchanges, and a diff stat to the issue tracker.

- **Tests:** test_post_container.py, test_marker_reliability.py
- **Status:** covered

### REQ-022: Host watcher pause/unpause
The host watcher monitors session directories, pauses idle containers (zero CPU), and unpauses them when answers arrive.

- **Tests:** _(none)_
- **Status:** untested

### REQ-023: External cancellation
If the tracked issue is closed externally while the agent is running, the session is cancelled gracefully.

- **Tests:** _(none)_
- **Status:** untested

### REQ-024: Workspace hooks
Users can configure shell hooks (after_create, before_run, after_run) that run at lifecycle points. A fatal hook failure stops the session.

- **Tests:** _(none)_
- **Status:** untested

### REQ-025: Pluggable adapters
The system supports swappable adapters for agents, trackers, workspaces, and notifiers via a registry + dynamic import pattern. New adapters require only a registry entry and protocol implementation.

- **Tests:** _(none)_
- **Status:** untested

---

## Traceability Matrix

| Test File | Requirements |
|---|---|
| test_accept_reject.py | REQ-006, REQ-007, REQ-020 |
| test_assistant_text_logging.py | REQ-008, REQ-009 |
| test_auto_start.py | REQ-011, REQ-018 |
| test_cli_env.py | REQ-012, REQ-013 |
| test_dotenv.py | REQ-013 |
| test_marker_reliability.py | REQ-002, REQ-003, REQ-004, REQ-005, REQ-015, REQ-021 |
| test_notifier_prefix.py | REQ-010, REQ-019 |
| test_post_container.py | REQ-005, REQ-021 |
| test_review.py | REQ-005, REQ-008 |
| test_review_step.py | REQ-008, REQ-009, REQ-011 |
| test_static_tracker.py | REQ-016 |
| test_stream_parser.py | REQ-014 |
| test_worktree_git_fix.py | REQ-001, REQ-017 |
| oq1_stdin_test.py | REQ-003 |

---

## Orphan Tests

No orphan tests — all test files map to at least one requirement.

**Note:** `oq1_stdin_test.py` maps to REQ-003 but requires manual execution with a real Claude Code installation. It is not included in automated CI runs. Consider keeping it as a manual verification script.

---

## Untested Requirements

| Requirement | Gap |
|---|---|
| REQ-001: Launch agent in isolated workspace | Only .git pointer fix is tested. No tests for full Docker launch, worktree creation, or container volume mounting. |
| REQ-002: Session lifecycle management | Marker tests cover some state transitions, but no direct tests for StateManager atomic writes, conversation logging, or status queries. |
| REQ-004: Checkpoint and resume on context limits | Auto-resume on exit-without-done is tested, but no tests for context-limit detection, stall detection, resume prompt construction, or checkpoint summarization. |
| REQ-010: Notifications via pluggable channels | Prefix behavior tested, but no tests for Telegram API calls, webhook delivery, or CompositeNotifier broadcast. |
| REQ-011: Configuration via WORKFLOW.md | Config parsing tested for auto_start and review sections only. No tests for agent/tracker/workspace/notifications/merge/hooks config parsing or $VAR resolution. |
| REQ-012: CLI for all operations | Only accept/reject and .env loading tested. No tests for init, start, resume, answer, status, logs, history, cleanup, or watcher commands. |
| REQ-022: Host watcher pause/unpause | No tests for container pause/unpause, waiting.json detection, answer.txt writing, or Telegram polling in the watcher. |
| REQ-023: External cancellation | No tests for issue-closed detection during a running session. |
| REQ-024: Workspace hooks | No tests for hook execution, fatal hook failure, or hook timeout. |
| REQ-025: Pluggable adapters | No tests for adapter registry, dynamic import, or factory functions. |
