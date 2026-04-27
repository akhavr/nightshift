# Session Lifecycle

This document describes the state machine for nightshift sessions.

## Status Values

### Active States

| Status | Description | Next States |
|--------|-------------|-------------|
| `starting` | Session created, container not yet running | `working` |
| `working` | Agent actively processing | `done:pending-review`, `waiting:question`, `suspended:*`, `cancelled:external` |
| `reviewing` | Review container running | `waiting:review`, `waiting:human-review` |

### Waiting States

| Status | Description | Next States |
|--------|-------------|-------------|
| `waiting:question` | Agent asked a question, waiting for human input | `waiting:answer` (implicit when answer arrives) |
| `waiting:answer` | Answer received, agent paused | `working` (on resume) |
| `waiting:review` | Work complete, awaiting review decision | `reviewing`, `accepted`, `rejected`, `working` (if revise) |
| `waiting:human-review` | Automated review failed/no verdict, needs human | `accepted`, `rejected`, `working` (if revise) |

### Suspended States (Auto-Resumable)

| Status | Description | Recovery |
|--------|-------------|----------|
| `suspended:context-limit` | Hit token limit | Auto-resumes with checkpoint summary |
| `suspended:stall` | No output for `stall_timeout_s` | Auto-resumes |
| `suspended:answer-ready` | Agent exited, answer collected | Auto-resumes with answer as prompt |
| `suspended:max-resumes` | Hit MAX_RESUMES (10) limit | Manual `nightshift resume` |
| `suspended:auth-failure` | API auth failed (401, invalid key) | Watcher retries every 5 min up to 6 times |
| `suspended:auth-failure-permanent` | Auth retries exhausted | Manual intervention needed |
| `suspended:provider-overload` | Transient 429/5xx errors | Auto-retries with backoff |
| `suspended:hook-failure` | before_run/after_run hook failed | Manual fix required |
| `suspended:unexpected` | Unexpected agent exit | Manual `nightshift resume` |
| `suspended:review-no-verdict` | Review hit max-turns without verdict | Transitions to `waiting:human-review` |

### Terminal States

| Status | Description |
|--------|-------------|
| `accepted` | Work merged via `nightshift accept` |
| `rejected` | Work discarded via `nightshift reject` |
| `cancelled:external` | Issue closed externally while agent was working |
| `cancelled:review-rejected` | Review explicitly rejected |
| `error:merge-conflict` | Merge failed due to conflicts |

## State Diagram

```
                                    ┌──────────────────────────────────────────┐
                                    │                                          │
                                    ▼                                          │
┌──────────┐   start   ┌─────────┐      ┌───────────────────┐                 │
│ (no      │ ────────► │starting │ ───► │     working       │◄────────────────┤
│ session) │           └─────────┘      └───────────────────┘                 │
└──────────┘                                    │                             │
                                               │                             │
           ┌───────────────────┬───────────────┼───────────────┬─────────────┤
           │                   │               │               │             │
           ▼                   ▼               ▼               ▼             │
    ┌─────────────┐    ┌───────────────┐ ┌───────────┐  ┌────────────┐       │
    │waiting:     │    │suspended:     │ │done:      │  │cancelled:  │       │
    │question     │    │context-limit  │ │pending-   │  │external    │       │
    └─────────────┘    │stall          │ │review     │  └────────────┘       │
           │           │max-resumes    │ └───────────┘                       │
           │           │auth-failure   │       │                             │
           ▼           │hook-failure   │       ▼                             │
    ┌─────────────┐    │unexpected     │ ┌───────────┐  ┌────────────┐       │
    │suspended:   │    └───────────────┘ │waiting:   │  │reviewing   │       │
    │answer-ready │           │          │review     │◄─┤            │       │
    └─────────────┘           │          └───────────┘  └────────────┘       │
           │                  │                │               │             │
           │     auto-resume  │                │               │             │
           └──────────────────┼────────────────┼───────────────┘             │
                              │                │                             │
                              │                ▼                             │
                              │     ┌───────────────────┐                    │
                              │     │ waiting:human-    │                    │
                              │     │ review            │                    │
                              │     └───────────────────┘                    │
                              │                │                             │
                              │                ├──────────────────────► revise
                              │                │
                              │                ▼
                              │     ┌───────────────────┐
                              │     │ accepted/rejected │
                              │     └───────────────────┘
                              │
                              ▼
                     manual resume needed
```

