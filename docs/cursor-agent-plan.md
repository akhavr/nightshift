# Plan: Add Cursor AI CLI Agent Adapter (with OpenRouter/M2.7)

## Context

Nightshift currently supports Claude Code as its primary coding agent. We want to add Cursor AI's CLI (`agent` command) as an alternative agent adapter, configured to use MiniMax M2.7 via OpenRouter. This bypasses Claude Code's model name validation restrictions entirely — Cursor CLI accepts arbitrary model names and API endpoints natively.

**Key advantage over overflow approach:** No litellm proxy needed, no model name rewriting, no OAuth credential conflicts. Cursor CLI is designed to work with any OpenAI-compatible API.

**Key challenge:** Cursor CLI is beta/evolving. The exact NDJSON output format, resume mechanism, and auth flow need runtime discovery.

## Requirement

### REQ-029: Cursor AI agent adapter

The system supports Cursor AI's CLI (`agent` command) as an alternative coding agent. Configuration via `agent.kind: cursor` in WORKFLOW.md. The adapter handles Cursor-specific output parsing, auth credentials, and Docker setup. The adapter implements the CodingAgent protocol and integrates with the existing session lifecycle (markers, checkpoints, resume, stall detection).

Default configuration routes through OpenRouter to MiniMax M2.7:
```yaml
agent:
  kind: cursor
  extra_args: []

# .env
CURSOR_API_KEY=sk-or-v1-...           # OpenRouter API key
CURSOR_BASE_URL=https://openrouter.ai/api/v1  # OpenRouter endpoint
CURSOR_MODEL=minimax/minimax-m2.7     # Model name
```

- **Tests:** test_cursor_agent.py, test_config_factories.py (updated)
- **Status:** planned

---

## Configuration Design

### WORKFLOW.md
```yaml
agent:
  kind: cursor
  max_turns: 50
  stall_timeout_s: 300
  extra_args: []
```

### .env
```bash
# Cursor agent via OpenRouter -> MiniMax M2.7
CURSOR_API_KEY=sk-or-v1-...
CURSOR_BASE_URL=https://openrouter.ai/api/v1
CURSOR_MODEL=minimax/minimax-m2.7
```

### Docker passthrough
`host/docker_cmd.py` passes `CURSOR_API_KEY`, `CURSOR_BASE_URL`, `CURSOR_MODEL` to the container.

### How Cursor CLI uses these
Cursor CLI likely reads:
- `OPENAI_API_KEY` or `CURSOR_API_KEY` for auth
- `OPENAI_BASE_URL` or similar for endpoint
- `--model` flag or env var for model selection

**Discovery needed:** Run `agent --help` to find exact env var names and flags. May need to map our `CURSOR_*` env vars to whatever Cursor expects (e.g., `OPENAI_API_KEY=$CURSOR_API_KEY`).

---

## Issues (implementation order)

### Issue 1: CursorAgent adapter skeleton + registry + tests

**Scope:** Create `adapters/agents/cursor.py` with `CursorAgent` class implementing the `CodingAgent` protocol. Register as `"cursor"` in `AGENT_REGISTRY`. All methods functional with basic output parsing.

**Command construction:**
```python
cmd = [
    self.command,  # "agent"
    "--headless",
    "chat",
    "--format", "json",
    "--no-stream",  # or streaming depending on discovery
]
if self._model:
    cmd += ["--model", self._model]
cmd += [*self.extra_args, "-p", prompt]
```

The agent reads `CURSOR_MODEL` from env to set `self._model`. If `CURSOR_BASE_URL` needs to be mapped to `OPENAI_BASE_URL`, the adapter sets it in the subprocess env.

**TDD approach:**
1. Write `tests/test_cursor_agent.py` with tests for:
   - Constructor accepts `command`, `stall_timeout_s`, `extra_args`, reads model from env
   - `start()` builds correct command with `--headless chat --format json`
   - `start()` includes `--model` when `CURSOR_MODEL` is set
   - `start()` maps `CURSOR_BASE_URL` → `OPENAI_BASE_URL` in subprocess env
   - `start()` maps `CURSOR_API_KEY` → `OPENAI_API_KEY` in subprocess env
   - `stream_events()` yields AgentEvent from mock subprocess stdout
   - `stream_events()` detects stalls (no output for stall_timeout_s)
   - `stream_events()` yields PROCESS_EXIT on process end
   - `terminate()` kills process
   - `send_input()` raises RuntimeError (fire-and-forget mode)
   - `is_alive()` returns correct bool
   - `pid` property returns process PID or None
   - `_parse()` handles JSON lines -> AgentEvent mapping
   - `_parse()` falls back to TEXT for non-JSON lines
   - Marker synthesis: `@@DONE@@` emitted on clean exit (exit code 0)
2. Update `test_config_factories.py` to verify `create_agent()` with `kind: cursor`
3. Implement `CursorAgent` to pass all tests

**Files:**
- `tests/test_cursor_agent.py` (new)
- `adapters/agents/cursor.py` (new)
- `core/config/factories.py` (add registry entry)
- `tests/test_config_factories.py` (update)

---

### Issue 2: Cursor marker translation + tests

**Scope:** Cursor CLI doesn't emit nightshift markers (`@@DONE@@`, `@@CHECKPOINT@@`, etc.). The adapter must detect Cursor's native completion/progress signals and synthesize equivalent markers.

