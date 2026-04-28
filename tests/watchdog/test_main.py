"""Tests for watchdog main module."""

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from host.watchdog.main import (
    list_watchers,
    check_once,
    format_status_line,
    REPEATED_ERROR_THRESHOLD,
)
from host.watchdog.scanner import WatcherStatus
from host.watchdog.log_monitor import LogMonitor
from host.watchdog.alerter import Alerter, AlertConfig


def test_format_status_line_alive():
    """Format status line for alive watcher."""
    status = WatcherStatus(
        project="test-project",
        path=Path("/home/user/src/test-project"),
        log_path=Path("/home/user/src/test-project/.nightshift/watcher.log"),
        pid=12345,
        started=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        alive=True,
        registration_file=Path("/home/user/.nightshift/projects.d/test-project.yaml"),
    )

    line = format_status_line(status)
    assert "✓" in line
    assert "test-project" in line
    assert "12345" in line


def test_format_status_line_dead():
    """Format status line for dead watcher."""
    status = WatcherStatus(
        project="dead-project",
        path=Path("/home/user/src/dead-project"),
        log_path=Path("/home/user/src/dead-project/.nightshift/watcher.log"),
        pid=99999,
        started=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        alive=False,
        registration_file=Path("/home/user/.nightshift/projects.d/dead-project.yaml"),
    )

    line = format_status_line(status)
    assert "✗" in line
    assert "dead-project" in line


def test_list_watchers_empty(tmp_path, capsys):
    """List watchers should handle empty registry."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    with patch("host.watchdog.main.PROJECTS_D", projects_d):
        with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
            result = list_watchers()

    assert result == 0
    captured = capsys.readouterr()
    assert "No watchers registered" in captured.out


def test_list_watchers_with_entries(tmp_path, capsys):
    """List watchers should show registered watchers."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    now = datetime.now(timezone.utc)
    (projects_d / "project-a.yaml").write_text(f"""\
path: /home/user/src/project-a
log: /home/user/src/project-a/.nightshift/watcher.log
pid: {os.getpid()}
started: {now.isoformat()}
""")

    with patch("host.watchdog.main.PROJECTS_D", projects_d):
        with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
            result = list_watchers()

    assert result == 0
    captured = capsys.readouterr()
    assert "project-a" in captured.out
    assert "Alive: 1" in captured.out


def test_list_watchers_skips_dead_entries(tmp_path, capsys):
    """List watchers should omit dead registrations and clean them up."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    now = datetime.now(timezone.utc)
    dead_reg = projects_d / "project-dead.yaml"
    dead_reg.write_text(f"""\
path: /home/user/src/project-dead
log: /home/user/src/project-dead/.nightshift/watcher.log
pid: 999999999
started: {now.isoformat()}
""")

    with patch("host.watchdog.main.PROJECTS_D", projects_d):
        with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
            result = list_watchers()

    assert result == 0
    captured = capsys.readouterr()
    assert "project-dead" not in captured.out
    assert "No watchers registered" in captured.out
    assert not dead_reg.exists()


def test_check_once_detects_crash(tmp_path):
    """Check once should detect crashed watchers."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    now = datetime.now(timezone.utc)
    (projects_d / "crashed.yaml").write_text(f"""\
path: /home/user/src/crashed
log: {tmp_path / "watcher.log"}
pid: 999999999
started: {now.isoformat()}
""")

    config = AlertConfig(alert_cooldown_s=0)
    alerter = Alerter(config=config)
    log_monitor = LogMonitor()

    with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
        with patch.object(alerter, "send", return_value=True) as mock_send:
            issues = check_once(alerter, log_monitor)

    assert issues == 1
    mock_send.assert_called()


def test_check_once_detects_log_errors(tmp_path):
    """Check once should detect errors in logs."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    log_file = tmp_path / "watcher.log"
    log_file.write_text("2026-04-27 10:00:00 CRITICAL Disk full!\n")

    now = datetime.now(timezone.utc)
    (projects_d / "project.yaml").write_text(f"""\
path: /home/user/src/project
log: {log_file}
pid: {os.getpid()}
started: {now.isoformat()}
""")

    config = AlertConfig(alert_cooldown_s=0)
    alerter = Alerter(config=config)
    log_monitor = LogMonitor()

    with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
        with patch.object(alerter, "send", return_value=True) as mock_send:
            issues = check_once(alerter, log_monitor)

    assert issues == 1
    mock_send.assert_called()


def test_check_once_suppresses_infrequent_missing_bug(tmp_path):
    """Check once should suppress infrequent missing_bug errors."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    log_file = tmp_path / "watcher.log"
    log_file.write_text("2026-04-27 10:00:00 WARNING bug doesn't exist: abc123\n")

    now = datetime.now(timezone.utc)
    (projects_d / "project.yaml").write_text(f"""\
path: /home/user/src/project
log: {log_file}
pid: {os.getpid()}
started: {now.isoformat()}
""")

    config = AlertConfig(alert_cooldown_s=0)
    alerter = Alerter(config=config)
    log_monitor = LogMonitor()

    with patch("host.watchdog.scanner.PROJECTS_D", projects_d):
        with patch.object(alerter, "send", return_value=True) as mock_send:
            issues = check_once(alerter, log_monitor)

    assert issues == 0
    mock_send.assert_not_called()
