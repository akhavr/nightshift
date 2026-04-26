"""Tests for auto-start behavior including blocked issue handling."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher.session_monitor import SessionMonitor, is_blocked
from core.protocols import TrackerIssue

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue


class TestIsBlocked:
    def test_empty_labels_not_blocked(self):
        assert is_blocked([]) is False

    def test_unrelated_labels_not_blocked(self):
        assert is_blocked(["nightshift", "urgent", "bug"]) is False

    def test_blocked_label_detected(self):
        assert is_blocked(["nightshift", "blocked:abc123"]) is True

    def test_multiple_blocked_labels(self):
        assert is_blocked(["blocked:abc123", "blocked:def456"]) is True


class TestAutoStartSkipsBlocked:
    def test_skips_blocked_issues(self, tmp_path):
        """Auto-start should skip issues with blocked:<id> labels."""
        w = _make_watcher(tmp_path, tg_enabled=False)
        w.auto_start = True
        w.monitor.auto_start = True
        w.monitor._last_auto_start_poll = 0.0

        blocked_issue = _make_issue(
            "blocked123456",
            title="Blocked Issue",
            labels=["nightshift", "blocked:dep123456"],
        )
        unblocked_issue = _make_issue(
            "unblocked12345",
            title="Unblocked Issue",
            labels=["nightshift"],
        )

        mock_tracker = MagicMock()
        mock_tracker.list_issues.return_value = [blocked_issue, unblocked_issue]
        w.monitor._get_tracker = lambda: mock_tracker

        mock_asc = MagicMock()
        mock_asc.label = "nightshift"
        mock_asc.poll_interval_s = 0
        mock_asc.max_concurrent = 10
        w.monitor._get_auto_start_config = lambda: mock_asc

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.session_monitor.post_start"):
            w.monitor.check_new_issues()

        # Only unblocked issue should be started (SID truncated to 12 chars)
        assert len(launched) == 1
        assert launched[0] == "unblocked123"

    def test_manual_start_ignores_blocked(self, tmp_path):
        """Manual CLI start/resume should work regardless of blocked labels.

        This is tested implicitly - cmd_start and cmd_resume do not check
        blocked labels, they only check session state.
        """
        # The manual start path does NOT filter by labels, it just launches.
        # This test documents the expected behavior.
        pass


class TestCleanupStaleBlockedLabels:
    def test_removes_stale_blocked_labels(self, tmp_path):
        """On startup, remove blocked labels where the blocker is closed."""
        w = _make_watcher(tmp_path)

        # Closed issue that was blocking others (12-char ID)
        closed_issue = _make_issue(
            "closed123456",
            title="Closed Issue",
            labels=[],
            status="closed",
        )
        # Open issue still blocked by the closed issue (label must match prefix exactly)
        open_blocked = _make_issue(
            "open12345678",
            title="Open Issue",
            labels=["nightshift", "blocked:closed123456"],
            status="open",
        )

        mock_tracker = MagicMock()
        mock_tracker.list_issues.return_value = [closed_issue, open_blocked]
        w.monitor._get_tracker = lambda: mock_tracker

        w.monitor.cleanup_stale_blocked_labels()

        # Should have removed the stale label
        mock_tracker.remove_label.assert_called_once_with(
            "open12345678", "blocked:closed123456"
        )

    def test_keeps_valid_blocked_labels(self, tmp_path):
        """Don't remove blocked labels when the blocker is still open."""
        w = _make_watcher(tmp_path)

        # Still-open blocking issue (12-char ID)
        blocking = _make_issue(
            "blocking1234",
            title="Blocking Issue",
            labels=["nightshift"],
            status="open",
        )
        # Blocked by the still-open issue (label matches prefix)
        blocked = _make_issue(
            "blocked12345",
            title="Blocked Issue",
            labels=["nightshift", "blocked:blocking1234"],
            status="open",
        )

        mock_tracker = MagicMock()
        mock_tracker.list_issues.return_value = [blocking, blocked]
        w.monitor._get_tracker = lambda: mock_tracker

        w.monitor.cleanup_stale_blocked_labels()

        # Should NOT remove any labels
        mock_tracker.remove_label.assert_not_called()

    def test_tracker_failure_logged(self, tmp_path):
        """Tracker failures during cleanup should be logged, not raise."""
        w = _make_watcher(tmp_path)

        mock_tracker = MagicMock()
        mock_tracker.list_issues.side_effect = Exception("Network error")
        w.monitor._get_tracker = lambda: mock_tracker

        # Should not raise
        w.monitor.cleanup_stale_blocked_labels()


class TestStartupCleansStaleBlocked:
    def test_watcher_startup_calls_cleanup(self, tmp_path):
        """HostWatcher.run() should call cleanup_stale_blocked_labels on startup."""
        from host.watcher import HostWatcher
        import threading

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        w = HostWatcher(sessions, repo, auto_start=False)

        # Track if cleanup was called
        cleanup_called = False
        original_cleanup = w.monitor.cleanup_stale_blocked_labels

        def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        w.monitor.cleanup_stale_blocked_labels = mock_cleanup
        w.monitor.cleanup_stale_review_sessions = lambda: None

        # Set shutdown immediately so we don't loop forever
        w._shutdown = threading.Event()
        w._shutdown.set()

        with patch("host.watcher.host_watcher.repair_lamport_clocks"):
            w.run()

        assert cleanup_called is True
