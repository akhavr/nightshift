# Codex Agent Plan

## Current State

- Stub at `adapters/agents/codex.py` — all methods raise `NotImplementedError`
- Registered as `"codex"` in `AGENT_REGISTRY` at `core/config/factories.py`
- No tests, no Docker support, no env var passthrough

## Codex CLI Interface

OpenAI Codex is a Rust-based coding agent. Two interfaces exist:

### 1. `codex exec` (headless, preferred)

```bash
codex exec --json --always-approve [--resume <conversation_id>] -t <prompt>
```

- Fire-and-forget, non-interactive
- JSON event output on stdout, separated by `--JSON Event--` lines
- `--resume <conversation_id>` for multi-turn (session ID extracted from stderr)
- `--max-turns <N>` to limit conversation turns
- `--override-with-envs` for env-based configuration
- Requires `OPENAI_API_KEY` env var
- Requires ChatGPT Plus or higher tier

### 2. `codex app-server` (JSON-RPC over stdio)

More complex protocol:
- JSON-RPC 2.0 over stdio (newline-delimited)
- Mandatory handshake: `initialize` request -> response -> `initialized` notification
- Primitives: Thread (session), Turn (prompt round), Item (atomic I/O unit)
- Methods: `thread/start`, `turn/start`, `turn/interrupt`, etc.
- Schema introspection: `codex app-server generate-json-schema`

**Decision: Start with `exec` mode.** It matches our fire-and-forget pattern (same as Claude Code and OpenHands). App-server can be explored later if `exec` is insufficient.

## Event Format (exec mode)

Based on documentation, events are JSON objects with a `kind` field. Expected mapping:

| Codex Event | Nightshift AgentEvent |
|---|---|
| ActionEvent + FinishAction | TEXT with `@@DONE@@` |
| ActionEvent + FileEditorAction | TOOL_CALL (+ CHECKPOINT on create/edit) |
| ActionEvent + TerminalAction | TOOL_CALL |
| ObservationEvent | TOOL_RESULT |
| ObservationEvent + is_error + auth pattern | AUTH_FAILURE |
| MessageEvent | TEXT |
| reasoning_content field | TEXT (log) |

## Implementation Plan (3 issues)

### Issue 1: CodexAgent adapter skeleton
- Inherit `HeadlessAgentBase`
- `start()`: spawn `codex exec --json --always-approve [-t prompt] [--resume id]`
- `_parse()`: JSON event parser dispatching on `kind` field
- `_on_process_exit()`: extract conversation ID from stderr
- `AUTH_FAILURE_PATTERNS`: OpenAI-specific ("invalid api key", "error code: 401/429", "rate limit", "insufficient_quota")
- Tests: follow `test_openhands_agent.py` structure

### Issue 2: Docker + env var support
- `npm install -g @openai/codex` in Dockerfile
- Add `OPENAI_API_KEY` to `_PASSTHROUGH_ENV_VARS` in `host/docker_cmd.py`
- No litellm/LLM_* mapping needed (Codex uses OpenAI API directly)

### Issue 3: docs/requirements.md + CLAUDE.md update
- Add REQ-031 for Codex adapter
- Update architecture section in CLAUDE.md

## Multi-Agent Architecture Context

Nightshift supports different agent kinds per pipeline step:
- **Coder step** reads WORKFLOW.md → `agent.kind` (e.g., `openhands`)
- **Review step** reads REVIEW.md → `agent.kind` (e.g., `claude-code`)
- **Overflow mode** can override the coder agent kind via `overflow.agent_kind`
- Overflow does NOT apply to review sessions (issue c2c6c84)

Codex could serve as:
1. **Coder agent** — `agent.kind: codex` in WORKFLOW.md (direct OpenAI, no litellm)
2. **Review agent** — `agent.kind: codex` in REVIEW.md (alternative to Claude Code)
3. **Overflow coder** — `overflow.agent_kind: codex` (switch to Codex when Claude quota runs out)

Unlike OpenHands (which uses litellm and works with any provider), Codex is locked to OpenAI models. This makes it less flexible for overflow but potentially stronger for code review (GPT-5.4).

## Discovery Checklist

Before filing implementation issues, run this checklist to resolve open questions:

```bash
# 1. Install Codex CLI
npm install -g @openai/codex

# 2. Verify version and help
codex --version
codex exec --help

# 3. Capture actual JSON event output
OPENAI_API_KEY=sk-... codex exec --json --always-approve \
  -t "create a file hello.py that prints hello world" \
  2>/tmp/codex-stderr.log | tee /tmp/codex-events.json

# 4. Inspect event structure
cat /tmp/codex-events.json | python3 -m json.tool

# 5. Check stderr for session ID
cat /tmp/codex-stderr.log

# 6. Test resume
CONVERSATION_ID=$(grep -oP 'Conversation ID:\s*\K\S+' /tmp/codex-stderr.log)
codex exec --json --always-approve --resume "$CONVERSATION_ID" \
  -t "add a test for hello.py" 2>&1 | tee /tmp/codex-resume.json

# 7. Check workspace handling
cd /tmp/test-workspace && codex exec --json --always-approve \
  -t "list files in current directory" 2>&1

# 8. Check model override
codex exec --help | grep -i model
```

