"""Tests for watcher config reload on SIGHUP."""

import signal
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher.main import _handle_reload, reload_event
from host.watcher.host_watcher import HostWatcher, _diff_config
from core.config.models import (
    WorkflowConfig, NotifierConfig, AutoStartConfig, MergeConfig,
    ReviewConfig, TrackerConfig,
)
from core.protocols import NotificationLevel

from tests.watcher.conftest import _make_watcher


# ---------------------------------------------------------------------------
# Signal handler tests
# ---------------------------------------------------------------------------

class TestReloadSignalHandler:
    def setup_method(self):
        reload_event.clear()

    def test_handle_reload_sets_event(self):
        """_handle_reload sets the reload event on SIGHUP."""
        assert not reload_event.is_set()
        _handle_reload(signal.SIGHUP, None)
        assert reload_event.is_set()

    def test_reload_event_is_independent_of_shutdown(self):
        """reload_event and shutdown_event are separate events."""
        from host.watcher.main import shutdown_event
        reload_event.clear()
        shutdown_event.clear()
        _handle_reload(signal.SIGHUP, None)
        assert reload_event.is_set()
        assert not shutdown_event.is_set()


# ---------------------------------------------------------------------------
# _diff_config tests
# ---------------------------------------------------------------------------

class TestDiffConfig:
    def test_no_changes(self):
        """Identical configs produce no changes."""
        a = WorkflowConfig()
        b = WorkflowConfig()
        assert _diff_config(a, b) == []

    def test_notification_change(self):
        a = WorkflowConfig(notifications=[NotifierConfig(kind="telegram", level="all")])
        b = WorkflowConfig(notifications=[NotifierConfig(kind="telegram", level="questions")])
        changes = _diff_config(a, b)
        assert "notifications" in changes

    def test_auto_start_change(self):
        a = WorkflowConfig()
        b = WorkflowConfig(auto_start=AutoStartConfig(enabled=True, label="nightshift"))
        changes = _diff_config(a, b)
        assert "auto_start" in changes

    def test_merge_change(self):
        a = WorkflowConfig()
        b = WorkflowConfig(merge=MergeConfig(require_review=False))
        changes = _diff_config(a, b)
        assert "merge" in changes

    def test_review_change(self):
        a = WorkflowConfig()
        b = WorkflowConfig(review=ReviewConfig(max_rounds=5))
        changes = _diff_config(a, b)
        assert "review" in changes

    def test_tracker_change(self):
        a = WorkflowConfig()
        b = WorkflowConfig(tracker=TrackerConfig(kind="github"))
        changes = _diff_config(a, b)
        assert "tracker" in changes

    def test_multiple_changes(self):
        a = WorkflowConfig()
        b = WorkflowConfig(
            merge=MergeConfig(require_review=False),
            review=ReviewConfig(max_rounds=10),
        )
        changes = _diff_config(a, b)
        assert "merge" in changes
        assert "review" in changes


# ---------------------------------------------------------------------------
# HostWatcher.reload_config tests
# ---------------------------------------------------------------------------

