# Hidden Modules: Architectural Simplification

Three modules with clean interfaces hiding inside nightshift. This document defines TDD issues to extract them incrementally, with each issue resulting in working, wired code.

---

## Module 1: Session State Machine (SSM)

**Problem:** Session lifecycle is implicit across 6+ files. Race conditions occur because `status` and `completed_at` are set separately.

**Solution:** Explicit FSM with atomic transitions.

### SSM-1: SSM class with validation wired into StateManager [DONE]

**Issue:** b5e7ac61001a (implemented via 0250e161a6d2)

**Tests (write first, must fail):**
```
tests/test_state_machine.py::test_initial_state_is_starting
tests/test_state_machine.py::test_valid_transition_succeeds
tests/test_state_machine.py::test_invalid_transition_raises_error
tests/test_state_machine.py::test_can_transition_returns_bool
tests/test_state.py::test_update_status_rejects_invalid_transition
tests/test_state.py::test_update_status_accepts_valid_transition
```

**Files:** core/state_machine.py (create), core/state.py (modify), tests/

**Implementation:**
1. SessionStateMachine with STATES, TRANSITIONS, transition(), can_transition()
2. StateManager.__init__() creates SSM from loaded status
3. StateManager.update_status() validates via SSM before writing JSON

**Wired:** Invalid status transitions rejected at runtime. All existing tests pass.

---

### SSM-2: SSM owns status, JSON is persistence only [DONE]

**Issue:** 0250e161a6d2

**Tests:**
```
tests/test_state.py::test_status_property_reads_from_ssm
tests/test_state.py::test_load_state_initializes_ssm
tests/test_state.py::test_transition_persists_to_json
```

**Files:** core/state.py (modify)

**Implementation:**
1. StateManager.status returns self._ssm.state
2. load_state() initializes SSM from JSON
3. All status reads go through SSM

**Wired:** In-memory status is always consistent. Race window narrowed.

---

### SSM-3: Atomic mark_done via SSM (race condition eliminated) [DONE]

**Issue:** 43e781891181 (commit 07d05553)

**Tests:**
```
tests/test_state_machine.py::test_terminal_state_sets_completed_at
tests/test_state.py::test_transition_to_waiting_review_sets_completed_at
tests/test_state.py::test_mark_done_method_removed
tests/test_post_run.py::test_notify_done_uses_transition
```

**Files:** core/state_machine.py, core/state.py, core/post_run.py

**Implementation:**
1. SSM.TERMINAL_STATES = {'accepted', 'rejected', 'closed'}
2. SSM.COMPLETION_STATES = {'waiting:review', 'waiting:human-review'} — completed_at set but resumable
3. Transition to terminal state atomically sets completed_at
4. Transition to completion state sets completed_at but allows resume (clears on resume)
5. Remove mark_done(), mark_completed() - use transition()
6. post_run.notify_done() calls state_mgr.transition('waiting:review')

**Wired:** The race condition (status="working" + completed_at set) is impossible by construction.

**Note:** `waiting:review` is NOT terminal — it can transition to `reviewing` or back to `working` (rebase conflict, revise verdict). Only `accepted`, `rejected`, `closed` are truly terminal.

---

### SSM-4: Hooks for logging and notifications

**Issue:** d2000b5c84aa

**Tests:**
```
tests/test_state_machine.py::test_on_enter_hook_called
tests/test_state_machine.py::test_on_exit_hook_called
tests/test_state_machine.py::test_hook_receives_context
tests/test_state.py::test_transition_logs_state_change
```

**Files:** core/state_machine.py, core/state.py

**Implementation:**
1. SSM.register_hook(state, 'enter'|'exit', callback)
2. transition() calls hooks with context dict
3. StateManager registers logging hook

**Wired:** State changes auto-log. Foundation for notification hooks.

---

### SSM-5: Watcher uses SSM-aware StateManager

**Issue:** 57561f29274b

**Tests:**
```
tests/watcher/test_session_monitor.py::test_orphan_check_uses_state_manager
tests/watcher/test_session_monitor.py::test_consistent_status_read
```

**Files:** host/watcher/session_monitor.py, host/session_utils.py

**Implementation:**
1. session_monitor loads StateManager (not raw JSON)
2. Orphan detection uses state_mgr.status

**Wired:** Watcher and container see consistent status.

---

### SSM-6: Q&A flow uses SSM transitions

**Issue:** 954974afa32b

**Tests:**
```
tests/test_qa_flow.py::test_question_transitions_to_waiting
tests/test_qa_flow.py::test_answer_transitions_to_working
tests/watcher/test_qa_handler.py::test_qa_validates_transition
```