## Open Questions

### Q1: Exact JSON event structure
The documentation describes the event format at a high level but we haven't verified the actual output of `codex exec --json`. Need to run:
```bash
OPENAI_API_KEY=sk-... codex exec --json --always-approve -t "print hello world" 2>/tmp/codex-stderr.log | tee /tmp/codex-events.json
```
and capture the exact event schema. Key unknowns:
- Are event `kind` values identical to OpenHands (ActionEvent, ObservationEvent, etc.) or different?
- What does the `--JSON Event--` separator look like exactly?
- What fields are present on each event type?

### Q2: Session ID extraction
Where exactly does the conversation ID appear? Stderr? Stdout? A final JSON event? Need to capture stderr from a real run to write the regex.

### Q3: Subscription requirement
Codex requires ChatGPT Plus or higher. This means:
- Cannot run in CI without a paid account
- Cannot test in Docker builds without credentials
- Is this a blocker for overflow mode usage? (OpenHands + litellm has no subscription gate)

### Q4: Model selection
- Default model is GPT-5.4 (latest as of May 2025)
- Can the model be overridden via env var or CLI flag?
- If not, Codex is locked to OpenAI models (unlike OpenHands which supports any litellm provider)

### Q5: Workspace handling
- Does `codex exec` respect `cwd` for file operations?
- Does it need an explicit `--workspace` flag like OpenHands?
- How does it handle git operations inside Docker?

### Q6: Rate limits and cost
- What are the rate limits for headless exec mode?
- Is there per-turn or per-token billing?
- How does cost compare to Claude Code / OpenHands + M2.7 for typical nightshift tasks?

### Q7: app-server mode value
- Is `exec` mode sufficient for all nightshift use cases?
- Does app-server mode offer benefits (streaming, better control, less overhead)?
- If we need app-server later, it's a significant protocol change (JSON-RPC handshake, thread/turn primitives)

### Q8: Codex vs OpenHands event format overlap
The event mapping table above assumes Codex uses the same event `kind` names as OpenHands (ActionEvent, ObservationEvent, etc.). This is unverified — Codex may use entirely different event types. If the format is similar enough, we could share parsing logic via a mixin or base class. If completely different, a standalone parser is needed.

### Blocking vs Non-blocking

**Must resolve before implementation:**
- Q1 (event structure) — can't write `_parse()` without it
- Q2 (session ID) — can't write `_on_process_exit()` without it
- Q5 (workspace handling) — affects `start()` command construction

**Can resolve during implementation:**
- Q3 (subscription) — document the requirement, don't block on it
- Q4 (model selection) — start with defaults, add override later
- Q8 (format overlap) — implement standalone, refactor if similar

**Can defer:**
- Q6 (cost) — operational concern, not implementation blocker
- Q7 (app-server) — future enhancement

## Future Enhancements

### Exponential backoff on provider overload

**Problem observed:** When the LLM provider is overloaded (e.g., OpenRouter "high demand"), Codex CLI retries 5 times internally with no backoff, then the turn fails. Nightshift auto-resumes immediately, Codex hits the same overloaded provider, and the cycle repeats until `max_resumes` is exhausted. No backoff at either level.

**Desired behavior:** When consecutive resumes fail with provider overload (not auth failure), nightshift should apply exponential backoff between resume attempts (e.g., 30s, 60s, 120s, 240s). This applies to all agent kinds, not just Codex.

**Detection:** The adapter needs to distinguish overload from auth failure. For Codex: "high demand" and "Reconnecting..." errors are overload; "401 Unauthorized" is auth. A new `AgentEventType.PROVIDER_OVERLOAD` or a flag on the existing SYSTEM event could signal this to `SessionRunner`.

**Scope:** Affects `core/session.py` (resume delay logic), adapter `_parse()` methods (overload detection), and potentially `host/constants.py` (backoff parameters). Cross-agent feature — Claude Code and OpenHands can also hit provider overload.

## Dependencies

- OpenAI Codex CLI (`npm install -g @openai/codex`)
- `OPENAI_API_KEY` with ChatGPT Plus or higher
- Node.js (already in Dockerfile)

## References

- [OpenAI Codex CLI docs](https://developers.openai.com/codex/cli)
- [OpenAI Codex app-server protocol](https://developers.openai.com/codex/app-server)
- [Codex GitHub repo](https://github.com/openai/codex)
