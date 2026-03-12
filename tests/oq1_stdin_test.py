#!/usr/bin/env python3
"""OQ-1: Does Claude Code read stdin in -p mode?

Run this OUTSIDE of a Claude Code session (unset CLAUDECODE env var).

    unset CLAUDECODE
    python tests/oq1_stdin_test.py

Tests three approaches:
  1. Basic -p with PTY stdin — does it read follow-up?
  2. --input-format stream-json — does streaming input work?
  3. Plain interactive mode (no -p) with PTY — does it accept piped prompts?

Results are printed to stdout and saved to tests/oq1_results.txt.
"""

import json
import os
import pty
import select
import subprocess
import sys
import time

TIMEOUT = 60  # seconds per test
FOLLOW_UP_TIMEOUT_S = 15
LOG_PREVIEW_LEN = 200
STDERR_PREVIEW_LEN = 500
LOG_LINE_LIMIT = 10
RESULTS = []


def log(msg):
    print(msg)
    RESULTS.append(msg)


def read_output(proc, master_fd, timeout_s=30):
    """Read stdout lines until process exits or timeout."""
    lines = []
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if proc.poll() is not None:
            for line in proc.stdout:
                lines.append(line.rstrip("\n"))
            break
        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if ready:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
    return lines


def cleanup(proc, master_fd):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass


def _start_claude(extra_args, slave_fd):
    """Start a claude process with common flags."""
    cmd = ["claude", "--dangerously-skip-permissions",
           "--output-format", "stream-json"] + extra_args
    proc = subprocess.Popen(
        cmd, stdin=slave_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    os.close(slave_fd)
    return proc


def _log_output(lines, prefix="stdout"):
    """Log first N lines of output."""
    for line in lines[:LOG_LINE_LIMIT]:
        log(f"  [{prefix}] {line[:LOG_PREVIEW_LEN]}")
    if len(lines) > LOG_LINE_LIMIT:
        log(f"  ... ({len(lines)} total lines)")


def _log_stderr(proc):
    """Read and log stderr if available."""
    if proc.stderr:
        try:
            stderr_out = proc.stderr.read()
        except Exception:
            stderr_out = ""
        if stderr_out:
            log(f"  [stderr] {stderr_out[:STDERR_PREVIEW_LEN]}")


def _send_follow_up(master_fd, message):
    """Write a follow-up message to the PTY. Returns False on failure."""
    try:
        os.write(master_fd, message)
        return True
    except OSError as e:
        log(f"  Write failed: {e}")
        return False


def test1_basic_p_mode():
    """Test 1: Does `claude -p` read follow-up input from PTY stdin?"""
    log("\n" + "=" * 60)
    log("TEST 1: claude -p with PTY stdin — follow-up input")
    log("=" * 60)

    master_fd, slave_fd = pty.openpty()
    proc = _start_claude(
        ["-p", "Say exactly the word HELLO and nothing else"], slave_fd)

    lines = read_output(proc, master_fd, timeout_s=TIMEOUT)
    _log_output(lines)
    _log_stderr(proc)

    if proc.poll() is not None:
        log(f"\n  Process exited with code {proc.poll()} after first response.")
        log("  RESULT: -p mode is fire-and-forget. Does NOT read stdin for follow-up.")
        cleanup(proc, master_fd)
        return False

    log("\n  Process still alive. Sending follow-up on PTY stdin...")
    if not _send_follow_up(master_fd,
                           b"Now say exactly the word WORLD and nothing else\n"):
        cleanup(proc, master_fd)
        return False

    follow_up_lines = read_output(proc, master_fd, timeout_s=FOLLOW_UP_TIMEOUT_S)
    _log_output(follow_up_lines, "follow-up")

    has_world = any("WORLD" in l.upper() for l in follow_up_lines)
    log(f"\n  Follow-up received response: {bool(follow_up_lines)}")
    log(f"  Response contains WORLD: {has_world}")
    log(f"  RESULT: {'stdin follow-up WORKS in -p mode' if has_world else 'stdin follow-up does NOT work'}")

    cleanup(proc, master_fd)
    return has_world


def test2_stream_json_input():
    """Test 2: Does --input-format stream-json allow multi-turn?"""
    log("\n" + "=" * 60)
    log("TEST 2: --input-format stream-json (streaming input)")
    log("=" * 60)

    master_fd, slave_fd = pty.openpty()
    first_msg = json.dumps({
        "type": "user",
        "content": "Say exactly the word HELLO and nothing else"
    }) + "\n"

    proc = _start_claude(
        ["--input-format", "stream-json", "-p", "-"], slave_fd)

    if not _send_follow_up(master_fd, first_msg.encode()):
        cleanup(proc, master_fd)
        return False

    lines = read_output(proc, master_fd, timeout_s=TIMEOUT)
    _log_output(lines)

    if proc.poll() is not None:
        log(f"  Process exited with code {proc.poll()}")
        _log_stderr(proc)
        log("  RESULT: stream-json input mode exited after first message.")
        cleanup(proc, master_fd)
        return False

    log("\n  Process alive. Sending stream-json follow-up...")
    second_msg = json.dumps({
        "type": "user",
        "content": "Now say exactly the word WORLD and nothing else"
    }) + "\n"
    if not _send_follow_up(master_fd, second_msg.encode()):
        cleanup(proc, master_fd)
        return False

    follow_up = read_output(proc, master_fd, timeout_s=FOLLOW_UP_TIMEOUT_S)
    _log_output(follow_up, "follow-up")

    has_world = any("WORLD" in l.upper() or "world" in l.lower() for l in follow_up)
    log(f"\n  RESULT: {'stream-json multi-turn WORKS' if has_world else 'stream-json multi-turn does NOT work'}")

    cleanup(proc, master_fd)
    return has_world


def test3_interactive_mode():
    """Test 3: Interactive mode (no -p) with PTY — can we pipe prompts?"""
    log("\n" + "=" * 60)
    log("TEST 3: Interactive mode (no -p) with PTY stdin")
    log("=" * 60)

    master_fd, slave_fd = pty.openpty()
    proc = _start_claude([], slave_fd)

    time.sleep(2)

    if proc.poll() is not None:
        log(f"  Process exited immediately (code={proc.poll()})")
        _log_stderr(proc)
        log("  RESULT: Interactive mode doesn't work without real terminal.")
        cleanup(proc, master_fd)
        return False

    log("  Sending first prompt via PTY...")
    if not _send_follow_up(master_fd,
                           b"Say exactly the word HELLO and nothing else\n"):
        cleanup(proc, master_fd)
        return False

    lines = read_output(proc, master_fd, timeout_s=TIMEOUT)
    _log_output(lines)

    has_hello = any("HELLO" in l.upper() for l in lines)
    log(f"  Got HELLO response: {has_hello}")

    if proc.poll() is not None:
        log("  Process exited after first response.")
        cleanup(proc, master_fd)
        return False

    log("  Sending follow-up...")
    if not _send_follow_up(master_fd,
                           b"Now say exactly the word WORLD and nothing else\n"):
        cleanup(proc, master_fd)
        return False

    follow_up = read_output(proc, master_fd, timeout_s=FOLLOW_UP_TIMEOUT_S)
    _log_output(follow_up, "follow-up")

    has_world = any("WORLD" in l.upper() for l in follow_up)
    log(f"\n  RESULT: {'Interactive PTY multi-turn WORKS' if has_world else 'Interactive PTY multi-turn does NOT work'}")

    cleanup(proc, master_fd)
    return has_world


def main():
    # Check we're not inside Claude Code
    if os.environ.get("CLAUDECODE"):
        log("ERROR: Cannot run inside Claude Code session.")
        log("Run: unset CLAUDECODE && python tests/oq1_stdin_test.py")
        sys.exit(1)

    log("OQ-1: Testing Claude Code stdin behavior")
    log(f"Claude version: {subprocess.run(['claude', '--version'], capture_output=True, text=True).stdout.strip()}")

    r1 = test1_basic_p_mode()
    r2 = test2_stream_json_input()
    r3 = test3_interactive_mode()

    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    log(f"  Test 1 (basic -p + PTY stdin):        {'PASS' if r1 else 'FAIL'}")
    log(f"  Test 2 (--input-format stream-json):   {'PASS' if r2 else 'FAIL'}")
    log(f"  Test 3 (interactive mode + PTY):        {'PASS' if r3 else 'FAIL'}")

    if r2:
        log("\n  RECOMMENDATION: Use --input-format stream-json for multi-turn.")
        log("  Update ClaudeCodeAgent to use stream-json input format.")
    elif r3:
        log("\n  RECOMMENDATION: Use interactive mode (no -p) with PTY for multi-turn.")
        log("  Update ClaudeCodeAgent to drop -p and feed prompts via stdin.")
    elif r1:
        log("\n  RECOMMENDATION: Basic -p + PTY works for follow-up input.")
        log("  Current ClaudeCodeAgent design is correct.")
    else:
        log("\n  RECOMMENDATION: All stdin approaches failed.")
        log("  Fallback to kill-and-restart with resume prompts for Q&A.")
        log("  Update ClaudeCodeAgent to not use send_input(); instead")
        log("  terminate and re-start with answer appended to resume prompt.")

    # Save results
    results_path = os.path.join(os.path.dirname(__file__), "oq1_results.txt")
    with open(results_path, "w") as f:
        f.write("\n".join(RESULTS))
    log(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