**Files:** core/qa_flow.py, host/watcher/qa_handler.py

**Implementation:**
1. raise_question() -> transition('waiting:question')
2. deliver_answer() -> transition('working')

**Wired:** Q&A lifecycle is SSM-controlled with validation.

---

### SSM-7: Review flow uses SSM transitions

**Issue:** 9ed266080dbd

**Tests:**
```
tests/watcher/test_review_orchestrator.py::test_launch_transitions_to_reviewing
tests/watcher/test_review_orchestrator.py::test_done_transitions_to_human_review
tests/watcher/test_verdict_handler.py::test_accept_transitions_to_accepted
tests/watcher/test_verdict_handler.py::test_reject_transitions_to_rejected
```

**Files:** host/watcher/review_orchestrator.py, host/watcher/verdict_handler.py

**Implementation:**
1. launch_review() -> transition('reviewing')
2. review done -> transition('waiting:human-review')
3. accept/reject -> transition('accepted'/'rejected')

**Wired:** Full review lifecycle SSM-controlled.

---

### SSM-8: CLI commands use SSM transitions

**Issue:** ab92d978d314

**Tests:**
```
tests/test_cli_commands.py::test_accept_validates_state
tests/test_cli_commands.py::test_accept_from_wrong_state_fails_with_message
tests/test_cli_commands.py::test_reject_validates_state
tests/test_cli_commands.py::test_resume_validates_state
```

**Files:** host/cli.py

**Implementation:**
1. cmd_accept -> transition('accepted')
2. cmd_reject -> transition('rejected')
3. Invalid state gives clear error

**Wired:** CLI commands validate via SSM.

---

### SSM-9: Remove legacy status code

**Issue:** f90977b5544b

**Tests:**
```
tests/test_codebase_audit.py::test_no_direct_status_writes
```

**Files:** Audit all, remove direct status manipulation

**Implementation:**
1. Grep for direct state.json status writes
2. Remove or migrate remaining callsites
3. status field read-only in SessionState

**Wired:** SSM is single source of truth. Clean codebase.

---

### SSM-10: Auto-cleanup stale review sessions

**Issue:** 83f750cd84e5

**Problem:** Completed review sessions (review-<id>) linger in `waiting:review` status after verdict processed. Manual `nightshift cleanup` required. Discovered when multiple stale reviews cluttered status output.

**Tests:**
```
tests/watcher/test_session_monitor.py::test_cleanup_completed_review_after_verdict
tests/watcher/test_session_monitor.py::test_no_cleanup_active_review
tests/watcher/test_session_monitor.py::test_cleanup_only_when_coder_transitioned
```

**Files:** host/watcher/session_monitor.py, host/watcher/verdict_handler.py

**Implementation:**
1. After processing approve/revise verdict, check if review session completed
2. If review session has completed_at and coder session transitioned, auto-archive review
3. Grace period (60s) to ensure verdict fully processed before cleanup

**Wired:** Review sessions auto-cleanup after verdict applied. Status output stays clean.

**Note:** This also fixes the "stale status after accept" UX issue - completed sessions won't linger in `nightshift status` output.

---

### SSM-11: Resume from completion states (rebase/revise fix) [DONE]

**Issue:** f2a6311 (commit 329f6522)

**Problem:** When coder completes, `completed_at` is set and status becomes `waiting:review`. If pre-review rebase conflicts (or revise verdict issued), watcher tries to resume coder but resume logic rejects because `completed_at` is set. This creates a loop: rebase fails → resume blocked → revert to waiting:review → rebase fails → loop.

**Root cause:** `waiting:review` was incorrectly treated as terminal. Resume logic checks `completed_at` to block resume of "completed" sessions, but completion states need to be resumable.

**Tests:**
```
tests/test_state_machine.py::test_completion_state_allows_resume
tests/test_state_machine.py::test_terminal_state_blocks_resume
tests/test_state_machine.py::test_resume_clears_completed_at
tests/watcher/test_review_orchestrator.py::test_rebase_conflict_resumes_coder
tests/watcher/test_verdict_handler.py::test_revise_resumes_coder
```

**Files:** core/state_machine.py, core/state.py, host/watcher/review_orchestrator.py, host/watcher/verdict_handler.py

**Implementation:**
1. Distinguish TERMINAL_STATES (truly done) from COMPLETION_STATES (done but resumable)
2. Resume logic checks TERMINAL_STATES, not completed_at
3. On resume from completion state, clear completed_at
4. review_orchestrator: on rebase conflict, transition to 'working' (clears completed_at)
5. verdict_handler: on revise, transition to 'working' (clears completed_at)

