"""Tests for SessionMonitor: orphaned sessions, closed issues, cleanup."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# check_orphaned_sessions tests
# ---------------------------------------------------------------------------

class TestCheckOrphanedSessions:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = time.time()
        _make_session(w.sessions_dir, "abc", status="working")
        with patch("host.watcher.docker_container_status") as mock_cs:
            w.monitor.check_orphaned_sessions()
        mock_cs.assert_not_called()

    def test_running_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_orphaned_session_auto_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" in w._recently_launched

    def test_recently_launched_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()  # just launched
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_paused_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="paused"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_non_active_status_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_grace_period_expired_triggers_resume(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # Put in recently_launched but with old timestamp
        w._recently_launched["abc"] = time.time() - 9999  # way past grace period
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" not in w._recently_launched or w._recently_launched["abc"] > time.time() - 5


# ---------------------------------------------------------------------------
# check_closed_issues tests
# ---------------------------------------------------------------------------

class TestCheckClosedIssues:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = time.time()
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w.monitor.check_closed_issues()

        tracker.get_issue.assert_not_called()

    def test_working_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w.monitor.check_closed_issues()

        tracker.get_issue.assert_not_called()

    def test_closed_issue_triggers_cleanup(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        with patch("host.watcher.docker_stop"):
            w.monitor.check_closed_issues()

        assert "abc" in cleaned

    def test_open_issue_not_cleaned(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="open")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        w.monitor.check_closed_issues()

        assert cleaned == []

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w.monitor.check_closed_issues()


# ---------------------------------------------------------------------------
# cleanup_session tests
# ---------------------------------------------------------------------------

class TestCleanupSession:
    def test_removes_session_dir_and_clears_tracking(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg
            w.monitor.cleanup_session("abc", "issue-abc", sd)

        mock_rmtree.assert_called_once_with(sd)
        assert "abc" not in w._recently_launched

    def test_cleanup_failure_logged_not_raised(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")

        with patch("core.config.load_workflow", side_effect=RuntimeError("boom")):
            # should not raise
            w.monitor.cleanup_session("abc", "issue-abc", sd)
