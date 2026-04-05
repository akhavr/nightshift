# OpenHands Docker Investigation

**Date:** 2026-04-05
**OpenHands versions tested:** 1.13.1, 1.14.0
**Model:** openrouter/qwen/qwen3-coder via OpenRouter
**Docker base:** Ubuntu 24.04, nightshift:latest image

## Summary

OpenHands works on the host but fails in Docker containers. The LLM summarizing condenser crashes on startup before the agent processes any events.

## Symptoms

```
ERROR    Hard context reset summarization failed after multiple attempts.
         llm_summarizing_condenser.py:305
```

Appears BEFORE "Initializing agent" in stderr. The agent initializes, receives the MessageEvent, but never processes it — exits immediately with code 0 and no work done.

## Reproduction

```bash
# WORKS on host:
export LLM_API_KEY=sk-or-...
export LLM_MODEL=openrouter/qwen/qwen3-coder
export LLM_BASE_URL=https://openrouter.ai/api/v1
openhands --headless --json --always-approve --override-with-envs \
  -t "Create hello.py that prints hello world"
# Creates file, uses FileEditorAction, TerminalAction, FinishAction. Success.

# FAILS in Docker (same env vars):
docker run --rm --entrypoint /bin/bash \
  --memory=8g --memory-swap=24g \
  -e LLM_API_KEY=... -e LLM_MODEL=... -e LLM_BASE_URL=... \
  -e TTY_INTERACTIVE=1 \
  nightshift:latest -c '
cd /tmp && mkdir test && cd test && git init -q
openhands --headless --json --always-approve --override-with-envs \
  -t "Create hello.py that prints hello world"'
# Condensation error, no file created, immediate exit.
```

## What was tested

| Test | Result |
|---|---|
| OpenHands 1.14.0 in Docker | Fails — condensation error |
| OpenHands 1.13.1 in Docker | Fails — condensation error (same) |
| OpenHands 1.13.1 on host | **Works** — file created, all actions successful |
| Docker with `--memory=8g --memory-swap=24g` | Fails — not an OOM issue |
| Docker with `LLM_TIMEOUT=300` | Fails — not a timeout issue |
| Docker with `TTY_INTERACTIVE=1 TTY_COMPATIBLE=1` | Fails |

## Root Cause Analysis

OpenHands uses `LLMSummarizingCondenser` (in `openhands/sdk/context/condenser/llm_summarizing_condenser.py`) which makes an extra LLM API call to summarize the conversation context. This fires during startup — before any agent action.

In Docker:
1. `CondensationRequest` event is emitted immediately after MessageEvent
2. The condenser tries to call the LLM for summarization
3. The call fails (timeout or error) — exact cause unclear from logs
4. `NoCondensationAvailableException` is raised
5. "Hard context reset summarization failed after multiple attempts" logged
6. Agent exits without processing any events

On the host:
- Same condenser fires but succeeds
- Difference: host has `~/.openhands/` with cached session data, persistent state
- Docker starts fresh every time — no cache, no state

### Why the condenser call fails in Docker but not on host

Possible causes (not definitively confirmed):
1. **Missing persistent state:** OpenHands stores session state in `~/.openhands/`. On host this persists; in Docker it's empty. The condenser may rely on cached state.
2. **Network timing:** Docker bridge NAT adds latency. The condenser's internal timeout may be shorter than the agent's main LLM timeout.
3. **Condenser triggers on empty context:** In headless mode with a fresh start, the condenser fires on the first message when there's nothing to condense, causing `NoCondensationAvailableException`.
4. **OpenHands bug in headless+Docker path:** The initialization sequence may differ between host and container environments.

### Code path

```
openhands/tools/preset/default.py:
  get_default_condenser(llm) → LLMSummarizingCondenser(llm=llm, max_size=80, keep_first=4)

openhands/sdk/context/condenser/llm_summarizing_condenser.py:305
  → tries LLM call to summarize
  → fails → NoCondensationAvailableException
  → "Hard context reset summarization failed"
```

A `NoOpCondenser` exists at `openhands/sdk/context/condenser/no_op_condenser.py` but there is no env var or CLI flag to select it. The default preset hardcodes `LLMSummarizingCondenser`.

## Native Tool Usage (confirmed working on host)

When OpenHands works (host), qwen3-coder uses native tools reliably:

| OpenHands Action | Description | Nightshift Mapping |
|---|---|---|
| `FileEditorAction` (kind: create/edit) | Creates/edits files | TOOL_CALL → CHECKPOINT |
| `TerminalAction` | Runs shell commands | TOOL_CALL → TOOL_RESULT |
| `FinishAction` | Signals completion | TEXT with @@DONE@@ |
| `ThinkAction` | Internal reasoning | LOG |

These are NOT MCP tools — they're OpenHands built-in actions. qwen3-coder calls them correctly.

## MCP Tool Support (tested separately)