**Wired:** Rebase conflicts and revise verdicts properly resume coder. No more loops.

**Related gaps this fixes:**
- Rebase conflict after coder completion
- Revise verdict resumes blocked coder
- Auth-failure retry from suspended state
- Orphan recovery from reviewing state

---

## Module 2: Agent Event Stream (AES)

**Problem:** Three signal paths (markers, file signals, JSON) parsed separately per agent. SessionRunner has agent-specific code.

**Solution:** Unified AgentEvent stream that all agents emit.

### AES-1: AgentEvent dataclass and enum

**Tests:**
```
tests/test_agent_events.py::test_event_creation
tests/test_agent_events.py::test_event_types_complete
tests/test_agent_events.py::test_event_serialization
tests/test_agent_events.py::test_event_from_dict
```

**Files:** core/agent_events.py (create)

**Implementation:**
1. AgentEventType enum: STARTED, TEXT, TOOL_CALL, TOOL_RESULT, QUESTION, CHECKPOINT, DONE, ERROR, AUTH_FAILURE
2. AgentEvent dataclass: type, timestamp, content, metadata
3. Serialization to/from dict

**Wired:** Foundation exists. No behavior change yet.

---

### AES-2: ClaudeCodeAgent emits AgentEvent

**Tests:**
```
tests/test_claude_code_agent.py::test_stream_yields_agent_events
tests/test_claude_code_agent.py::test_done_marker_becomes_done_event
tests/test_claude_code_agent.py::test_question_marker_becomes_question_event
tests/test_claude_code_agent.py::test_auth_failure_becomes_auth_event
```

**Files:** adapters/agents/claude_code.py, core/agent_events.py

**Implementation:**
1. _parse() returns AgentEvent instead of raw dict
2. Marker parsing produces QUESTION/DONE/CHECKPOINT events
3. stream() yields AgentEvent objects

**Wired:** ClaudeCodeAgent speaks unified events. SessionRunner still works (duck typing).

---

### AES-3: OpenHandsAgent emits AgentEvent

**Tests:**
```
tests/test_openhands_agent.py::test_stream_yields_agent_events
tests/test_openhands_agent.py::test_observation_becomes_tool_result
tests/test_openhands_agent.py::test_action_becomes_tool_call
```

**Files:** adapters/agents/openhands.py

**Implementation:**
1. Parse JSON events into AgentEvent
2. Map OpenHands event types to AgentEventType

**Wired:** OpenHands speaks unified events.

---

### AES-4: CodexAgent emits AgentEvent

**Tests:**
```
tests/test_codex_agent.py::test_stream_yields_agent_events
tests/test_codex_agent.py::test_jsonl_parsed_to_events
```

**Files:** adapters/agents/codex.py

**Implementation:**
1. Parse JSONL into AgentEvent
2. Map Codex event types

**Wired:** All three agents speak unified events.

---

### AES-5: SessionRunner consumes AgentEvent stream

**Tests:**
```
tests/test_session_runner.py::test_handles_done_event
tests/test_session_runner.py::test_handles_question_event
tests/test_session_runner.py::test_handles_auth_failure_event
tests/test_session_runner.py::test_agent_agnostic_event_loop
```

**Files:** core/session.py

**Implementation:**
1. Event loop dispatches on event.type
2. Remove agent-specific marker parsing
3. match event.type: case DONE: ... case QUESTION: ...

**Wired:** SessionRunner is agent-agnostic. Adding new agents trivial.

---

### AES-6: File signals become events

**Tests:**
```
tests/test_session_runner.py::test_file_signal_done_becomes_event
tests/test_session_runner.py::test_file_signal_question_becomes_event
```

**Files:** core/session.py

**Implementation:**
1. _check_file_signals() yields AgentEvent
2. Merge file signal events into main stream
3. Single event handling path

**Wired:** All three signal paths unified into AgentEvent stream.

---

## Module 3: Workspace Transaction (WT)

**Problem:** Git operations scattered. Entry/exit not paired (.git pointer bug).

**Solution:** Transactional context manager with auto-cleanup.

### WT-0: Worktree integrity check before operations

**Problem:** Worktree metadata (`.git/worktrees/<name>/`) goes missing or corrupts, causing "not a git repository" errors. Discovered when multiple sessions needed `git worktree repair`.

