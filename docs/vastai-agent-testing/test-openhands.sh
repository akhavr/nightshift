#!/bin/bash
# Test OpenHands agent compatibility
# Requires:
#   - SSH tunnel to vLLM (ssh -L 8000:127.0.0.1:8000 ...)
#   - OpenHands installed (pip install openhands-ai)

set -e

BASE_URL="${BASE_URL:-http://127.0.0.1:8000/v1}"
MODEL="${MODEL:-qwen36-27b-fp8}"
TIMEOUT="${TIMEOUT:-300}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR=$(mktemp -d)

echo "Testing OpenHands agent..."
echo "Base URL: $BASE_URL"
echo "Model: openai/$MODEL"
echo "Workdir: $WORKDIR"
echo ""

# Copy task file (stub version)
cat > "$WORKDIR/task.py" << 'EOF'
"""
Thread-safe LRU Cache with TTL support.
Implement the TTLLRUCache class below.
"""

import threading
import time
from collections import OrderedDict


class TTLLRUCache:
    """
    Thread-safe LRU cache with per-item TTL support.

    Args:
        capacity: Maximum number of items
        default_ttl_seconds: Default TTL for items
    """

    def __init__(self, capacity: int, default_ttl_seconds: float):
        # TODO: Implement
        pass

    def put(self, key, value, ttl_seconds=None) -> None:
        """Add or update item. Evict LRU if at capacity."""
        # TODO: Implement
        pass

    def get(self, key):
        """Get item or None if missing/expired. Updates LRU order."""
        # TODO: Implement
        pass


# Tests - do not modify
def test_basic():
    cache = TTLLRUCache(3, 60)
    cache.put("a", 1)
    assert cache.get("a") == 1, "Basic get failed"
    print("✓ basic")

def test_lru():
    cache = TTLLRUCache(2, 60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None, "LRU eviction failed"
    print("✓ lru")

def test_ttl():
    cache = TTLLRUCache(3, 0.1)
    cache.put("a", 1)
    time.sleep(0.15)
    assert cache.get("a") is None, "TTL expiration failed"
    print("✓ ttl")

if __name__ == "__main__":
    test_basic()
    test_lru()
    test_ttl()
    print("✅ All tests passed!")
EOF

PROMPT="Implement the TTLLRUCache class in task.py. The class needs:
- Thread-safe operations using threading.Lock
- O(1) get/put using OrderedDict
- LRU eviction when capacity exceeded
- TTL expiration checked on get()
- Optional per-item TTL override

Run 'python task.py' to verify. Do not modify the tests."

cd "$WORKDIR"

echo "Running OpenHands..."
timeout "$TIMEOUT" env \
    LLM_API_KEY=dummy \
    LLM_BASE_URL="$BASE_URL" \
    LLM_MODEL="openai/$MODEL" \
    openhands --headless --json --override-with-envs \
    -t "$PROMPT" 2>&1 || true

echo ""
echo "=== Verifying Result ==="
if python3 task.py 2>/dev/null; then
    echo ""
    echo "✅ OpenHands test: PASSED"
else
    echo ""
    echo "❌ OpenHands test: FAILED (agent did not complete implementation)"
fi

echo ""
echo "Workdir preserved at: $WORKDIR"
