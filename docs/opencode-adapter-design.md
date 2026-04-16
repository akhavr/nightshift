# OpenCode Adapter Design

Research notes for adding `opencode` as an `agent.kind` option.

## CLI Overview

| Aspect | Details |
|--------|---------|
| **Binary** | `opencode` |
| **Headless mode** | `opencode run "prompt" --format json` or `opencode -p "prompt" -f json` |
| **Output** | Final response only (no streaming events) |
| **Resume** | `opencode run --session ses_XXX "continue"` or `--continue` for last session |
| **Auto-approve** | `--dangerously-skip-permissions` or `-p` flag |
| **Auth** | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. |

## Event Format (JSONL)

When using `--format json`, events are JSONL:

```json
{"type": "step_start", "sessionID": "ses_XXX", "timestamp": 1712345678000, ...}
{"type": "text", "sessionID": "ses_XXX", "part": {"text": "..."}}
{"type": "tool_use", "sessionID": "ses_XXX", "part": {"tool": "bash", "state": {"status": "completed", "output": "..."}}}
{"type": "step_finish", "sessionID": "ses_XXX", "part": {"reason": "stop", "tokens": {...}}}
{"type": "error", "sessionID": "ses_XXX", "error": {"name": "APIError", "data": {"message": "..."}}}
```

Key fields:
- `type`: Event type (`step_start`, `text`, `tool_use`, `step_finish`, `error`)
- `sessionID`: Format `ses_XXXXXXXXXXXXXXXXXXXX` (extract for resume)
- `part.reason`: In `step_finish`, `"stop"` means final, `"tool-calls"` means continuing
- `part.tokens`: Token usage in `step_finish` events

## MCP Support

OpenCode supports MCP via `.opencode.json` config file (not CLI flags):

```json
{
  "mcpServers": {
    "nightshift-signals": {
      "type": "stdio",
      "command": "python3",
      "args": ["/opt/nightshift/nightshift-mcp-server.py"]
    }
  }
}
```

**Limitation:** MCP tool calls are internal — they don't appear in JSON output. We cannot detect `nightshift_done` tool calls by parsing stdout.

## Signaling Strategy

Given MCP tool calls are not visible in output, use **file signals** (same as OpenHands):

| Signal | File | Content |
|--------|------|---------|
| Done | `/session/signal/done` | Summary text |
| Question | `/session/signal/question.json` | `{"question": "..."}` |
| Checkpoint | `/session/signal/checkpoint` | Description text |

Prompt instructs agent to write these files. SessionRunner polls `/session/signal/` directory.

**Fallback:** Parse `step_finish` with `reason: "stop"` as implicit `@@DONE@@`.

## Commands

```bash
# Start
opencode run --format json --dangerously-skip-permissions -m "anthropic/claude-sonnet-4-5" "prompt"

# Resume
opencode run --format json --dangerously-skip-permissions --session ses_XXX "continue"

# Continue last session
opencode run --format json --dangerously-skip-permissions --continue "continue"
```

## Implementation

### Adapter (~150 lines)

```python
# adapters/agents/opencode.py

class OpenCodeAgent(HeadlessAgentBase):
    AUTH_FAILURE_PATTERNS = (
        "unauthorized", "401", "invalid api key", "rate limit",
        "authentication_error", "insufficient_quota",
    )

    def __init__(
        self,
        command: str = "opencode",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
    ):
        super().__init__(command, stall_timeout_s, extra_args)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        cmd = [
            self.command, "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            *self.extra_args,
        ]
        if self._session_id:
            cmd += ["--session", self._session_id]
        cmd.append(prompt)

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse JSONL event from opencode."""
        stripped = raw.strip()
        if not stripped:
            return None

        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

        event_type = ev.get("type", "")

        # Extract session ID for resume
        if "sessionID" in ev and not self._session_id:
            self._session_id = ev["sessionID"]

        # Text content
        if event_type == "text":
            text = ev.get("part", {}).get("text", "")
            if text:
                return AgentEvent(type=AgentEventType.TEXT, content=text, raw=raw)

        # Tool use
        if event_type == "tool_use":
            part = ev.get("part", {})
            tool = part.get("tool", "?")
            state = part.get("state", {})
            status = state.get("status", "")
            output = str(state.get("output", ""))[:TOOL_RESULT_PREVIEW_LEN]
            return AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                content=f"[{tool}] {status}: {output}",
                raw=raw,
            )

        # Completion
        if event_type == "step_finish":
            part = ev.get("part", {})
            reason = part.get("reason", "")
            if reason == "stop":
                # Extract usage
                tokens = part.get("tokens", {})
                metadata = {}
                if tokens:
                    metadata["usage"] = {
                        "input_tokens": tokens.get("input", 0),
                        "output_tokens": tokens.get("output", 0),
                    }
                return AgentEvent(
                    type=AgentEventType.TEXT,
                    content="@@DONE@@",
                    raw=raw,
                    metadata=metadata,
                )

        # Error
        if event_type == "error":
            err = ev.get("error", {})
            msg = err.get("data", {}).get("message", str(err))
            if self._is_auth_failure(msg):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE, content=msg, raw=raw)
            return AgentEvent(type=AgentEventType.TEXT, content=f"Error: {msg}", raw=raw)

        return None
```

