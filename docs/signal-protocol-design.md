# Signal Protocol: Tool Use + File Fallback

## Current State

Agents communicate signals (done, question, checkpoint) via text markers in stdout:
- `@@DONE@@`, `@@QUESTION@@`, `@@WAITING@@`, `@@CHECKPOINT@@`, `@@LOG@@`
- Parsed by `core/protocols.py:parse_marker()` via string matching
- Each adapter maps native events to these markers (e.g., Codex `turn.completed` → `@@DONE@@`)

Problems:
- Text markers can appear inside code blocks, strings, or explanations → false positives
- Weaker models don't reliably emit markers in the right format
- String matching is fragile — whitespace, casing, partial matches

## Experiment Results (2026-04-05)

Tested MCP tool registration with all three agents using a minimal MCP server exposing `nightshift_done`, `nightshift_checkpoint`, and `nightshift_question` tools.

### MCP Server

Minimal Python stdio server implementing MCP protocol (JSON-RPC 2.0):
- `tools/list` → returns three nightshift signal tools
- `tools/call` → logs the signal and returns acknowledgment

Config (`mcp-config.json`):
```json
{
  "mcpServers": {
    "nightshift-signals": {
      "command": "python3",
      "args": ["/path/to/nightshift-mcp-server.py"]
    }
  }
}
```

### Results

| Agent | MCP Registration | Model Called Tool? | Succeeded? | Blocker |
|---|---|---|---|---|
| **Claude Code** | `--mcp-config config.json` | **Yes** | **Yes** | None |
| **Codex** | `codex mcp add name -- cmd args` | **Yes** | **Yes** | None |
| **OpenHands** | `openhands mcp add name --transport stdio -- cmd args` | **Yes** (10 attempts) | **No** | OpenHands injects `security_risk` field into MCP tool validation |

**Important:** First OpenHands test used wrong litellm model name (`qwen/qwen3-coder` instead of `openrouter/qwen/qwen3-coder`), causing `LLMBadRequestError`. With correct model name, qwen3-coder reliably calls both native OpenHands tools AND MCP tools.

### Claude Code (SUCCESS)

```bash
claude --mcp-config /tmp/mcp-config.json --output-format stream-json --verbose \
  --dangerously-skip-permissions --max-turns 3 \
  -p "Create hello.py. When done, call nightshift_done tool."
```

Output events:
```
TOOL_USE: Write input={"file_path": "hello.py", "content": "print(\"hello\")\n"}
TOOL_USE: ToolSearch input={"query": "select:mcp__nightshift-signals__nightshift_done"}
TOOL_USE: mcp__nightshift-signals__nightshift_done input={"summary": "Created hello.py"}
RESULT: success cost=$0.0644
```

Claude Code automatically discovered the MCP tool via `ToolSearch` (deferred tool resolution), then called it. The tool name in stream-json is prefixed: `mcp__<server-name>__<tool-name>`.

### Codex (SUCCESS)

```bash
codex mcp add nightshift-signals -- python3 /tmp/nightshift-mcp-server.py
codex exec --json --full-auto "Create test.py. When done, call nightshift_done tool."
```

Output events:
```json
{"type": "item.started", "item": {"id": "item_4", "type": "mcp_tool_call", "server": "nightshift-signals", "tool": "nightshift_done", "arguments": {"summary": "Created test.py..."}}}
{"type": "item.completed", "item": {"id": "item_4", "type": "mcp_tool_call", ...}}
```

Codex has a dedicated `mcp_tool_call` item type. Fields: `server` (MCP server name), `tool` (tool name), `arguments` (tool input). Called the tool multiple times (model kept retrying because it expected the conversation to end after calling it).

### OpenHands (MODEL CALLS TOOL, FRAMEWORK BLOCKS IT)

```bash
openhands mcp add nightshift-signals --transport stdio -- python3 /tmp/nightshift-mcp-server.py
# NOTE: litellm requires provider prefix: openrouter/qwen/qwen3-coder, not qwen/qwen3-coder
export LLM_MODEL="openrouter/qwen/qwen3-coder"
openhands --headless --json --always-approve --override-with-envs \
  -t "Create test2.py. When done, call nightshift_done tool."
```

