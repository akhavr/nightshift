#!/usr/bin/env python3
"""
Context length benchmark for vLLM endpoints.

Tests increasing context sizes to find the maximum usable context
and measure latency at each level.

Usage:
    python benchmark-context.py [--base-url URL] [--model NAME]
"""

import argparse
import json
import time
import urllib.request

# Approximate tokens per line of "x = N\n" (empirically measured)
TOKENS_PER_LINE = 4.85


def make_prompt(target_tokens: int) -> str:
    """Generate a prompt with approximately target_tokens tokens."""
    num_lines = int(target_tokens / TOKENS_PER_LINE)
    lines = [f"x = {i}" for i in range(num_lines)]
    code_block = "\n".join(lines)
    return f"Count the lines in this code:\n```python\n{code_block}\n```\nHow many lines?"


def test_context(base_url: str, model: str, target_tokens: int, timeout: int = 600) -> dict:
    """Test a specific context size and return results."""
    prompt = make_prompt(target_tokens)

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
        "temperature": 0
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        elapsed = time.time() - start

        usage = result.get("usage", {})
        return {
            "target_tokens": target_tokens,
            "actual_tokens": usage.get("prompt_tokens", "?"),
            "latency_s": round(elapsed, 2),
            "status": "ok",
            "throughput": round(usage.get("prompt_tokens", 0) / elapsed, 0) if elapsed > 0 else 0
        }
    except Exception as e:
        return {
            "target_tokens": target_tokens,
            "latency_s": round(time.time() - start, 2),
            "status": "error",
            "error": str(e)[:100]
        }


def main():
    parser = argparse.ArgumentParser(description="Benchmark vLLM context lengths")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="qwen36-27b-fp8")
    parser.add_argument("--max-context", type=int, default=262144)
    args = parser.parse_args()

    # Test sizes: 8K, 32K, 64K, 128K, 192K, 252K, max
    sizes = [8192, 32768, 65536, 131072, 192000, 252000]
    if args.max_context not in sizes:
        sizes.append(args.max_context)
    sizes = sorted([s for s in sizes if s <= args.max_context])

    print(f"Benchmarking: {args.base_url}")
    print(f"Model: {args.model}")
    print(f"Max context: {args.max_context:,}")
    print()
    print(f"{'Context':>12} | {'Actual':>10} | {'Latency':>10} | {'Throughput':>12} | Status")
    print("-" * 65)

    for size in sizes:
        result = test_context(args.base_url, args.model, size)

        actual = result.get("actual_tokens", "?")
        latency = f"{result['latency_s']}s"
        throughput = f"{result.get('throughput', 0):,.0f} tok/s" if result["status"] == "ok" else "-"
        status = "✅" if result["status"] == "ok" else f"❌ {result.get('error', '')[:30]}"

        print(f"{size:>12,} | {str(actual):>10} | {latency:>10} | {throughput:>12} | {status}")

    print()
    print("Note: Actual tokens may differ from target due to tokenization variance.")
    print("Max usable context is typically ~252K to leave room for output tokens.")


if __name__ == "__main__":
    main()
