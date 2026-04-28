# Qwen3.6-27B-FP8 on Vast.ai: Deployment & Benchmark Results

**Date:** 2026-04-28

## Overview

Transient deployment of Qwen3.6-27B-FP8 on Vast.ai for performance testing and coding quality evaluation.

## Infrastructure

| Component | Value |
|-----------|-------|
| GPU | A100 SXM4 80GB |
| Instance cost | ~$0.85/hr |
| Image | `vllm/vllm-openai:latest` |
| Model | `Qwen/Qwen3.6-27B-FP8` |
| Max context | 262,144 tokens |
| VRAM used | ~75.5GB |

## vLLM Configuration

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.6-27B-FP8 \
  --served-model-name qwen36-27b-fp8 \
  --host 0.0.0.0 \
  --port 8000 \
  --max-model-len 262144 \
  --gpu-memory-utilization 0.95 \
  --kv-cache-dtype fp8 \
  --trust-remote-code \
  --download-dir /workspace/hf-cache
```

**For tool calling** (OpenCode, OpenHands), add:
```bash
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --default-chat-template-kwargs '{"enable_thinking": false}'
```

**Critical:** Use `qwen3_coder` parser (NOT hermes) — Qwen3 outputs XML-format tool calls.

## Context Length Benchmarks

| Context Size | Latency | Status |
|--------------|---------|--------|
| 8,192 | 14.05s | ✅ |
| 32,768 | 57.73s | ✅ |
| 65,536 | 162.93s | ✅ |
| 131,072 | 259.25s | ✅ |
| ~252,000 (52K repeats) | 345.47s | ✅ |
| 262,144 | - | ❌ (hits limit) |

**Max usable context:** ~252K tokens (leaves room for output tokens)

**Throughput:** ~25,000 tokens/s prompt processing at high context

## Coding Quality Evaluation

### Task: Thread-safe LRU Cache with TTL

A non-trivial data structure requiring:
- O(1) get/put using OrderedDict
- TTL expiration
- Thread safety with locks
- LRU eviction policy

### Result: ✅ All tests passed

```python
# Generated code (extracted from reasoning output)
import threading
import time
from collections import OrderedDict

class TTLLRUCache:
    def __init__(self, capacity: int, default_ttl_seconds: float):
        self.capacity = capacity
        self.default_ttl_seconds = default_ttl_seconds
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def put(self, key, value, ttl_seconds=None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expiration = time.time() + ttl
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            elif len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)
            self.cache[key] = (value, expiration)

    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            value, expiration = self.cache[key]
            if time.time() > expiration:
                del self.cache[key]
                return None
            self.cache.move_to_end(key)
            return value
```

Tests passed:
- Basic get/put
- LRU eviction
- TTL expiration
- Custom TTL per item
- Thread safety (10 concurrent threads)

## Agent Compatibility

### Codex CLI
**Status:** ❌ Incompatible

Codex uses OpenAI's `/v1/responses` API (not `/v1/chat/completions`) and the `developer` message role. vLLM doesn't support these.

### OpenCode
**Status:** ✅ Compatible (with correct vLLM flags)

**vLLM configuration (critical):**
```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.6-27B-FP8 \
  --served-model-name qwen36-27b-fp8 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  ...
```

Key flags:
- `--tool-call-parser qwen3_coder` (NOT hermes — Qwen3 uses XML format)
- `--default-chat-template-kwargs '{"enable_thinking": false}'` (disable reasoning output)

Configure `~/.config/opencode/config.json`:

```json
{
  "provider": {
    "vastai": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Vast.ai vLLM",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1"
      },
      "models": {
        "qwen36-27b-fp8": {
          "name": "Qwen3.6 27B FP8",
          "limit": { "context": 262144, "output": 4096 }
        }
      }
    }
  }
}
```

Then use: `opencode run -m "vastai/qwen36-27b-fp8" "prompt"`

**Requires:** SSH tunnel to vast.ai instance (`ssh -L 8000:127.0.0.1:8000 -p PORT root@HOST`)

### OpenHands
**Status:** ✅ Compatible (with correct vLLM flags)

**vLLM must be configured with:**
- `--tool-call-parser qwen3_coder` (NOT hermes)
- `--default-chat-template-kwargs '{"enable_thinking": false}'`

```bash
LLM_API_KEY=dummy \
LLM_BASE_URL=http://127.0.0.1:8000/v1 \
LLM_MODEL=openai/qwen36-27b-fp8 \
openhands --headless --json --override-with-envs -t "prompt"
```

**Test results:** TTLLRUCache implementation completed in **1m15s**, 6 agent messages, all tests passed

## Model Behavior Notes

1. **Reasoning output:** Qwen3.6 is a reasoning model — outputs extensive thinking before the answer. Code must be extracted from the response.

2. **Tokenization:** "x = 1\n" ≈ 4.85 tokens (not 2-3 as might be expected). Account for this when estimating context usage.

3. **Tool calling:** Requires `--enable-auto-tool-choice --tool-call-parser qwen3_coder` flags. The `qwen3_coder` parser handles Qwen3's XML tool call format; `hermes` parser doesn't work.

## Known vLLM Bug: Reasoning Breaks Tool Parsing

**Issue:** [vllm-project/vllm#19513](https://github.com/vllm-project/vllm/issues/19513) (closed as "not planned")

When `--enable-reasoning --reasoning-parser deepseek_r1` is combined with `--tool-call-parser hermes`, tool calls appear as **plain text** instead of structured `tool_calls`:

```
# Expected: response.choices[0].message.tool_calls = [...]
# Actual: response.choices[0].message.content contains:
#   <tool_call>{"name": "file_editor", ...}</tool_call>
```

With `tool_choice: "required"`, returns HTTP 400:
```
Invalid JSON: expected value at line 1 column 1 
[type=json_invalid, input_value='<think>\nOkay, the user...
```

**Workarounds:**
1. **Disable reasoning** — omit `--enable-reasoning` and `--reasoning-parser` flags
2. **Use non-reasoning model** — Qwen2.5-Coder-32B works with hermes parser
3. **Disable thinking per-request** — `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`
4. **Direct API only** — skip agents, extract code from reasoning output manually

## Deployment Sequence

See `~/Downloads/qwen_vast_guide.md` for full step-by-step:

1. `vastai search offers 'gpu_ram>=80 num_gpus=1 disk_space>=150' --order 'dph_total'`
2. `vastai create instance <ID> --image vllm/vllm-openai:latest --disk 150`
3. Wait for instance, SSH in
4. Start vLLM server
5. Wait for model download (~30GB, 5-10 min)
6. Run benchmarks/tests
7. `vastai destroy instance <ID>` when done

## Cost Summary

- Instance: ~$0.85/hr (A100 80GB)
- Model download: one-time ~10 min
- Benchmark run: ~20 min total
- Estimated total: ~$0.50 for full evaluation

## Conclusions

Qwen3.6-27B-FP8 on Vast.ai A100:
- **Performance:** Good latency up to 131K context, acceptable at 252K
- **Quality:** Passed complex coding task with correct, idiomatic code (via direct API)
- **Cost:** Very affordable (~$0.85/hr vs cloud API costs)
- **Caveats:**
  - Reasoning model outputs verbose thinking — disable with `--default-chat-template-kwargs '{"enable_thinking": false}'`
  - **Codex incompatible** (uses /v1/responses API, not chat completions)
  - **OpenCode & OpenHands work** with `--tool-call-parser qwen3_coder` (NOT hermes)
  - The hermes parser doesn't recognize Qwen3's XML tool call format
