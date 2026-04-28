# Vast.ai Agent Testing Framework

Reproducible scripts for testing coding agents against any model deployed on Vast.ai via vLLM.

## Quick Start

```bash
# 1. Find and create instance
vastai search offers 'gpu_ram>=80 num_gpus=1 disk_space>=150' --order 'dph_total'
vastai create instance <OFFER_ID> --image vllm/vllm-openai:latest --disk 150

# 2. Wait for instance, get connection info
vastai show instances --raw | jq '.[] | {id, status, ssh_host, ssh_port}'

# 3. SSH in and start vLLM (see deploy-vllm.sh)
vastai ssh <INSTANCE_ID>

# 4. In another terminal, create SSH tunnel
ssh -L 8000:127.0.0.1:8000 -p <PORT> root@<HOST>

# 5. Run tests locally against tunneled endpoint
./test-direct-api.sh
./test-opencode.sh
./test-openhands.sh

# 6. Cleanup
vastai destroy instance <INSTANCE_ID>
```

## Contents

| File | Purpose |
|------|---------|
| `deploy-vllm.sh` | Run on Vast.ai instance to start vLLM server |
| `benchmark-context.py` | Context length latency benchmarks |
| `test-direct-api.sh` | Direct API coding quality test |
| `test-opencode.sh` | OpenCode agent compatibility test |
| `test-openhands.sh` | OpenHands agent compatibility test |
| `task.py` | Test task: TTLLRUCache with unit tests |
| `opencode-config.json` | OpenCode custom provider template |

## Customizing for Different Models

Edit `deploy-vllm.sh`:
```bash
MODEL="Qwen/Qwen2.5-Coder-32B-Instruct"  # Change model
SERVED_NAME="qwen25-coder-32b"            # Change served name
MAX_MODEL_LEN=32768                       # Adjust context limit
```

Then update model name in test scripts accordingly.

## GPU Requirements

| Model Size | Min VRAM | Recommended GPU |
|------------|----------|-----------------|
| 7B FP16 | 16GB | RTX 4090, A5000 |
| 7B FP8 | 10GB | RTX 3090 |
| 27B FP8 | 32GB | A100 40GB |
| 32B FP8 | 40GB | A100 40GB |
| 70B FP8 | 80GB | A100 80GB, H100 |

## Known Agent Compatibility Issues

### Codex CLI
Requires OpenAI Responses API (`/v1/responses`) and `developer` message role. vLLM only supports Chat Completions API. **Incompatible with all vLLM models.**

### Reasoning Models (Qwen3.x, DeepSeek-R1)
Output tool calls as text instead of executing them. Causes:
- OpenCode: Infinite reasoning loops
- OpenHands: Tool calls rendered as XML text

**Use non-reasoning models for agentic workflows** (Qwen2.5-Coder, CodeLlama, DeepSeek-Coder).
