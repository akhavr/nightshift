# Cursor CLI Discovery (2026-04-01)

## Binary Info
- Location: `~/.local/bin/agent` → `~/.local/share/cursor-agent/versions/2026.03.30-a5d3e17/cursor-agent`
- Version: 2026.03.30-a5d3e17
- Architecture: Node.js bundle (JS chunks + native binaries)

## CLI Interface (very similar to Claude Code)

| Feature | Cursor CLI | Claude Code |
|---------|-----------|-------------|
| Non-interactive mode | `--print` | `-p` |
| Stream JSON output | `--output-format stream-json` | `--output-format stream-json` |
| Resume session | `--resume [chatId]` / `--continue` | `--resume` |
| Model selection | `--model <name>` | `--model <name>` |
| Skip permissions | `--force` / `--yolo` | `--dangerously-skip-permissions` |
| Trust workspace | `--trust` | N/A |
| Custom headers | `-H "Name: Value"` | N/A |
| Workspace dir | `--workspace <path>` | N/A (uses cwd) |

## Env Vars Found in Binary

### Auth & API
- `CURSOR_API_KEY` — primary API key (validates against Cursor backend)
- `CURSOR_AUTH_TOKEN` — alternative auth token
- `CURSOR_API_BASE_URL` — custom API base URL
- `CURSOR_API_ENDPOINT` — alternative endpoint var

### Other Notable
- `CURSOR_CONFIG_DIR` — config directory override
- `CURSOR_DATA_DIR` — data directory override
- `CURSOR_WORKTREES_ROOT` — worktree root (has native worktree support!)
- `CURSOR_RULES` — rules/instructions
- `CURSOR_SANDBOX` — sandbox control

## OpenRouter Integration: BLOCKED

**Cursor CLI validates the API key against its own backend before making any model API calls.**

Tested:
1. `CURSOR_API_KEY=<openrouter-key>` → "Invalid API key"
2. `CURSOR_AUTH_TOKEN=<openrouter-key>` → "Invalid authentication"
3. `CURSOR_API_BASE_URL=https://openrouter.ai/api/v1` without valid Cursor auth → Still rejects

**To use Cursor CLI with OpenRouter, you need:**
1. Valid Cursor subscription (login via `agent login`)
2. Then set `CURSOR_API_BASE_URL` to redirect API calls to OpenRouter
3. Use `--model minimax/minimax-m2.7` for model selection

Without a Cursor subscription, the CLI is unusable regardless of what API endpoint you point it at.

## Implications for Nightshift

### If user has Cursor subscription:
- Adapter is straightforward — nearly identical to ClaudeCodeAgent
- Same `--print --output-format stream-json` pattern
- Resume via `--resume <chatId>`
- `--force --trust` for non-interactive mode
- Mount Cursor auth creds (from `agent login`) into container

### If user doesn't have Cursor subscription:
- Cursor CLI cannot be used at all
- Need a different CLI tool that works with arbitrary OpenAI-compatible APIs
- Alternatives: aider, codex CLI, or a custom wrapper

### Recommended adapter command
```python
cmd = [
    "agent", "--print", "--force", "--trust",
    "--output-format", "stream-json",
    "--model", self._model,
    *self.extra_args,
    "-p", prompt,
]
```
