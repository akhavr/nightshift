# Hidden Modules: Architectural Simplification

Three modules with clean interfaces hiding inside nightshift. This document defines TDD issues to extract them incrementally, with each issue resulting in working, wired code.

---

## Module 1: Session State Machine (SSM)

**Problem:** Session lifecycle is implicit across 6+ files. Race conditions occur because `status` and `completed_at` are set separately.

**Solution:** Explicit FSM with atomic transitions.

### SSM-1: SSM class with validation wired into StateManager

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

### SSM-2: SSM owns status, JSON is persistence only

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

### SSM-3: Atomic mark_done via SSM (race condition eliminated)

**Tests:**
```
tests/test_state_machine.py::test_terminal_state_sets_completed_at
tests/test_state.py::test_transition_to_waiting_review_sets_completed_at
tests/test_state.py::test_mark_done_method_removed
tests/test_post_run.py::test_notify_done_uses_transition
```

**Files:** core/state_machine.py, core/state.py, core/post_run.py

**Implementation:**
1. SSM.TERMINAL_STATES = {'waiting:review', 'accepted', 'rejected', 'closed'}
2. Transition to terminal state atomically sets completed_at
3. Remove mark_done(), mark_completed() - use transition()
4. post_run.notify_done() calls state_mgr.transition('waiting:review')

**Wired:** The race condition (status="working" + completed_at set) is impossible by construction.

---

### SSM-4: Hooks for logging and notifications

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

### WT-2: docker-entrypoint.sh uses WorkspaceTransaction

**Tests:**
```
tests/test_entrypoint_git.py::test_git_pointer_restored_on_exit
tests/test_entrypoint_git.py::test_git_pointer_restored_on_error
```

**Files:** docker-entrypoint.sh, entrypoint.py

**Implementation:**
1. Python wrapper calls WorkspaceTransaction
2. Shell script delegates to Python for .git handling
3. EXIT trap removed (handled by Python)

**Wired:** Container .git pointer handling uses WT. Bug impossible.

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

## Execution Order

Priority based on impact and dependencies:

| Phase | Issues | Key Outcome |
|-------|--------|-------------|
| 1 | SSM-1 to SSM-3 | Race condition eliminated |
| 2 | SSM-4 to SSM-9 | Full SSM integration |
| 3 | AES-1 to AES-2 | Event foundation + ClaudeCode |
| 4 | AES-3 to AES-6 | All agents unified |
| 5 | WT-1 to WT-2 | .git pointer bulletproof |
| 6 | WT-3 to WT-5 | Full transactional git |

**Total: 20 issues, each TDD, each wired to real flow.**

After Phase 1 (3 issues): Race condition fixed structurally.
After Phase 2 (6 more): SSM complete, lifecycle explicit.
After Phase 4 (6 more): Adding new agents is trivial.
After Phase 6 (5 more): Git operations are bulletproof.