qwen3-coder **correctly called** `nightshift_done` with `{"summary": "Created test2.py..."}`. But OpenHands wraps MCP tools in Pydantic models (`MCPNightshiftDoneAction`) that inject a mandatory `security_risk` field not in our schema. Every call failed validation:

```
Error validating args {"summary": "Created test2.py...", "security_risk": "LOW"} 
for tool 'nightshift_done': 1 validation error for MCPNightshiftDoneAction
summary  Field required [type=missing, input_value={}, input_type=dict]
```

The model tried 10+ times with various arg combinations, all rejected by OpenHands' validator. Finally fell back to `FinishAction`.

**Tested fix:** Adding `security_risk` to our MCP schema does NOT help. OpenHands strips all args from the tool call to extract `security_risk` at the framework level, then passes an empty `{}` to the MCP tool's Pydantic validator. The tool's own arguments (`summary`) are lost in the splitting process. This is an OpenHands bug in MCP argument handling.

**Resolution:** Skip MCP for OpenHands. Their native actions (`FinishAction`, `FileEditorAction`, `TerminalAction`) already map cleanly to nightshift signals and work reliably with qwen3-coder. MCP adds nothing for OpenHands that native actions don't already provide.

**Options if needed later:**
1. File upstream bug at github.com/OpenHands/software-agent-sdk — MCP arg splitting drops tool args
2. Wait for OpenHands MCP maturity (support is experimental)

## Proposed: Two-Layer Signal Protocol

### Layer 1: MCP tools (primary, for capable models)

Register nightshift signal tools via MCP with each agent:
- `nightshift_done(summary: str)` → replaces @@DONE@@
- `nightshift_checkpoint(description: str)` → replaces @@CHECKPOINT@@
- `nightshift_question(question: str)` → replaces @@QUESTION@@ + @@WAITING@@

**Claude Code:** `--mcp-config` flag. Tool events appear as `tool_use` with `mcp__nightshift-signals__<tool>` name.
```python
# ClaudeCodeAgent._parse()
if part["type"] == "tool_use" and part["name"].startswith("mcp__nightshift-signals__"):
    tool = part["name"].split("__")[-1]
    return self._handle_signal_tool(tool, part["input"])
```

**Codex:** `codex mcp add`. Tool events appear as `item.type: "mcp_tool_call"`.
```python
# CodexAgent._parse_item()
if item_type == "mcp_tool_call" and item.get("server") == "nightshift-signals":
    tool = item.get("tool", "")
    return self._handle_signal_tool(tool, item.get("arguments", {}))
```

**OpenHands:** MCP blocked (see Experiment 1). Use file signals instead (see Layer 2).

### Layer 2: File signals (fallback, primary for OpenHands)

For agents/models that can't call custom tools, write signal files:
```
/session/signal/done          — agent completed work
/session/signal/question.json — {"question": "...", "context": "..."}
/session/signal/checkpoint    — description text
```

Detection: inotify or polling in SessionRunner. File signals override stdout if both present.

Already partially implemented:
- `waiting.json` → question signal
- `answer.txt` → answer delivery
- `state.json` → state persistence

#### File Signal Experiment Results (2026-04-06)

Tested with OpenHands + qwen3-coder. Prompt: "Create hello.py. When done, write `/tmp/signal/done` with a summary."

| Environment | hello.py | Signal file written | Content |
|---|---|---|---|
| Host (no patches) | Created | **Yes** | "Created hello.py that prints hello world" |
| Docker (with patches) | Created | **Yes** | "Created hello.py that prints hello world" |

OpenHands writes signal files via `FileEditorAction` (create command) or `TerminalAction` (`echo > file`). Both work reliably with qwen3-coder.

**Conclusion:** File signals are the recommended signal mechanism for OpenHands. MCP is blocked by their arg-stripping bug, but file signals work on both host and Docker (with the condenser+output-token patches for Docker — see `docs/openhands-docker-investigation.md`).

### Layer 3: Text markers (tertiary fallback, current system)

Keep existing `@@MARKER@@` parsing as last resort. This is what we use today and what adapter event-type mapping provides (Codex `turn.completed` → `@@DONE@@`, etc.).

