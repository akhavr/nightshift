# Codex CLI Discovery Findings

**Date:** 2026-04-03
**Codex version:** 0.118.0
**Model:** qwen/qwen3-coder via OpenRouter
**Config:** `~/.codex/config.toml` with `env_key = "OVERFLOW_API_KEY"`

## Q1: Event Structure (RESOLVED)

Codex uses **completely different** event types from OpenHands. No overlap.

Event format: JSONL (one JSON object per line, no `--JSON Event--` separators).

### Event types observed:

| Event type | Fields | Meaning |
|---|---|---|
| `thread.started` | `thread_id` (UUID) | Session created |
| `turn.started` | (none) | Agent turn begins |
| `item.completed` | `item.id`, `item.type`, `item.text` OR `item.command`/`item.exit_code`/`item.aggregated_output` | Completed work unit |
| `item.started` | Same as completed but `status: "in_progress"` | Work unit in progress |
| `turn.completed` | `usage.input_tokens`, `usage.cached_input_tokens`, `usage.output_tokens` | Turn finished with token usage |
| `turn.failed` | `error.message` | Turn failed |
| `error` | `message` | Error (auth, connection, etc.) |

### Item types:
- `agent_message` — text output (`item.text`)
- `command_execution` — shell command (`item.command`, `item.aggregated_output`, `item.exit_code`, `item.status`)

## Q2: Session ID (RESOLVED)

Session ID is the `thread_id` field in the `thread.started` event (first event emitted).
Format: UUID (`019d53b2-73dc-79e2-9fdc-a6b13ea8c7b7`).

**NOT in stderr.** Stderr only contains "Reading additional input from stdin..."

Resume syntax: `codex exec resume <thread_id> "prompt"`

## Q3: Subscription (CONFIRMED)

Works with OpenRouter — no ChatGPT Plus required when using a custom provider.
Config: `model_provider = "openrouter"` with `env_key = "OVERFLOW_API_KEY"`.

## Q4: Model Selection (RESOLVED)

- CLI flag: `-m "qwen/qwen3-coder"`
- Config: `model = "qwen/qwen3-coder"` in `~/.codex/config.toml`
- Supports any model available on the configured provider

## Q5: Workspace Handling (RESOLVED)

- Codex respects `cwd` — creates files in the current directory
- CLI flag: `-C <dir>` to specify working root
- No `--workspace` flag needed (unlike OpenHands)
- Creates `.codex` marker file in the workspace
- Requires git repo (use `--skip-git-repo-check` to bypass)

## Q6: Auth Error Patterns (RESOLVED)

Auth errors emit multiple events:
```json
{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: ...)"}
{"type":"error","message":"Reconnecting... 2/5 (...)"}
...
{"type":"error","message":"unexpected status 401 Unauthorized: ..."}
{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: ..."}}
```

Patterns to detect:
- `"status 401"` — invalid/missing API key
- `"Unauthorized"` — auth failure
- `"Reconnecting..."` — transient, but 5/5 = permanent failure
- `turn.failed` with error — definitive failure signal

## Q7: Token Usage (BONUS)

`turn.completed` includes usage data:
```json
{"type":"turn.completed","usage":{"input_tokens":20546,"cached_input_tokens":10304,"output_tokens":80}}
```

This directly feeds into the usage tracking feature (issue 1075e49).

## Q8: Format Overlap with OpenHands (RESOLVED)

**No overlap.** Completely different event schemas:
- OpenHands: `kind` field, ActionEvent/ObservationEvent/MessageEvent/StatusEvent
- Codex: `type` field, thread.started/turn.started/item.completed/turn.completed/error

Codex adapter needs a standalone parser — cannot share with OpenHands.

## Marker Mapping (Updated)

| Codex Event | Nightshift AgentEvent |
|---|---|
| `item.completed` + `type: agent_message` | TEXT |
| `item.completed` + `type: command_execution` | TOOL_CALL |
| `item.completed` + `type: command_execution` + `exit_code` | TOOL_RESULT |
| `turn.completed` | TEXT with `@@DONE@@` |
| `turn.failed` + auth pattern | AUTH_FAILURE |
| `error` + auth pattern | AUTH_FAILURE |
| `thread.started` | Extract `thread_id` as session ID |

## Resume Support (CONFIRMED)

Resume works with `codex exec resume <thread_id> "new prompt"`:
- Preserves full conversation context
- Same thread_id reused
- Agent sees previous files and actions

## Config

`~/.codex/config.toml`:
```toml
model = "qwen/qwen3-coder"
model_provider = "openrouter"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OVERFLOW_API_KEY"
```
