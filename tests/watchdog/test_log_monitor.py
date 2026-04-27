"""Tests for watchdog log monitor module."""

from pathlib import Path

import pytest

from host.watchdog.log_monitor import LogMonitor, ErrorMatch


def test_detects_error_patterns(tmp_path):
    """Log monitor should detect error patterns."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("""\
2026-04-27 10:00:00 INFO Starting watcher
2026-04-27 10:00:01 ERROR Something failed badly
2026-04-27 10:00:02 INFO Normal operation
Traceback (most recent call last):
  File "test.py", line 1
Exception: Test error
2026-04-27 10:00:03 INFO Done
""")

    monitor = LogMonitor()
    errors = list(monitor.tail(log_file, "test-project"))

    assert len(errors) >= 3
    error_types = {e.error_type for e in errors}
    assert "error" in error_types
    assert "traceback" in error_types
    assert "exception" in error_types


def test_ignores_harmless_patterns(tmp_path):
    """Log monitor should ignore known harmless patterns."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("""\
2026-04-27 10:00:00 INFO no label added or removed - nothing failed here
2026-04-27 10:00:01 INFO Normal operation
""")

    monitor = LogMonitor()
    errors = list(monitor.tail(log_file, "test-project"))

    assert len(errors) == 0


def test_tracks_position(tmp_path):
    """Log monitor should track position and only report new errors."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("2026-04-27 10:00:00 ERROR First error\n")

    monitor = LogMonitor()
    errors1 = list(monitor.tail(log_file, "test-project"))
    assert len(errors1) == 1

    errors2 = list(monitor.tail(log_file, "test-project"))
    assert len(errors2) == 0

    with open(log_file, "a") as f:
        f.write("2026-04-27 10:00:01 ERROR Second error\n")

    errors3 = list(monitor.tail(log_file, "test-project"))
    assert len(errors3) == 1
    assert "Second" in errors3[0].line


def test_handles_log_rotation(tmp_path):
    """Log monitor should handle log rotation (file shrinks)."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("A" * 1000 + "\n2026-04-27 10:00:00 ERROR Old error\n")

    monitor = LogMonitor()
    list(monitor.tail(log_file, "test-project"))

    log_file.write_text("2026-04-27 10:00:01 ERROR New error after rotation\n")

    errors = list(monitor.tail(log_file, "test-project"))
    assert len(errors) == 1
    assert "rotation" in errors[0].line


def test_handles_missing_file(tmp_path):
    """Log monitor should handle missing log file gracefully."""
    log_file = tmp_path / "nonexistent.log"

    monitor = LogMonitor()
    errors = list(monitor.tail(log_file, "test-project"))
    assert errors == []


def test_error_counting():
    """Log monitor should count errors per project/type."""
    monitor = LogMonitor()

    count1 = monitor.count_error("project-a", "exception")
    assert count1 == 1

    count2 = monitor.count_error("project-a", "exception")
    assert count2 == 2

    count3 = monitor.count_error("project-b", "exception")
    assert count3 == 1

    monitor.reset_count("project-a", "exception")
    count4 = monitor.count_error("project-a", "exception")
    assert count4 == 1


def test_detects_critical(tmp_path):
    """Log monitor should detect CRITICAL level."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("2026-04-27 10:00:00 CRITICAL Disk full!\n")

    monitor = LogMonitor()
    errors = list(monitor.tail(log_file, "test-project"))

    assert len(errors) == 1
    assert errors[0].error_type == "critical"


def test_detects_missing_bug(tmp_path):
    """Log monitor should detect 'bug doesn't exist' errors."""
    log_file = tmp_path / "watcher.log"
    log_file.write_text("2026-04-27 10:00:00 WARNING bug doesn't exist: abc123\n")

    monitor = LogMonitor()
    errors = list(monitor.tail(log_file, "test-project"))

    assert len(errors) == 1
    assert errors[0].error_type == "missing_bug"