qwen3-coder **can call MCP tools** through OpenHands — the model sends correct tool call args. However, OpenHands' MCP argument validation has a bug: it strips tool arguments during `security_risk` extraction, passing an empty dict to the tool's Pydantic validator. See `docs/signal-protocol-design.md` for details.

## LiteLLM Model Naming

OpenHands uses litellm which requires provider-prefixed model names:
- **Correct:** `openrouter/qwen/qwen3-coder`
- **Wrong:** `qwen/qwen3-coder` → `LLMBadRequestError: LLM Provider NOT provided`

## Bug #2: max_output_tokens Overflow (discovered 2026-04-06)

After fixing the condenser, a second bug emerged: `LLMContextWindowExceedError`.

```
litellm.BadRequestError: OpenrouterException - This endpoint's maximum context length
is 262144 tokens. However, you requested about 274649 tokens (7405 of text input,
5144 of tool input, 262100 in the output).
```

litellm reports qwen3-coder's `max_output_tokens` as 262100 (nearly the full 262K context window). OpenHands uses this value directly as `max_completion_tokens` in the API request, leaving no room for input tokens.

**Root cause:** `LLM._init_model_info_and_caps()` sets `self.max_output_tokens` from litellm's model registry. For qwen3-coder on OpenRouter, litellm reports 262100. OpenHands passes this through `select_chat_options()` as `max_completion_tokens`. The `LLM_MAX_OUTPUT_TOKENS` env var is NOT respected by `--override-with-envs` (checked: the field is set on the Pydantic model but `_init_model_info_and_caps` overwrites it from litellm data).

## Working Workaround (tested 2026-04-06)

Two monkey-patches applied before launching OpenHands CLI:

```python
# 1. Replace LLMSummarizingCondenser with NoOpCondenser
import openhands.tools.preset.default as preset
from openhands.sdk.context.condenser.no_op_condenser import NoOpCondenser
def patched_condenser(llm):
    return NoOpCondenser()
preset.get_default_condenser = patched_condenser

# 2. Cap max_output_tokens to prevent context overflow
from openhands.sdk.llm.llm import LLM
_orig = LLM._init_model_info_and_caps
def _patched(self):
    _orig(self)
    if self.max_output_tokens and self.max_output_tokens > 16384:
        self.max_output_tokens = 16384
LLM._init_model_info_and_caps = _patched
```

### Test results with workaround

| Test | Environment | hello.py | File signal | Actions used |
|---|---|---|---|---|
| Host (no patches) | Host | Created | Written | FileEditorAction, TerminalAction, FinishAction |
| Docker (both patches) | Docker | **Created** | **Written** | FileEditorAction, TerminalAction, FinishAction |

Full event sequence in Docker:
```
MessageEvent → FileEditorAction → FileEditorObservation →
TerminalAction → TerminalObservation → FinishAction → FinishObservation
```

### File signal test

Prompt: "Create hello.py. When done, write `/tmp/signal/done` with a summary."

Both host and Docker: agent wrote the signal file with content "Created hello.py that prints hello world". This confirms **file signals work for OpenHands** — the agent can write arbitrary files as completion signals.

## Current Status

**OpenHands in Docker: works with monkey-patches.** Two patches needed:
1. `NoOpCondenser` — replaces the crashing `LLMSummarizingCondenser`
2. `max_output_tokens` cap (16384) — prevents context overflow from litellm's inflated value

**OpenHands on host: works without patches.**

**File signals: confirmed working** on both host and Docker. OpenHands can write signal files via `FileEditorAction` or `TerminalAction`.

**Dockerfile:** Pinned to `openhands==1.13.1`. Patches are version-dependent — need re-verification on OpenHands upgrades.

## Implementation for nightshift

To use OpenHands in Docker containers, `docker-entrypoint.sh` should launch OpenHands via a wrapper script that applies both patches:

```bash
# In docker-entrypoint.sh, when AGENT_KIND=openhands:
python3 /opt/nightshift/openhands-launcher.py "$@"
```

Where `openhands-launcher.py`:
```python
import openhands.tools.preset.default as preset
from openhands.sdk.context.condenser.no_op_condenser import NoOpCondenser
from openhands.sdk.llm.llm import LLM

# Patch 1: NoOp condenser
preset.get_default_condenser = lambda llm: NoOpCondenser()

# Patch 2: Cap output tokens
_orig = LLM._init_model_info_and_caps
def _patched(self):
    _orig(self)
    if self.max_output_tokens and self.max_output_tokens > 16384:
        self.max_output_tokens = 16384
LLM._init_model_info_and_caps = _patched

import sys
from openhands_cli.entrypoint import main
main()
```

## Files

- `docs/openhands-docker-investigation.md` — this file
- `docs/signal-protocol-design.md` — MCP experiment results
- `adapters/agents/openhands.py` — OpenHands adapter (works when OpenHands works)
- `Dockerfile` — `openhands==1.13.1` pinned
