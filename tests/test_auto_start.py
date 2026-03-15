"""Tests for auto-start: config parsing and watcher issue filtering."""

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.config import load_workflow, AutoStartConfig
from core.protocols import TrackerIssue


# --- Config parsing ---

def test_auto_start_config_defaults():
    """AutoStartConfig has sensible defaults."""
    cfg = AutoStartConfig()
    assert cfg.enabled is False
    assert cfg.label == "nightshift"
    assert cfg.poll_interval_s == 30
    assert cfg.max_concurrent == 4


def test_load_workflow_auto_start_section(tmp_path):
    """WORKFLOW.md auto_start section is parsed correctly."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        auto_start:
          enabled: true
          label: auto-work
          poll_interval_s: 60
          max_concurrent: 2
        ---
        Prompt body
    """))
    config = load_workflow(wf)
    assert config.auto_start.enabled is True
    assert config.auto_start.label == "auto-work"
    assert config.auto_start.poll_interval_s == 60
    assert config.auto_start.max_concurrent == 2


def test_load_workflow_auto_start_missing(tmp_path):
    """Missing auto_start section uses defaults."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        agent:
          kind: claude-code
        ---
        Prompt body
    """))
    config = load_workflow(wf)
    assert config.auto_start.enabled is False
    assert config.auto_start.label == "nightshift"


def test_load_workflow_auto_start_partial(tmp_path):
    """Partial auto_start section fills in defaults."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(textwrap.dedent("""\
        ---
        auto_start:
          enabled: true
        ---
        Prompt body
    """))
    config = load_workflow(wf)
    assert config.auto_start.enabled is True
    assert config.auto_start.label == "nightshift"
    assert config.auto_start.poll_interval_s == 30
    assert config.auto_start.max_concurrent == 4


# --- Watcher auto-start logic ---

def _make_issue(issue_id, title="Test", labels=None, status="open"):
    return TrackerIssue(
        id=issue_id, identifier=issue_id[:12], title=title,
        body="", status=status, labels=labels or [],
    )


class TestWatcherAutoStart:
    """Test HostWatcher._check_new_issues() filtering logic."""

    def _make_watcher(self, tmp_path, label="nightshift", max_concurrent=4,
                      poll_interval_s=0):
        """Create a HostWatcher with mocked config and tracker."""
        from host.watcher import HostWatcher

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        watcher = HostWatcher(sessions_dir, tmp_path, auto_start=True)

        # Inject config directly
        asc = AutoStartConfig(
            enabled=True, label=label,
            poll_interval_s=poll_interval_s, max_concurrent=max_concurrent,
        )
        watcher._auto_start_config = asc
        watcher.telegram.enabled = False

        return watcher

    def test_filters_by_label(self, tmp_path):
        """Only issues with the configured label are started."""
        watcher = self._make_watcher(tmp_path, label="nightshift")
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
            _make_issue("id-2", labels=["bug"]),
            _make_issue("id-3", labels=["nightshift", "urgent"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        watcher.monitor.check_new_issues()

        assert sorted(launched) == ["id-1", "id-3"]

    def test_skips_existing_sessions(self, tmp_path):
        """Issues with existing sessions are not re-started."""
        watcher = self._make_watcher(tmp_path, label="nightshift")
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
            _make_issue("id-2", labels=["nightshift"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()

        # Create existing session for id-1
        sd = tmp_path / "sessions" / "id-1"
        sd.mkdir()
        (sd / "state.json").write_text(json.dumps({"issue_id": "id-1", "status": "working"}))

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        watcher.monitor.check_new_issues()

        assert launched == ["id-2"]

    def test_respects_max_concurrent(self, tmp_path):
        """Max concurrent sessions limit is respected."""
        watcher = self._make_watcher(tmp_path, label="nightshift", max_concurrent=2)
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
            _make_issue("id-2", labels=["nightshift"]),
            _make_issue("id-3", labels=["nightshift"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()

        # One session already active
        sd = tmp_path / "sessions" / "existing"
        sd.mkdir()
        (sd / "state.json").write_text(json.dumps({"issue_id": "existing", "status": "working"}))

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        watcher.monitor.check_new_issues()

        # max_concurrent=2, 1 already active, so only 1 new launch
        assert len(launched) == 1
        assert launched == ["id-1"]

    def test_known_issue_ids_prevents_relaunch(self, tmp_path):
        """Issues already in _known_issue_ids are not re-launched."""
        watcher = self._make_watcher(tmp_path, label="nightshift")
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()
        watcher.monitor._known_issue_ids.add("id-1")

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        watcher.monitor.check_new_issues()

        assert launched == []

    def test_poll_interval_respected(self, tmp_path):
        """Second poll within interval is skipped."""
        import time

        watcher = self._make_watcher(tmp_path, label="nightshift", poll_interval_s=999)
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        # First poll succeeds
        watcher.monitor._last_auto_start_poll = 0
        watcher.monitor.check_new_issues()
        assert len(launched) == 1

        # Second poll within interval is skipped
        launched.clear()
        watcher.monitor._known_issue_ids.clear()
        watcher.monitor.check_new_issues()
        assert len(launched) == 0

    def test_empty_label_matches_all(self, tmp_path):
        """Empty label string matches all open issues."""
        watcher = self._make_watcher(tmp_path, label="")
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["bug"]),
            _make_issue("id-2", labels=[]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        watcher.monitor.check_new_issues()

        # Empty label means no filtering
        assert sorted(launched) == ["id-1", "id-2"]

    def test_tracker_poll_failure_handled_gracefully(self, tmp_path):
        """Tracker poll failure logs warning and does not crash or launch."""
        watcher = self._make_watcher(tmp_path, label="nightshift")
        tracker = MagicMock()
        tracker.list_issues.side_effect = RuntimeError("connection refused")
        watcher._tracker = tracker
        watcher._config = MagicMock()

        launched = []
        watcher.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        # Should not raise
        watcher.monitor.check_new_issues()

        assert launched == []

    def test_telegram_notification_on_launch(self, tmp_path):
        """Telegram notification is sent when auto-starting."""
        watcher = self._make_watcher(tmp_path, label="nightshift")
        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", title="Fix bug", labels=["nightshift"]),
        ]
        watcher._tracker = tracker
        watcher._config = MagicMock()
        watcher.monitor._launch_background = lambda cmd, sid: None

        tg_messages = []
        watcher.telegram.notify = lambda msg, **kw: tg_messages.append(msg)

        watcher.monitor.check_new_issues()

        assert len(tg_messages) == 1
        assert "id-1" in tg_messages[0]


class TestWatcherCountActiveSessions:
    """Test _count_active_sessions helper."""

    def test_counts_working_sessions(self, tmp_path):
        from host.watcher import HostWatcher

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        watcher = HostWatcher(sessions_dir, tmp_path)

        for sid, status in [("s1", "working"), ("s2", "starting"),
                            ("s3", "waiting:review"), ("s4", "done")]:
            sd = sessions_dir / sid
            sd.mkdir()
            (sd / "state.json").write_text(json.dumps({"status": status}))

        assert watcher.monitor.count_active_sessions() == 2
