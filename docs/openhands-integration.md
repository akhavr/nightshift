# OpenHands Integration

## Current Status: IN PROGRESS

OpenHands (formerly OpenDevin) is an MIT-licensed AI coding agent platform. Unlike Claude Code and Cursor CLI, it requires **no vendor subscription** — you bring your own LLM API key. It uses litellm under the hood, so it works with any LLM provider including OpenRouter, Anthropic, OpenAI, Ollama, Bedrock, etc.

## Why OpenHands for overflow

| Feature | OpenHands | Claude Code |
|---------|-----------|-------------|
| Subscription required | No (MIT, self-hosted) | Yes (Anthropic account) |
| OpenRouter support | Native (via litellm) | Requires litellm proxy |
| Custom LLM endpoint | `LLM_BASE_URL` env var | `ANTHROPIC_BASE_URL` env var |
| Model name validation | None (litellm accepts anything) | Client-side validation |
| License | MIT | Proprietary |

## Configuration

### WORKFLOW.md

```yaml
agent:
  kind: openhands
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []

overflow:
  env:
    # OpenHands uses LLM_* env vars (litellm under the hood)
    LLM_API_KEY: $OVERFLOW_API_KEY
    LLM_MODEL: $OVERFLOW_MODEL
    LLM_BASE_URL: $OVERFLOW_BASE_URL
    # Claude Code uses ANTHROPIC_* env vars
    ANTHROPIC_BASE_URL: $OVERFLOW_BASE_URL
    ANTHROPIC_AUTH_TOKEN: $OVERFLOW_API_KEY
    ANTHROPIC_API_KEY: $OVERFLOW_API_KEY
    ANTHROPIC_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_SMALL_FAST_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_SONNET_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_OPUS_MODEL: $OVERFLOW_MODEL
    ANTHROPIC_DEFAULT_HAIKU_MODEL: $OVERFLOW_MODEL
```

### .env

```bash
OVERFLOW_MODEL=openrouter/minimax/minimax-m2.7
OVERFLOW_BASE_URL=https://openrouter.ai/api/v1
OVERFLOW_API_KEY=sk-or-v1-...
```

## CLI Interface

```bash
openhands --headless -t "task description"          # headless, text output
openhands --headless --json -t "task description"   # headless, JSON output
openhands --headless --always-approve -t "task"     # auto-approve all actions
openhands --resume <id>                              # resume conversation
openhands --resume --last                            # resume most recent
```

**Important:** `--override-with-envs` flag is REQUIRED for LLM_* env vars to work.

## Env Vars

| Var | Purpose | Agent |
|-----|---------|-------|
| `LLM_API_KEY` | API key for LLM provider | OpenHands |
| `LLM_MODEL` | Model name (litellm format) | OpenHands |
| `LLM_BASE_URL` | Custom API base URL | OpenHands |
| `ANTHROPIC_API_KEY` | API key for Anthropic | Claude Code |
| `ANTHROPIC_BASE_URL` | Custom API base URL | Claude Code |
| `ANTHROPIC_MODEL` | Model name | Claude Code |

## JSON Event Format

Events are separated by `--JSON Event--` lines. Each event is a JSON object.

### Event Types

**MessageEvent** (user input):
```json
{
  "kind": "MessageEvent",
  "source": "user",
  "llm_message": {"content": [{"text": "task", "type": "text"}], "role": "user"}
}
```

**ActionEvent** (agent action):
```json
{
  "kind": "ActionEvent",
  "source": "agent",
  "action": {"kind": "FileEditorAction", "command": "create", "path": "...", "file_text": "..."},
  "reasoning_content": "...",
  "summary": "...",
  "tool_name": "file_editor"
}
```

Action kinds: `FileEditorAction`, `TerminalAction`, `FinishAction`

**ObservationEvent** (tool result):
```json
{
  "kind": "ObservationEvent",
  "source": "environment",
  "observation": {"kind": "FileEditorObservation", "content": [...], "is_error": false}
}
```

### Marker Mapping

| OpenHands Event | Nightshift Marker |
|---|---|
| `ActionEvent` + `FileEditorAction` (create/edit) | `@@CHECKPOINT@@` (from `summary`) |
| `ActionEvent` + `FinishAction` | `@@DONE@@` |
| `reasoning_content` field | `@@LOG@@` |
| `ActionEvent` + `TerminalAction` | tool_call |
| `ObservationEvent` | tool_result |
| Process exit non-zero | PROCESS_EXIT |
| No output for stall_timeout_s | STALL |

## Implementation Status

### Completed

- [x] OpenHandsAgent adapter (`adapters/agents/openhands.py`)
- [x] Registry entry in `core/config/factories.py`
- [x] Marker translation
- [x] Session ID extraction for resume
- [x] Docker support (LLM_* env vars passthrough)
- [x] Removed litellm proxy (agents use env vars directly)

### In Progress

- [ ] DRY refactor: extract `HeadlessAgentBase` from ClaudeCodeAgent + OpenHandsAgent
- [ ] Fix reasoning_content shadowing FinishAction bug
- [ ] CLAUDE.md update

### Pending

- [ ] Auth failure detection
- [ ] Full integration testing with real overflow

## Architecture

### Adapter Pattern

Both agents share similar infrastructure:

```
ClaudeCodeAgent          OpenHandsAgent
      |                        |
      +---- HeadlessAgentBase (to be extracted)
                    |
            subprocess.Popen
            select.select
            stream parsing
```

### Env Var Routing

```
WORKFLOW.md (overflow.env)
    |
    +-- LLM_* vars  -->  OpenHands (litellm)
    |
    +-- ANTHROPIC_* vars --> Claude Code
```

## Files Changed

- `adapters/agents/openhands.py` - OpenHands agent adapter
- `core/config/factories.py` - Registry entry
- `host/docker_cmd.py` - LLM_* env vars passthrough
- `docker-entrypoint.sh` - Removed litellm proxy startup
- `WORKFLOW.md` - Agent config + overflow env vars

## Testing

```bash
# Test OpenHands directly
LLM_API_KEY=$OVERFLOW_API_KEY LLM_MODEL=$OVERFLOW_MODEL LLM_BASE_URL=$OVERFLOW_BASE_URL \
  openhands --headless --json --override-with-envs -t "Create hello.py"

# Run tests
.venv/bin/python -m pytest tests/test_openhands_agent.py -v
```