**Tests:**
```
tests/test_workspace_transaction.py::test_check_worktree_integrity_valid
tests/test_workspace_transaction.py::test_check_worktree_integrity_missing_metadata
tests/test_workspace_transaction.py::test_check_worktree_integrity_auto_repair
tests/test_host_rebase.py::test_rebase_repairs_broken_worktree
```

**Files:** core/workspace_transaction.py (create), host/rebase.py (modify)

**Implementation:**
1. check_worktree_integrity(worktree_path) verifies .git file and metadata dir exist
2. repair_worktree() runs `git worktree repair` if metadata missing
3. Called before git operations in host/rebase.py and host/merge.py

**Wired:** Host operations self-heal broken worktree metadata.

---

### WT-1: WorkspaceTransaction with .git pointer

**Tests:**
```
tests/test_workspace_transaction.py::test_context_manager_restores_git_pointer
tests/test_workspace_transaction.py::test_exception_triggers_restore
tests/test_workspace_transaction.py::test_rewrite_git_pointer
tests/test_workspace_transaction.py::test_nested_transactions_error
```

**Files:** core/workspace_transaction.py (create)

**Implementation:**
1. Context manager saves .git content on enter
2. rewrite_git_pointer() changes pointer
3. __exit__ restores original pointer
4. Exception handling for rollback

**Wired:** Foundation exists. Can test in isolation.

---

### WT-1.5: Host-side gitdir sanitization (defense in depth) [DONE]

**Issue:** abdbb1e

**Problem:** Container may crash before cleanup. Host operations fail with "not a git repository" or `core.worktree` pollution breaks all git commands.

**Tests:**
```
tests/test_host_rebase.py::test_rebase_fixes_container_gitdir (EXISTS - 88acf5d)
tests/test_host_rebase.py::test_rebase_preserves_valid_gitdir (EXISTS - 88acf5d)
tests/test_host_rebase.py::test_sanitize_removes_core_worktree
tests/test_host_rebase.py::test_sanitize_preserves_valid_config
```

**Files:** host/rebase.py, host/git_utils.py

**Implementation:**
1. _fix_container_gitdir() detects /repo-git/ paths, rewrites to host paths (DONE: 88acf5d)
2. _sanitize_git_config() removes core.worktree if set to container path (/workspace)
3. Called before any host-side git operation (rebase, merge, accept)

**Wired:** Host operations self-heal corrupt git state. Container cleanup (WT-2) is defense-in-depth, not primary protection.

---

### WT-2: docker-entrypoint.sh uses WorkspaceTransaction (defense in depth)

**Tests:**
```
tests/test_entrypoint_git.py::test_git_pointer_restored_on_exit
tests/test_entrypoint_git.py::test_git_pointer_restored_on_error
tests/test_entrypoint_git.py::test_no_config_pollution_on_exit
```

**Files:** docker-entrypoint.sh, entrypoint.py

**Implementation:**
1. Python wrapper calls WorkspaceTransaction
2. Shell script delegates to Python for .git handling
3. EXIT trap restores .git pointer AND removes any core.worktree pollution
4. On crash: host-side WT-0 and WT-1.5 provide backup protection

**Wired:** Container cleanup is defense-in-depth. Host-side sanitization (WT-1.5) is primary protection since containers can crash/be killed.

**Note:** This is secondary to WT-1.5. Even if container cleanup fails, host operations self-heal.

---

### WT-3: Branch operations in WorkspaceTransaction

**Tests:**
```
tests/test_workspace_transaction.py::test_create_branch_with_rollback
tests/test_workspace_transaction.py::test_checkout_with_rollback
tests/test_workspace_transaction.py::test_rollback_deletes_created_branch
```

**Files:** core/workspace_transaction.py

**Implementation:**
1. create_branch() with auto-delete on rollback
2. checkout() with auto-restore on rollback
3. Rollback stack for multiple operations

**Wired:** Branch operations are transactional.

---

### WT-4: workspace_setup.py uses WorkspaceTransaction

**Tests:**
```
tests/test_workspace_setup.py::test_setup_uses_transaction
tests/test_workspace_setup.py::test_setup_failure_cleans_up
```

**Files:** host/workspace_setup.py

**Implementation:**
1. Wrap worktree creation in transaction
2. Wrap branch setup in transaction
3. Failure rolls back partial work

**Wired:** Host-side workspace setup is transactional.

---

### WT-5: Merge and rebase in WorkspaceTransaction

**Tests:**
```
tests/test_workspace_transaction.py::test_merge_returns_result
tests/test_workspace_transaction.py::test_merge_conflict_detected
tests/test_workspace_transaction.py::test_rebase_with_conflict_aborts
```

