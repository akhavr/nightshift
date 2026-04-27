"""Tests for watchdog session checker module."""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from host.watchdog.session_checker import (
    check_sessions,
    find_stuck_sessions,
    StuckSession,
    STUCK_THRESHOLD_MINUTES,
)


def test_finds_stuck_working_session(tmp_path):
    """Session checker should find sessions stuck in working state."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "abc123def456"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text(json.dumps({"status": "working"}))

    old_time = time.time() - (STUCK_THRESHOLD_MINUTES + 5) * 60
    import os
    os.utime(state_file, (old_time, old_time))

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 1
    assert stuck[0].status == "working"
    assert stuck[0].minutes_stuck >= STUCK_THRESHOLD_MINUTES


def test_ignores_recent_session(tmp_path):
    """Session checker should ignore recently updated sessions."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "recent123"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text(json.dumps({"status": "working"}))

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 0


def test_ignores_non_active_status(tmp_path):
    """Session checker should ignore sessions not in active states."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "completed123"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text(json.dumps({"status": "waiting:review"}))

    old_time = time.time() - (STUCK_THRESHOLD_MINUTES + 5) * 60
    import os
    os.utime(state_file, (old_time, old_time))

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 0


def test_finds_stuck_across_projects(tmp_path):
    """find_stuck_sessions should check all registered projects."""
    projects_d = tmp_path / "projects.d"
    projects_d.mkdir()

    project_a = tmp_path / "project-a"
    project_a.mkdir()
    sessions_a = project_a / ".nightshift" / "sessions" / "session-a"
    sessions_a.mkdir(parents=True)
    state_a = sessions_a / "state.json"
    state_a.write_text(json.dumps({"status": "working"}))

    old_time = time.time() - (STUCK_THRESHOLD_MINUTES + 5) * 60
    import os
    os.utime(state_a, (old_time, old_time))

    (projects_d / "project-a.yaml").write_text(f"""\
path: {project_a}
log: {project_a / '.nightshift' / 'watcher.log'}
pid: 12345
started: 2026-04-27T10:00:00+00:00
""")

    stuck = list(find_stuck_sessions(projects_d))
    assert len(stuck) == 1
    assert stuck[0].project == "project-a"


def test_handles_missing_state_file(tmp_path):
    """Session checker should handle missing state.json gracefully."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "nostate123"
    session_dir.mkdir(parents=True)

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 0


def test_handles_invalid_state_json(tmp_path):
    """Session checker should handle invalid state.json gracefully."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "badjson123"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text("not valid json")

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 0


def test_handles_missing_sessions_dir(tmp_path):
    """Session checker should handle missing sessions directory."""
    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 0


def test_finds_stuck_starting_session(tmp_path):
    """Session checker should find sessions stuck in starting state."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "starting123"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text(json.dumps({"status": "starting"}))

    old_time = time.time() - (STUCK_THRESHOLD_MINUTES + 5) * 60
    import os
    os.utime(state_file, (old_time, old_time))

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 1
    assert stuck[0].status == "starting"


def test_finds_stuck_reviewing_session(tmp_path):
    """Session checker should find sessions stuck in reviewing state."""
    sessions_dir = tmp_path / ".nightshift" / "sessions"
    session_dir = sessions_dir / "reviewing123"
    session_dir.mkdir(parents=True)

    state_file = session_dir / "state.json"
    state_file.write_text(json.dumps({"status": "reviewing"}))

    old_time = time.time() - (STUCK_THRESHOLD_MINUTES + 5) * 60
    import os
    os.utime(state_file, (old_time, old_time))

    stuck = list(check_sessions(tmp_path, "test-project"))
    assert len(stuck) == 1
    assert stuck[0].status == "reviewing"