## Lifecycle Flows

### Happy Path (Coder)

1. **start**: `nightshift start <id>` creates worktree, session dir, launches container
   - Status: `starting` → `working`
   - Source: `entrypoint.py:94`

2. **work**: Agent processes issue, makes commits
   - Status: `working`
   - Checkpoints logged via `@@CHECKPOINT@@`

3. **done**: Agent outputs `@@DONE@@`
   - Status: `working` → `done:pending-review` → `waiting:review`
   - Source: `core/session.py:383`, `core/post_run.py:171`
   - Posts proof-of-work comment, adds `needs-review` label

4. **review**: Watcher launches review container
   - Status: `waiting:review` → `reviewing`
   - Source: `host/watcher/review_orchestrator.py:178`

5. **verdict**: Reviewer outputs `@nightshift approve` or `@nightshift revise`
   - If approve: Status → `waiting:human-review` (human confirms)
   - If revise: Status → `working` (coder resumes with feedback)
   - Source: `host/watcher/verdict_handler.py`

6. **accept/reject**: Human decision
   - `nightshift accept`: merges branch → `accepted`
   - `nightshift reject`: discards work → `rejected`
   - Source: `host/cli.py`

### Q&A Flow

1. Agent outputs `@@QUESTION@@<text>@@WAITING@@`
   - Status: `working` → `waiting:question`
   - Source: `core/qa_flow.py:27`
   - Container paused, notification sent

2. Human provides answer (Telegram, tracker comment, or `nightshift answer`)
   - Answer written to `answer.txt`

3. Agent resumed with answer as prompt
   - Status: `suspended:answer-ready` → `working`
   - Source: `core/qa_flow.py:77`, `core/post_run.py:204`

### Auto-Resume Flow

1. Context limit / stall / max-turns detected
   - Status: `working` → `suspended:<reason>`
   - WIP committed, resume prompt built

2. `_post_run()` checks if resumable
   - Source: `core/post_run.py:36-60`
   - Builds checkpoint summary, appends to prompt

3. Agent restarted with `--resume`
   - Status: `suspended:<reason>` → `working`
   - Up to MAX_RESUMES (10) attempts

### Auth Failure Recovery

1. Agent returns 401 / invalid key error
   - Status: `working` → `suspended:auth-failure`
   - Source: `core/session.py:184`

2. Watcher retries every 5 min (AUTH_RETRY_INTERVAL_S)
   - Up to MAX_AUTH_RETRIES (6) attempts
   - Source: `host/watcher/session_monitor.py`

3. If retries exhausted:
   - Status: → `suspended:auth-failure-permanent`
   - Manual intervention required

## Code References

| Component | File | Responsibility |
|-----------|------|----------------|
| Container entrypoint | `entrypoint.py` | Initial `working` status |
| Session runner | `core/session.py` | Event loop, status transitions during work |
| Q&A flow | `core/qa_flow.py` | Question/answer handling |
| Post-run | `core/post_run.py` | Determines next action after agent exits |
| CLI | `host/cli.py` | accept/reject/revise commands |
| Session monitor | `host/watcher/session_monitor.py` | Orphan detection, auth retry |
| Review orchestrator | `host/watcher/review_orchestrator.py` | Review launch, rebase |
| Verdict handler | `host/watcher/verdict_handler.py` | Process approve/revise |
| Command executor | `host/watcher/command_executor.py` | CLI command dispatch |

## Session Files

Located in `.nightshift/sessions/<issue-id>/`:

| File | Purpose |
|------|---------|
| `state.json` | Current status, step count, checkpoints |
| `conversation.jsonl` | Full conversation history |
| `raw-output.log` | Raw agent output stream |
| `resume-prompt.md` | Built prompt for next resume |
| `waiting.json` | Question details when paused |
| `answer.txt` | Human answer to question |
| `tracker-outbox.jsonl` | Pending tracker operations |
| `diff.patch` | Cached diff for review |
