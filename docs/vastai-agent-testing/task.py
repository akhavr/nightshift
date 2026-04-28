"""
Test task: Thread-safe LRU Cache with TTL

A non-trivial coding task requiring:
- O(1) get/put using OrderedDict
- TTL expiration
- Thread safety with locks
- LRU eviction policy

Used to evaluate model coding quality.
"""

import threading
import time
from collections import OrderedDict


class TTLLRUCache:
    """
    Thread-safe LRU cache with per-item TTL support.

    Implement this class with:
    - __init__(capacity: int, default_ttl_seconds: float)
    - put(key, value, ttl_seconds=None) -> None
    - get(key) -> value or None
    """

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


# === TESTS ===

def test_basic_get_put():
    cache = TTLLRUCache(capacity=3, default_ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") is None
    print("✓ test_basic_get_put passed")


def test_lru_eviction():
    cache = TTLLRUCache(capacity=2, default_ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)  # Should evict "a"
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    print("✓ test_lru_eviction passed")


def test_ttl_expiration():
    cache = TTLLRUCache(capacity=3, default_ttl_seconds=0.1)
    cache.put("a", 1)
    assert cache.get("a") == 1
    time.sleep(0.15)
    assert cache.get("a") is None
    print("✓ test_ttl_expiration passed")


def test_custom_ttl():
    cache = TTLLRUCache(capacity=3, default_ttl_seconds=60)
    cache.put("a", 1, ttl_seconds=0.1)
    cache.put("b", 2)  # Uses default TTL
    time.sleep(0.15)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    print("✓ test_custom_ttl passed")


def test_thread_safety():
    cache = TTLLRUCache(capacity=100, default_ttl_seconds=60)
    errors = []

    def writer(thread_id):
        try:
            for i in range(100):
                cache.put(f"key_{thread_id}_{i}", i)
        except Exception as e:
            errors.append(e)

    def reader(thread_id):
        try:
            for i in range(100):
                cache.get(f"key_{thread_id}_{i}")
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=writer, args=(i,)))
        threads.append(threading.Thread(target=reader, args=(i,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread safety errors: {errors}"
    print("✓ test_thread_safety passed")


def test_lru_access_updates_order():
    cache = TTLLRUCache(capacity=2, default_ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # Access "a", making "b" the LRU
    cache.put("c", 3)  # Should evict "b", not "a"
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3
    print("✓ test_lru_access_updates_order passed")


if __name__ == "__main__":
    test_basic_get_put()
    test_lru_eviction()
    test_ttl_expiration()
    test_custom_ttl()
    test_thread_safety()
    test_lru_access_updates_order()
    print("\n✅ All tests passed!")
