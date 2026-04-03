# Generic Agent Adapter Design

## Problem

Each new coding agent requires a custom adapter: `start()`, `_parse()`, auth patterns, Docker setup, config mount, entrypoint logic. At 3 agents (Claude Code, OpenHands, Codex) this is manageable. At 10-20+, the per-agent overhead becomes the bottleneck:

- **Dockerfile bloat** — each agent adds its own runtime (npm, pip, binary). Multi-GB images, version conflicts, slow builds.
- **Config proliferation** — each agent has unique auth (API keys, OAuth, config files). `docker_cmd.py` and `docker-entrypoint.sh` accumulate hardcoded blocks per agent.
- **Discovery cost** — each new agent needs manual event capture, format mapping, session ID extraction, resume testing. Human time per agent: ~2-4 hours.

## Observation

Most coding agents are converging on similar patterns:
- JSON/JSONL output mode (`--json`, `--output-format stream-json`)
- Fire-and-forget execution with resume support
- Session/conversation IDs
- Tool calls (commands, file edits) + results (output, exit codes)
- Auth errors in output stream

The differences are in field names and nesting, not in structure.

## Proposed Solution: GenericJsonAgent

A single adapter that reads event mapping from configuration instead of code.

### Config-driven event mapping

```yaml
agent:
  kind: generic
  command: "my-agent"
  args: ["--json", "--headless", "{prompt}"]
  resume_args: ["resume", "{session_id}", "{prompt}"]
  cwd: "{workspace}"
  events:
    # JSONPath-like selectors for event dispatch
    session_id: "$.thread_id"           # where to find session ID (first event)
    session_id_event: "$.type == 'thread.started'"
    done: "$.type == 'turn.completed'"
    text: "$.item.text"                 # when item.type == agent_message
    text_event: "$.item.type == 'agent_message'"
    tool_call: "$.item.command"         # when item.type == command_execution, in_progress
    tool_result: "$.item.aggregated_output"  # when status == completed
    error: "$.error.message"
    auth_patterns:
      - "status 401"
      - "unauthorized"
      - "invalid api key"
  usage:
    input_tokens: "$.usage.input_tokens"
    output_tokens: "$.usage.output_tokens"
```

### Implementation sketch

```python
class GenericJsonAgent(HeadlessAgentBase):
    def __init__(self, command, args_template, resume_template,
                 event_config, auth_patterns, **kwargs):
        super().__init__(command, **kwargs)
        self._args_template = args_template
        self._resume_template = resume_template
        self._event_config = event_config
        self.AUTH_FAILURE_PATTERNS = tuple(auth_patterns)

    def start(self, prompt, workspace, max_turns=50):
        template = self._resume_template if self._session_id else self._args_template
        cmd = [self.command] + [
            arg.format(prompt=prompt, session_id=self._session_id, workspace=workspace)
            for arg in template
        ]
        # ... standard Popen launch

    def _parse(self, raw):
        ev = json.loads(raw)
        # Walk event_config rules to dispatch
        # Return appropriate AgentEvent based on matched rule
```

### What this replaces

| Current | Generic |
|---|---|
| `adapters/agents/codex.py` (170 lines) | 10-line YAML config block |
| `adapters/agents/openhands.py` (160 lines) | 10-line YAML config block |
| Per-agent `_parse()` with hardcoded field names | Config-driven field selectors |
| Per-agent `AUTH_FAILURE_PATTERNS` tuple | `auth_patterns` list in config |
| Per-agent Docker/entrypoint blocks | Agent config includes `docker.install` and `docker.setup` commands |

### What this does NOT replace

- `ClaudeCodeAgent` — its stream-json format has complex nested content arrays (text, tool_use, thinking blocks in a single event). Too complex for simple field selectors.
- Agents with non-JSON output (e.g., plain text, XML).
- Agents requiring bidirectional communication (stdin/stdout interleaving).

### Docker scaling

Complement the generic adapter with per-agent Docker layers:

```yaml
agent:
  kind: generic
  docker:
    install: "npm install -g @openai/codex"
    setup: |
      mkdir -p "$HOME/.codex"
      cat > "$HOME/.codex/config.toml" << EOF
      model = "${OVERFLOW_MODEL}"
      EOF
    env: ["OVERFLOW_API_KEY", "OVERFLOW_MODEL"]
```

`docker-entrypoint.sh` reads registered agents and runs their setup blocks, instead of hardcoding each one.

## When to implement

Not now. The current pattern (one file per agent, `HeadlessAgentBase`, registry dict) works well up to ~10 agents. Implement when:
- A 4th or 5th agent is added and the pattern feels repetitive
- The Dockerfile exceeds 50 lines of agent-specific install commands
- `docker-entrypoint.sh` exceeds 100 lines of agent-specific config blocks

## Prerequisites

- Stable event mapping across 3+ agents to identify the common pattern
- JSONPath or similar selector library (or a simple homegrown matcher)
- Agent config schema validation (so misconfigurations fail fast)