### Registry Entry

```python
# core/config/factories.py
AGENT_REGISTRY["opencode"] = ("adapters.agents.opencode", "OpenCodeAgent")
```

### Docker Entrypoint

```bash
# docker-entrypoint.sh
if [ "$AGENT_KIND" = "opencode" ]; then
    # Generate MCP config for file signals (if MCP becomes useful later)
    mkdir -p ~/.config/opencode
    cat > ~/.config/opencode/.opencode.json << 'EOF'
{
  "mcpServers": {
    "nightshift-signals": {
      "type": "stdio",
      "command": "python3",
      "args": ["/opt/nightshift/nightshift-mcp-server.py"]
    }
  }
}
EOF
fi
```

### Tests (~100 lines)

```
tests/test_opencode_agent.py
- test_start_builds_correct_command
- test_start_resume_uses_session_id
- test_parse_text_event
- test_parse_tool_use_event
- test_parse_step_finish_emits_done
- test_parse_error_event
- test_auth_failure_detection
- test_extracts_session_id
- test_extracts_usage_metadata
```

## Comparison with Other Agents

| Feature | OpenCode | Claude Code | Codex | OpenHands |
|---------|----------|-------------|-------|-----------|
| MCP tool visibility | ❌ Internal | ✅ stream-json | ✅ JSONL | ❌ Blocked |
| Signal mechanism | File signals | MCP tools | MCP tools | File signals |
| Resume support | ✅ --session | ✅ --resume | ✅ exec resume | ✅ --resume |
| Token usage | ✅ step_finish | ✅ result event | ✅ turn.completed | ❌ No |

## Model Testing Results (2026-04-14)

Tested OpenCode with OpenRouter models. Rankings from best to worst:

| Model | Role | Result | Notes |
|-------|------|--------|-------|
| `qwen/qwen3-235b-a22b-07-25` | Reviewer | **Works** | Made proper tool calls (git diff, pytest, file reads). Ran tests successfully. |
| `minimax/minimax-m2.5` | Coder | **Works** | Completed simple tasks (create file). Good for basic coding. |
| `deepseek/deepseek-chat-v3-0324` | Reviewer | **Poor** | Didn't follow review instructions. Started making code edits instead of reviewing and outputting verdict. |

### Known Issues

1. **Model name format**: OpenRouter requires exact model names. `qwen/qwen3-235b-a22b` fails; must use `qwen/qwen3-235b-a22b-07-25` (with date suffix).

2. **Review instruction following**: Some models (deepseek) don't follow the strict review prompt. They treat it as a coding task and make edits instead of outputting `@nightshift approve/revise`.

3. **Verdict detection**: Even capable models may not write the verdict in a format the system can parse before hitting max-turns.

### Recommendations

- **Coder**: minimax-m2.5 or qwen3-235b work well for coding tasks
- **Reviewer**: qwen3-235b-a22b-07-25 is best; avoid deepseek for strict review workflows
- **Model names**: Always check OpenRouter for exact model ID (including date suffixes)

### Configuration Examples

```yaml
# WORKFLOW.md - coder with minimax
agent:
  kind: opencode
  extra_args: ["-m", "openrouter/minimax/minimax-m2.5"]

# REVIEW.md - reviewer with qwen3
agent:
  kind: opencode
  extra_args: ["-m", "openrouter/qwen/qwen3-235b-a22b-07-25"]
```

## Implementation Phases

1. **Phase 1: Basic adapter** — Start/parse/resume, `step_finish` → `@@DONE@@`
2. **Phase 2: File signals** — Add file signal polling to SessionRunner (shared with OpenHands)
3. **Phase 3: Usage tracking** — Extract tokens from `step_finish.part.tokens`

## Future Work

### Charmbracelet Crush Support

[Crush](https://github.com/charmbracelet/crush) is a separate terminal-based AI coding agent from Charmbracelet. While it shares some historical roots with OpenCode, it's now an independent project with its own CLI and output format.

Adding `agent.kind: crush` would require:
- Research Crush's headless/JSON output mode (if any)
- Determine event format and session resume semantics
- Evaluate MCP tool visibility in output
- Implement `CrushAgent` adapter following the same pattern
