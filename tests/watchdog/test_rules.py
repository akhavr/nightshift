"""Tests for watchdog rules module."""

from host.watchdog import rules


def test_check_errors_detects_outbox_validation_failure():
    """Error detection should catch outbox validation failures logged at ERROR level."""
    lines = [
        "2026-04-28 12:00:00 INFO [watcher] Starting",
        "2026-04-28 12:00:01 ERROR [watcher] [ca8e754a8413] Invalid outbox entry on line 1: Invalid issue_id: xyz",
        "2026-04-28 12:00:02 INFO [watcher] Done",
    ]

    anomalies = rules.check_errors(lines, threshold=0)

    assert len(anomalies) == 1
    assert anomalies[0].type == "error_threshold"
    assert "Invalid outbox entry" in anomalies[0].context


def test_check_errors_ignores_warning_level():
    """Error detection should NOT catch WARNING level logs."""
    lines = [
        "2026-04-28 12:00:00 INFO [watcher] Starting",
        "2026-04-28 12:00:01 WARNING [watcher] Something minor happened",
        "2026-04-28 12:00:02 INFO [watcher] Done",
    ]

    anomalies = rules.check_errors(lines, threshold=0)

    assert len(anomalies) == 0
