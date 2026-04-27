"""Tests for watchdog scanner module."""

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from host.watchdog.scanner import (
    WatcherStatus,
    scan_registrations,
    check_pid_alive,
    parse_registration,
    cleanup_stale,
    STALE_THRESHOLD_HOURS,
)


def test_check_pid_alive_current_process():
    """Current process PID should be alive."""
    assert check_pid_alive(os.getpid()) is True


def test_check_pid_alive_dead_process():
    """Non-existent PID should be dead."""
    assert check_pid_alive(999999999) is False


def test_finds_registered_watchers(tmp_path):
    """Scanner should find all registered watchers."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    now = datetime.now(timezone.utc)
    (projects_d / "project-a.yaml").write_text(f"""\
path: /home/user/src/project-a
log: /home/user/src/project-a/.nightshift/watcher.log
pid: {os.getpid()}
started: {now.isoformat()}
""")
    (projects_d / "project-b.yaml").write_text(f"""\
path: /home/user/src/project-b
log: /home/user/src/project-b/.nightshift/watcher.log
pid: 999999999
started: {now.isoformat()}
""")

    watchers = list(scan_registrations(projects_d))
    assert len(watchers) == 2

    names = {w.project for w in watchers}
    assert names == {"project-a", "project-b"}

    alive = [w for w in watchers if w.alive]
    assert len(alive) == 1
    assert alive[0].project == "project-a"


def test_detects_dead_pid(tmp_path):
    """Scanner should detect dead PIDs."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    now = datetime.now(timezone.utc)
    (projects_d / "dead-project.yaml").write_text(f"""\
path: /home/user/src/dead-project
log: /home/user/src/dead-project/.nightshift/watcher.log
pid: 999999999
started: {now.isoformat()}
""")

    watchers = list(scan_registrations(projects_d))
    assert len(watchers) == 1
    assert watchers[0].alive is False


def test_parse_registration_invalid_yaml(tmp_path):
    """Invalid YAML should return None."""
    reg_file = tmp_path / "bad.yaml"
    reg_file.write_text("not: valid: yaml: [")

    result = parse_registration(reg_file)
    assert result is None


def test_parse_registration_missing_fields(tmp_path):
    """Missing required fields should return None."""
    reg_file = tmp_path / "incomplete.yaml"
    reg_file.write_text("path: /some/path\n")

    result = parse_registration(reg_file)
    assert result is None


def test_watcher_status_is_stale():
    """Stale detection should work correctly."""
    old_time = datetime.now(timezone.utc) - timedelta(hours=STALE_THRESHOLD_HOURS + 1)

    status_dead_old = WatcherStatus(
        project="test",
        path=Path("/test"),
        log_path=Path("/test/log"),
        pid=999999999,
        started=old_time,
        alive=False,
        registration_file=Path("/test.yaml"),
    )
    assert status_dead_old.is_stale is True

    recent_time = datetime.now(timezone.utc) - timedelta(hours=1)
    status_dead_recent = WatcherStatus(
        project="test",
        path=Path("/test"),
        log_path=Path("/test/log"),
        pid=999999999,
        started=recent_time,
        alive=False,
        registration_file=Path("/test.yaml"),
    )
    assert status_dead_recent.is_stale is False

    status_alive = WatcherStatus(
        project="test",
        path=Path("/test"),
        log_path=Path("/test/log"),
        pid=os.getpid(),
        started=old_time,
        alive=True,
        registration_file=Path("/test.yaml"),
    )
    assert status_alive.is_stale is False


def test_cleanup_stale_removes_file(tmp_path):
    """Stale registration should be removed."""
    reg_file = tmp_path / "stale.yaml"
    reg_file.write_text("dummy")

    old_time = datetime.now(timezone.utc) - timedelta(hours=STALE_THRESHOLD_HOURS + 1)
    status = WatcherStatus(
        project="stale",
        path=Path("/test"),
        log_path=Path("/test/log"),
        pid=999999999,
        started=old_time,
        alive=False,
        registration_file=reg_file,
    )

    assert reg_file.exists()
    result = cleanup_stale(status)
    assert result is True
    assert not reg_file.exists()


def test_cleanup_stale_keeps_non_stale(tmp_path):
    """Non-stale registration should not be removed."""
    reg_file = tmp_path / "recent.yaml"
    reg_file.write_text("dummy")

    recent_time = datetime.now(timezone.utc) - timedelta(hours=1)
    status = WatcherStatus(
        project="recent",
        path=Path("/test"),
        log_path=Path("/test/log"),
        pid=999999999,
        started=recent_time,
        alive=False,
        registration_file=reg_file,
    )

    result = cleanup_stale(status)
    assert result is False
    assert reg_file.exists()


def test_scan_empty_directory(tmp_path):
    """Scanning empty directory should yield nothing."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    watchers = list(scan_registrations(projects_d))
    assert watchers == []


def test_scan_nonexistent_directory(tmp_path):
    """Scanning nonexistent directory should yield nothing."""
    projects_d = tmp_path / "nonexistent"

    watchers = list(scan_registrations(projects_d))
    assert watchers == []