class TestReloadConfig:
    def _write_workflow(self, path, notifications_level="all", auto_start_enabled=False,
                        merge_require_review=True):
        """Write a minimal WORKFLOW.md with configurable settings."""
        content = f"""---
notifications:
  - kind: telegram
    level: {notifications_level}
auto_start:
  enabled: {str(auto_start_enabled).lower()}
  label: nightshift
merge:
  require_review: {str(merge_require_review).lower()}
---
Prompt body.
"""
        path.write_text(content)

    def test_reload_updates_notification_level(self, tmp_path):
        """reload_config updates TelegramRelay notification level."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf, notifications_level="all")
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        # Force initial config load
        w._config = None
        w._telegram_level_from_config()

        assert w.telegram._level == NotificationLevel.ALL

        # Update workflow to questions-only
        self._write_workflow(wf, notifications_level="questions")
        w.reload_config()

        assert w.telegram._level == NotificationLevel.QUESTIONS

    def test_reload_updates_auto_start(self, tmp_path):
        """reload_config enables/disables auto_start based on new config."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf, auto_start_enabled=False)
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        w.auto_start = False

        # Enable auto-start in config
        self._write_workflow(wf, auto_start_enabled=True)
        w.reload_config()

        assert w.auto_start is True
        assert w._auto_start_config.enabled is True

    def test_reload_disables_auto_start(self, tmp_path):
        """reload_config disables auto_start when config changes."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf, auto_start_enabled=True)
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        w.auto_start = True
        w._auto_start_config = AutoStartConfig(enabled=True)

        self._write_workflow(wf, auto_start_enabled=False)
        w.reload_config()

        assert w.auto_start is False

    def test_reload_recreates_tracker(self, tmp_path):
        """reload_config forces tracker re-creation."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf)
        w = _make_watcher(tmp_path)
        w.workflow_path = wf

        old_tracker = MagicMock()
        w._tracker = old_tracker

        with patch("host.watcher.host_watcher.create_tracker") as mock_ct:
            new_tracker = MagicMock()
            mock_ct.return_value = new_tracker
            w.reload_config()

        assert w._tracker is new_tracker
        old_tracker.terminate_current.assert_called_once()

    def test_reload_propagates_shutdown_to_new_tracker(self, tmp_path):
        """reload_config propagates _shutdown event to the new tracker."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf)
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        w._shutdown = threading.Event()

        with patch("host.watcher.host_watcher.create_tracker") as mock_ct:
            new_tracker = MagicMock()
            new_tracker._shutdown = threading.Event()
            mock_ct.return_value = new_tracker
            w.reload_config()

        assert new_tracker._shutdown is w._shutdown

    def test_reload_survives_parse_error(self, tmp_path):
        """reload_config logs error and keeps old config on parse failure."""
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf, notifications_level="all")
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        w.reload_config()  # initial load

        old_config = w._config

        # Write invalid YAML
        wf.write_text("---\n  bad:\nyaml: [unclosed\n---\n")
        w.reload_config()

        # Config should be unchanged
        assert w._config is old_config

    def test_reload_logs_changes(self, tmp_path, caplog):
        """reload_config logs which config sections changed."""
        import logging
        wf = tmp_path / "WORKFLOW.md"
        self._write_workflow(wf, notifications_level="all")
        w = _make_watcher(tmp_path)
        w.workflow_path = wf
        w.reload_config()  # initial load

        self._write_workflow(wf, notifications_level="questions",
                             merge_require_review=False)
        with caplog.at_level(logging.INFO, logger="watcher"):
            w.reload_config()

        assert any("notifications" in r.message and "merge" in r.message
                    for r in caplog.records)


# ---------------------------------------------------------------------------
# run() integration with reload_event
# ---------------------------------------------------------------------------

class TestRunReloadIntegration:
    def test_run_checks_reload_event(self, tmp_path):
        """run() calls reload_config when reload_event is set."""
        w = _make_watcher(tmp_path)
        shutdown = threading.Event()
        reload_ev = threading.Event()

        reload_ev.set()  # trigger reload on first iteration
        # Shutdown after one loop iteration
        original_reload = w.reload_config

        def reload_and_stop():
            original_reload()
            shutdown.set()

        w.reload_config = MagicMock(side_effect=reload_and_stop)
        w.run(shutdown_event=shutdown, reload_event=reload_ev)

        w.reload_config.assert_called_once()
        # reload_event should be cleared after processing
        assert not reload_ev.is_set()

    def test_run_default_reload_event(self, tmp_path):
        """run() creates a default reload event if none passed."""
        w = _make_watcher(tmp_path)
        ev = threading.Event()
        ev.set()  # exit immediately
        w.run(shutdown_event=ev)
        assert hasattr(w, '_reload')
        assert isinstance(w._reload, threading.Event)
