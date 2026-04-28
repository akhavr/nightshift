#!/bin/bash
# Test coding quality via direct API call
# Requires: SSH tunnel to vLLM (ssh -L 8000:127.0.0.1:8000 ...)

set -e

BASE_URL="${BASE_URL:-http://127.0.0.1:8000/v1}"
MODEL="${MODEL:-qwen36-27b-fp8}"

TASK=$(cat <<'EOF'
Implement a thread-safe LRU cache with TTL support in Python.

Requirements:
- Class: TTLLRUCache
- Constructor: __init__(capacity: int, default_ttl_seconds: float)
- Methods:
  - put(key, value, ttl_seconds=None) -> None
  - get(key) -> value or None (returns None if expired or missing)
- O(1) operations using OrderedDict
- Thread-safe with threading.Lock
- LRU eviction when capacity exceeded
- Per-item TTL override support

Return only the implementation code, no explanation.
EOF
)

echo "Testing direct API coding quality..."
echo "Endpoint: $BASE_URL"
echo "Model: $MODEL"
echo ""

RESPONSE=$(curl -s "$BASE_URL/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [{\"role\": \"user\", \"content\": $(echo "$TASK" | jq -Rs .)}],
    \"temperature\": 0.7,
    \"max_tokens\": 2048
  }")

# Extract content
CONTENT=$(echo "$RESPONSE" | jq -r '.choices[0].message.content // empty')

if [[ -z "$CONTENT" ]]; then
    echo "❌ Error: No response from API"
    echo "$RESPONSE" | jq .
    exit 1
fi

echo "=== Model Response ==="
echo "$CONTENT"
echo ""
echo "=== Extracting Code ==="

# Extract Python code from response (handles markdown code blocks)
CODE=$(echo "$CONTENT" | sed -n '/```python/,/```/p' | sed '1d;$d')
if [[ -z "$CODE" ]]; then
    # Try without markdown
    CODE="$CONTENT"
fi

# Save and test
TMPDIR=$(mktemp -d)
echo "$CODE" > "$TMPDIR/generated.py"

# Copy test file with implementation replaced
cat > "$TMPDIR/test_cache.py" << 'TESTEOF'
import sys
sys.path.insert(0, '.')
from generated import TTLLRUCache
import threading
import time

def test_basic():
    cache = TTLLRUCache(3, 60)
    cache.put("a", 1)
    assert cache.get("a") == 1
    print("✓ basic get/put")

def test_lru():
    cache = TTLLRUCache(2, 60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    print("✓ LRU eviction")

def test_ttl():
    cache = TTLLRUCache(3, 0.1)
    cache.put("a", 1)
    time.sleep(0.15)
    assert cache.get("a") is None
    print("✓ TTL expiration")

def test_threads():
    cache = TTLLRUCache(100, 60)
    def work():
        for i in range(50):
            cache.put(f"k{i}", i)
            cache.get(f"k{i}")
    threads = [threading.Thread(target=work) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    print("✓ thread safety")

if __name__ == "__main__":
    test_basic()
    test_lru()
    test_ttl()
    test_threads()
    print("\n✅ All tests passed!")
TESTEOF

echo "Running tests..."
cd "$TMPDIR"
if python3 test_cache.py; then
    echo ""
    echo "✅ Direct API test: PASSED"
else
    echo ""
    echo "❌ Direct API test: FAILED"
    exit 1
fi

rm -rf "$TMPDIR"