### Architecture

```
                    Claude Code         Codex              OpenHands          Weak model
                    ───────────         ─────              ─────────          ──────────
Layer 1 (MCP):      mcp__*__done        mcp_tool_call      MCP ActionEvent    (not reliable)
                    tool_use event      item event          (model-dependent)

Layer 2 (files):    ─────────────────── file signals ──────────────────────────────────────
                    /session/signal/done, /session/signal/question.json, etc.

Layer 3 (text):     ─────────────────── @@MARKER@@ parsing (current, fallback) ───────────
                    adapter maps native events → markers

Data plane:         ─────────────────── stdout parsing (unchanged) ────────────────────────
                    conversation logging, tool calls, tool results
```

### MCP Server Deployment

The MCP server needs to run inside the Docker container alongside the agent. Options:

1. **Bundled script:** Ship `nightshift-mcp-server.py` in the Docker image. Each adapter passes the appropriate MCP config flag when launching the agent.

2. **Config generation:** `docker-entrypoint.sh` generates MCP config at startup (like we do for `~/.codex/config.toml`). Points to the bundled server script.

3. **Per-agent registration:**
   - Claude Code: `--mcp-config /opt/nightshift/mcp-config.json`
   - Codex: `codex mcp add` in entrypoint (persisted in `~/.codex/`)
   - OpenHands: `openhands mcp add` in entrypoint (persisted in config)

### Adapter Changes

Each adapter's `_parse()` checks for MCP signal tools first, falls through to existing parsing:

```python
# In _parse(), before existing event handling:
signal = self._check_mcp_signal(event)
if signal:
    return signal  # AgentEvent with type TEXT content "@@DONE@@" etc.
```

### SessionRunner Changes

```python
# Check file signals each iteration (cheap — stat() call)
def _check_file_signals(self):
    signal_dir = Path("/session/signal")
    if (signal_dir / "done").exists():
        (signal_dir / "done").unlink()
        return "DONE"
    if (signal_dir / "question.json").exists():
        q = json.loads((signal_dir / "question.json").read_text())
        (signal_dir / "question.json").unlink()
        return ("QUESTION", q["question"])
    return None
```

## Benefits

1. **Eliminates false positive markers** — tool calls are structured, not text
2. **Cross-agent consistency** — each adapter translates MCP signals the same way
3. **Richer signals** — MCP tools carry structured arguments (summary, description, question with context)
4. **Weaker models have fallback** — file signals require only `echo > file`, text markers still work
5. **Backward compatible** — all three layers coexist, checked in priority order

## Implementation Phases

### Phase 1: MCP server + Claude Code integration
- Ship `nightshift-mcp-server.py` in Docker image
- Add `--mcp-config` to Claude Code agent `start()` command
- Parse `mcp__nightshift-signals__*` tool_use events in `_parse()`
- Keep text markers as fallback
- Tests: verify MCP tool call produces same AgentEvent as text marker

### Phase 2: Codex MCP integration
- Register MCP server via `codex mcp add` in entrypoint
- Parse `mcp_tool_call` items in `_parse()`
- Tests: verify `mcp_tool_call` with nightshift-signals produces correct AgentEvent

### Phase 3: File signal fallback
- Add `/session/signal/` directory to container
- Add `_check_file_signals()` to SessionRunner
- Update WORKFLOW.md prompt to instruct agents about file signals
- Tests: file signal produces same result as MCP/text marker

### Phase 4: OpenHands — file signals (confirmed working)
- MCP is broken in OpenHands (arg splitting bug drops tool arguments)
- **File signals work**: tested on host and Docker (2026-04-06)
- OpenHands writes `/session/signal/done` via `FileEditorAction` or `TerminalAction`
- Docker requires two monkey-patches: NoOpCondenser + max_output_tokens cap (see `docs/openhands-docker-investigation.md`)
- Implementation: include file signal instructions in OpenHands prompt, check signal dir in SessionRunner

### Phase 5: Deprecate text markers
- Once all agents use MCP or file fallback
- Remove `@@MARKER@@` instructions from prompts
- Keep `parse_marker()` for backward compatibility with old sessions
