#!/bin/bash
# Test OpenCode agent compatibility
# Requires:
#   - SSH tunnel to vLLM (ssh -L 8000:127.0.0.1:8000 ...)
#   - OpenCode installed (npm install -g opencode-ai)
#   - Config in ~/.config/opencode/config.json (see opencode-config.json)

set -e

PROVIDER="${PROVIDER:-vastai}"
MODEL="${MODEL:-qwen36-27b-fp8}"
TIMEOUT="${TIMEOUT:-300}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR=$(mktemp -d)

echo "Testing OpenCode agent..."
echo "Provider: $PROVIDER"
echo "Model: $MODEL"
echo "Workdir: $WORKDIR"
echo ""

# Copy task file (stub version for agent to implement)
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

Run the tests with 'python task.py' to verify your implementation."

cd "$WORKDIR"

echo "Running OpenCode..."
timeout "$TIMEOUT" opencode run -m "${PROVIDER}/${MODEL}" "$PROMPT" 2>&1 || true

echo ""
echo "=== Verifying Result ==="
if python3 task.py 2>/dev/null; then
    echo ""
    echo "✅ OpenCode test: PASSED"
else
    echo ""
    echo "❌ OpenCode test: FAILED (agent did not complete implementation)"
fi

echo ""
echo "Workdir preserved at: $WORKDIR"