**Files:** core/workspace_transaction.py, host/rebase.py, host/merge.py

**Implementation:**
1. merge() returns MergeResult with conflict info
2. rebase() with auto-abort on failure
3. Migrate rebase.py and merge.py to use WT

**Wired:** All git operations go through WT.

---

### WT-6: Safe worktree prune (no collateral damage) [DONE]

**Issue:** 89f882f

**Problem:** `git worktree prune` is called in `session_utils.remove_worktree()` and `workspace_setup.create_worktree()`. It's a **global** operation that prunes ALL orphaned worktrees, not just the one being removed/created.

If any worktree has a corrupted `.git` file (pointing to `/repo-git/...` container path), prune sees it as orphaned and deletes its metadata — even if it's actively in use by a running container.

**Observed failure:** Session A cleanup runs prune → deletes metadata for corrupted session B → session B's commits become unreachable.

**Tests:**
```
tests/test_session_utils_host.py::test_remove_worktree_no_global_prune
tests/test_session_utils_host.py::test_prune_skips_active_sessions
tests/test_session_utils_host.py::test_prune_fixes_corrupted_gitdir_first
tests/test_workspace_setup.py::test_create_worktree_no_collateral_prune
```

**Files:** host/session_utils.py, host/workspace_setup.py, host/rebase.py

**Implementation (defense in depth - all 3):**
1. **Don't call global prune** — use `git worktree remove <path>` for specific worktree only, not global prune
2. **Fix corrupted .git files first** — before any prune, scan all worktrees and fix `/repo-git/...` paths to host paths (reuse `_fix_container_gitdir()` from rebase.py)
3. **Check session status before prune** — skip prune if any session is `working`, `starting`, `reviewing` (active states)

**Wired:** Cleanup of one session never damages another. Corrupted worktrees are healed, not pruned.

---

## SSM Dependency Chain

**IMPORTANT:** SSM issues must be completed in order. Each issue depends on the previous one.

```
SSM-4 → SSM-5 → SSM-6 → SSM-7 → SSM-8 → SSM-9 → SSM-10
```

| Issue | Depends On | Description |
|-------|------------|-------------|
| SSM-4 | SSM-3 (done) | Hooks for logging and notifications |
| SSM-5 | SSM-4 | Watcher uses SSM-aware StateManager |
| SSM-6 | SSM-5 | Q&A flow uses SSM transitions |
| SSM-7 | SSM-6 | Review flow uses SSM transitions |
| SSM-8 | SSM-7 | CLI commands use SSM transitions |
| SSM-9 | SSM-8 | Remove legacy status code |
| SSM-10 | SSM-9 | Auto-cleanup stale review sessions |

**When labeling for nightshift:** Only label the next issue in the chain once the previous one is accepted. Do not label multiple SSM issues at once.

---

## Execution Order

Priority based on impact and dependencies:

| Phase | Issues | Key Outcome | Status |
|-------|--------|-------------|--------|
| 1 | SSM-1 to SSM-3 | Race condition eliminated | **DONE** |
| 2 | SSM-4 to SSM-9 | Full SSM integration | TODO |
| 2.5 | SSM-10, SSM-11 | Cleanup + resume-from-completion fix | SSM-11 DONE, SSM-10 TODO |
| 3 | AES-1 to AES-2 | Event foundation + ClaudeCode | TODO |
| 4 | AES-3 to AES-6 | All agents unified | TODO |
| 5 | WT-0, WT-1, WT-1.5, WT-1.6, WT-1.7, WT-6 | Git state self-healing + safe prune | **DONE** |
| 6 | WT-2 to WT-5 | Full transactional git | TODO |

**Total: 27 issues, each TDD, each wired to real flow.**

**Completed:** SSM-1, SSM-2, SSM-3, SSM-11, WT-1.5, WT-1.6, WT-1.7, WT-6 (8 issues)
**Remaining:** SSM-4 to SSM-10, AES-1 to AES-6, WT-0 to WT-5 (19 issues)

After Phase 1 (3 issues): Race condition fixed structurally. **DONE**
After Phase 2 (6 more): SSM complete, lifecycle explicit.
After Phase 2.5 (2 more): Rebase/revise loops fixed, stale sessions auto-cleanup. (SSM-11 done)
After Phase 4 (6 more): Adding new agents is trivial.
After Phase 5 (6 issues): Git state self-heals from container corruption, safe prune prevents collateral damage. **DONE**
After Phase 6 (5 more): Git operations are bulletproof.
