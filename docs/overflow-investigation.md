# Overflow Mode: Investigation Notes (2026-03-31, updated 2026-04-01)

## Goal

Route nightshift agent containers to an alternate LLM provider (e.g., MiniMax M2.7 via OpenRouter) when Claude usage is high.

## What Works

### Infrastructure (fully operational)
- **litellm proxy** starts inside the container, health-checks pass, all model endpoints healthy
- **overflow-proxy.py** (SSE rewriting proxy) correctly rewrites `model` field in `message_start` events from `minimax/minimax-m2.7` to `claude-sonnet-4-6`
- **`nightshift overflow on/off`** CLI works, flag file mechanism works, watcher detects it
- **Docker env injection** works — overflow env vars are passed to containers via `-e` flags
- **litellm Anthropic passthrough** (`/v1/messages`) works — confirmed streaming SSE responses via curl from inside the container
- **OpenRouter Anthropic-compatible API** works — `https://openrouter.ai/api/v1/messages` accepts both Anthropic model names and provider-specific names (e.g., `MiniMax-M2.7`)
- **Dockerfile `ARG CLAUDE_CODE_VERSION`** — enables pinning or upgrading Claude Code version with `--build-arg` or `--no-cache`

### Verified via curl (inside container)
```bash
curl -s -N -X POST http://localhost:4001/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: test" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":50,"stream":true,...}'
# Returns: "model": "claude-sonnet-4-6" (rewritten), streaming SSE, correct content
```

## The Blocker

**Claude Code CLI ignores `ANTHROPIC_BASE_URL` and always connects to Anthropic's API.**

Tested on both v2.1.73 and v2.1.87. TCP connection analysis confirms Claude Code connects to Anthropic IPs (160.79.104.10 or Cloudflare 104.18.x.x), never to localhost, regardless of `ANTHROPIC_BASE_URL` value.

### All attempts (v9-v18, both Claude Code versions)

| # | Attempt | Result |
|---|---------|--------|
| v9 | Remove `.credentials.json`, set `ANTHROPIC_API_KEY` env var | `apiKeySource: ANTHROPIC_API_KEY`, still connects externally |
| v10 | Same + `ANTHROPIC_BASE_URL` via Docker `-e` | Still connects externally |
| v11 | `ANTHROPIC_MODEL=MiniMax-M2.7` + all model env vars (MiniMax approach) | "model doesn't exist" — client-side name validation |
| v12 | `ANTHROPIC_AUTH_TOKEN` without `ANTHROPIC_API_KEY` | `apiKeySource: "none"` — not recognized |
| v13 | Both `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_API_KEY` used, connects externally |
| v14 | Only `ANTHROPIC_API_KEY`, no `AUTH_TOKEN` | Connects externally |
| v15 | Write `ANTHROPIC_BASE_URL` into `~/.claude/settings.json` env section | settings.json confirmed correct, still connects externally |
| v16 | Upgrade to Claude Code v2.1.87 | `api_retry` errors (progress!), but still not hitting proxy |
| v17 | Fix `export ANTHROPIC_BASE_URL` in entrypoint (was missing) | Confirmed in `/proc/<pid>/environ`, still connects externally |
| v18 | All correct: API_KEY + BASE_URL both in env, v2.1.87 | `api_retry err=unknown`, no connection to localhost |

### Key observations
- Claude Code emits `api_retry` with `error: unknown` and `error_status: null` on v2.1.87 (vs silent hang on v2.1.73)
- The env var IS present in the Claude process environ (`/proc/<pid>/environ` confirms it)
- No TCP connections to localhost:4001 are ever established — Claude Code never even tries
- External connections go to port 443 (HTTPS) — Claude Code connects to `api.anthropic.com` via TLS

### MiniMax documentation claims it works

https://platform.minimax.io/docs/token-plan/claude-code recommends:
```
ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic
ANTHROPIC_AUTH_TOKEN=<minimax-key>
ANTHROPIC_MODEL=MiniMax-M2.7
```
Possible explanations for the discrepancy:
- MiniMax endpoint is HTTPS (TLS) while our proxy is HTTP — Claude Code may require HTTPS for `ANTHROPIC_BASE_URL`
- MiniMax may work only in interactive mode, not `-p` (fire-and-forget) mode
- There may be a Claude Code config or flag we're missing

## Unexplored Paths (ordered by likelihood)

1. **HTTPS requirement** — Claude Code may reject non-HTTPS `ANTHROPIC_BASE_URL`. Test with a self-signed cert on the proxy, or tunnel via `socat` to add TLS.

2. **`ANTHROPIC_UNIX_SOCKET`** — found in Claude Code binary strings (`process.env.ANTHROPIC_UNIX_SOCKET`). Could route through a Unix socket to the local proxy, completely bypassing the BASE_URL issue.

3. **Interactive vs `-p` mode** — MiniMax docs may assume interactive mode. Test `ANTHROPIC_BASE_URL` with `claude` in interactive mode on the host first (not in Docker, not `-p`).

4. **iptables/network redirect** — redirect outgoing traffic to Anthropic's IPs to localhost:4001. Would work regardless of Claude Code's behavior. E.g.: `iptables -t nat -A OUTPUT -d 160.79.104.10 -p tcp --dport 443 -j REDIRECT --to-port 4001`

5. **`settings.local.json`** — untested, may have different precedence.

6. **Newer Claude Code version** — v2.1.88+ may fix this. The `api_retry` behavior on v2.1.87 suggests it IS trying to use the base URL but failing for another reason (possibly HTTP vs HTTPS).

## Architecture (ready to use once BASE_URL works)

```
Claude Code → overflow-proxy (:4001) → litellm (:4000) → OpenRouter → MiniMax M2.7
                  ↑ rewrites model name        ↑ remaps model
                  in SSE responses              claude-sonnet-4-6 → openrouter/minimax/minimax-m2.7
```

### Files
- `overflow-proxy.py` — threaded HTTP proxy, rewrites `model` in `message_start` SSE events
- `litellm-config.yaml` — model mapping config (maps all Claude model names to MiniMax via OpenRouter)
- `docker-entrypoint.sh` — starts litellm + overflow-proxy, writes settings.json, removes OAuth creds
- `host/docker_cmd.py` — mounts litellm config, injects overflow env vars
- `host/launch.py` — checks overflow flag file, passes overflow config
- `core/config/models.py` — `OverflowConfig` dataclass with `litellm_config` field

### Configuration
WORKFLOW.md:
```yaml
overflow:
  litellm_config: litellm-config.yaml
  env:
    OVERFLOW_API_KEY: $OVERFLOW_API_KEY
    ANTHROPIC_API_KEY: litellm-local
```

.env:
```
OVERFLOW_API_KEY=sk-or-v1-...
```

## Status

**Parked.** All infrastructure works. The sole blocker is Claude Code not connecting to the local proxy despite `ANTHROPIC_BASE_URL` being set correctly.

**Most promising next step:** Test with HTTPS (self-signed cert) or `ANTHROPIC_UNIX_SOCKET`.
