#!/bin/bash
# Deploy vLLM on Vast.ai instance
# Run this after SSH'ing into the instance

set -e

# === CONFIGURATION ===
MODEL="${MODEL:-Qwen/Qwen3.6-27B-FP8}"
SERVED_NAME="${SERVED_NAME:-qwen36-27b-fp8}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"
GPU_UTIL="${GPU_UTIL:-0.95}"
PORT="${PORT:-8000}"

# Tool calling (optional - set ENABLE_TOOLS=1)
# Use qwen3_coder for Qwen3.x models (XML format), hermes for others
TOOL_PARSER="${TOOL_PARSER:-qwen3_coder}"
TOOL_ARGS=""
if [[ "${ENABLE_TOOLS:-0}" == "1" ]]; then
    TOOL_ARGS="--enable-auto-tool-choice --tool-call-parser $TOOL_PARSER"
fi

# Disable thinking for Qwen3 reasoning models (set DISABLE_THINKING=1)
CHAT_TEMPLATE_ARGS=""
if [[ "${DISABLE_THINKING:-0}" == "1" ]]; then
    CHAT_TEMPLATE_ARGS="--default-chat-template-kwargs '{\"enable_thinking\": false}'"
fi

# === SETUP ===
mkdir -p /workspace/hf-cache

# Optional: set HF token for gated models
if [[ -n "$HF_TOKEN" ]]; then
    export HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"
fi

echo "Starting vLLM server..."
echo "  Model: $MODEL"
echo "  Served as: $SERVED_NAME"
echo "  Max context: $MAX_MODEL_LEN"
echo "  Tool calling: ${ENABLE_TOOLS:-disabled}"

# === START SERVER ===
python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --served-model-name "$SERVED_NAME" \
    --host 0.0.0.0 \
    --port "$PORT" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --kv-cache-dtype fp8 \
    --trust-remote-code \
    --download-dir /workspace/hf-cache \
    $TOOL_ARGS