**Marker mapping strategy:**
- **@@DONE@@** — synthesized on clean exit (exit code 0), or when Cursor outputs a completion signal
- **@@CHECKPOINT@@** — synthesized when Cursor reports a file write/edit (if detectable from NDJSON)
- **@@LOG@@** — map Cursor's thinking/reasoning output
- **@@QUESTION@@** / **@@WAITING@@** — if Cursor has a Q&A mechanism in headless mode
- **PROCESS_EXIT** — on process termination
- **STALL** — on stall_timeout_s with no output (same as ClaudeCodeAgent)

**TDD approach:**
1. Add tests for marker synthesis in `test_cursor_agent.py`:
   - Clean exit → @@DONE@@ marker emitted
   - File edit event → @@CHECKPOINT@@ marker emitted
   - Reasoning text → @@LOG@@ marker emitted
   - Error exit → no @@DONE@@, PROCESS_EXIT emitted
2. Implement marker synthesis in `_parse()` and `stream_events()`

**Files:**
- `tests/test_cursor_agent.py` (update)
- `adapters/agents/cursor.py` (update)

---

### Issue 3: Cursor auth failure detection + tests

**Scope:** Add Cursor-specific auth failure patterns.

**Patterns to detect:**
- Invalid API key (OpenRouter returns 401)
- Subscription expired
- Rate limited (429)
- Model not found (OpenRouter returns 404 for invalid model names)

**TDD approach:**
1. Add tests to `test_cursor_agent.py`:
   - `_is_auth_failure()` detects auth patterns
   - `_parse()` returns AUTH_FAILURE event for auth errors
2. Implement in `CursorAgent`

**Files:**
- `tests/test_cursor_agent.py` (update)
- `adapters/agents/cursor.py` (update)

---

### Issue 4: Docker support for Cursor agent

**Scope:** Enable running Cursor agent inside the Nightshift Docker container.

**Changes:**
- `Dockerfile`: Install Cursor CLI (`curl https://cursor.com/install -fsSL | bash`), add `$HOME/.local/bin` to PATH
- `docker-entrypoint.sh`: Add block to copy Cursor credentials from `/cursor-auth` to writable `$HOME/.cursor/` (same pattern as Claude credentials)
- `host/docker_cmd.py`:
  - Add `CURSOR_API_KEY`, `CURSOR_BASE_URL`, `CURSOR_MODEL` to `_PASSTHROUGH_ENV_VARS`
  - Add `_cursor_auth_mounts()` to mount `~/.cursor` as `/cursor-auth:ro` (if needed)
  - Agent-specific mount selection: only mount Claude auth for claude-code, Cursor auth for cursor

**Testing:** Docker build succeeds; `agent --version` works inside container.

**Files:**
- `Dockerfile` (update)
- `docker-entrypoint.sh` (update)
- `host/docker_cmd.py` (update)

---

### Issue 5: Output format refinement (post-discovery)

**Scope:** After running Cursor CLI against real tasks, refine the parser with actual output samples.

**TDD approach:**
1. Capture real Cursor NDJSON output from several tasks
2. Save as test fixtures in `tests/fixtures/cursor_output/`
3. Add test cases using real fixtures
4. Refine `_parse()` for actual event types, field names, tool_use structure
5. Handle Cursor-specific edge cases (context limits, model errors)

**Files:**
- `tests/test_cursor_agent.py` (update with real fixtures)
- `tests/fixtures/cursor_output/` (new)
- `adapters/agents/cursor.py` (refine parser)

---

### Issue 6: Resume/session support + tests

**Scope:** Implement conversation resume if Cursor CLI supports it.

**Discovery needed:** Run `agent --headless chat "hello" --format json` and examine output for session/thread identifiers. Check `agent --help` for `--resume` or `--session` flags.

**Fallback:** If no native resume, nightshift's resume-prompt mechanism works — checkpoint history + recent conversation are included in the resume prompt, giving the new Cursor session sufficient context.

**Files:**
- `tests/test_cursor_agent.py` (update)
- `adapters/agents/cursor.py` (update)

---

### Issue 7: Requirements + docs update

**Scope:** Add REQ-029 to `docs/requirements.md`, update traceability matrix, update CLAUDE.md.

**Files:**
- `docs/requirements.md` (add REQ-029 + matrix entries)
- `CLAUDE.md` (mention CursorAgent in adapter list, add CURSOR_* to env var docs)

---

## Dependency graph

```
Issue 1 (skeleton + registry)
  |-- Issue 2 (marker translation)
  |-- Issue 3 (auth failure detection)
  +-- Issue 4 (Docker support)
        +-- Issue 5 (output refinement — needs real CLI)
        +-- Issue 6 (resume — needs real CLI)

Issue 7 (docs) — can be done first or last
```

Issues 2, 3, 4 are independent and can run in parallel after Issue 1.
Issues 5, 6 require Issue 4 (working Docker setup) to run Cursor for real.

## Discovery Checklist (before or during Issue 1)

Run on the host (with Cursor installed):
```bash
# Check CLI basics
agent --help
agent --version

# Check env var support
OPENAI_API_KEY=sk-or-v1-... OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
  agent --headless chat --model minimax/minimax-m2.7 --format json -p "say hi"

# Check output format
agent --headless chat --format json -p "say hello" 2>&1 | head -50

# Check if --no-stream vs streaming matters
agent --headless chat --format json --no-stream -p "say hello"

# Check resume support
agent --help | grep -i resume
agent --help | grep -i session
```

Save output samples to `docs/cursor-discovery/` for reference.
