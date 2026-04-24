"""Tests for HostWatcher init and Docker utility integration."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import host.watcher as wmod
from host.watcher import HostWatcher
from core.config.models import TrackerConfig, WorkflowConfig
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------

class TestHostWatcherInit:
    def test_default_state(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.sessions_dir == tmp_path / "sessions"
        assert w.repo_dir == tmp_path / "repo"
        assert w.auto_start is False
        assert w.qa._paused == {}
        assert w.reviews._comment_counts == {}
        assert w.reviews._rounds == {}
        assert w._recently_launched == {}
        assert w.reviews._command_failures == {}
        assert w.telegram._offset == 0

    def test_telegram_disabled_without_env(self, tmp_path):
        w = _make_watcher(tmp_path)
        assert w.telegram.enabled is False

    def test_telegram_enabled_with_token_and_chat(self, tmp_path):
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = True
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.telegram.enabled is True
                assert w.telegram.token == "tok"
                assert w.telegram.chat_id == "42"
        finally:
            wmod.HAS_REQUESTS = orig

    def test_telegram_disabled_without_requests(self, tmp_path):
        orig = wmod.HAS_REQUESTS
        wmod.HAS_REQUESTS = False
        try:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}):
                w = HostWatcher(tmp_path / "sessions", tmp_path / "repo")
                assert w.telegram.enabled is False
        finally:
            wmod.HAS_REQUESTS = orig


class TestTrackerSyncConfig:
    def test_maybe_sync_tracker_skips_when_disabled(self, tmp_path):
        w = _make_watcher(tmp_path)
        tracker = MagicMock()
        w._tracker = tracker
        w._config = WorkflowConfig(tracker=TrackerConfig(sync=False))
        w.reviews._last_poll = 0.0

        w._maybe_sync_tracker()

        tracker.sync.assert_not_called()

    def test_maybe_sync_tracker_runs_when_enabled(self, tmp_path):
        w = _make_watcher(tmp_path)
        tracker = MagicMock()
        w._tracker = tracker
        w._config = WorkflowConfig(tracker=TrackerConfig(sync=True))
        w.reviews._last_poll = 0.0

        w._maybe_sync_tracker()

        tracker.sync.assert_called_once()


# ---------------------------------------------------------------------------
# docker utils (through watcher) tests
# ---------------------------------------------------------------------------

class TestDockerUtils:
    def test_docker_pause_called_on_waiting(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "waiting.json").write_text(json.dumps({"question": "Q?", "issue_id": "i"}))

        with patch("host.watcher.docker_pause", return_value=True) as mock_pause, \
             patch("host.watcher.time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")

    def test_docker_unpause_called_with_container_name(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        (sd / "answer.txt").write_text("answer")
        w.qa._paused["abc"] = {
            "container": "nightshift-abc",
            "dir": sd,
            "paused_at": time.time(),
        }

        with patch("host.watcher.docker_unpause") as mock_unpause:
            w.qa.check_for_answers({})

        mock_unpause.assert_called_once_with("nightshift-abc")

    def test_docker_stop_called_on_closed_issue(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.return_value = _make_issue("issue-abc", status="closed")
        w._tracker = tracker
        w.monitor.cleanup_session = MagicMock()

        with patch("host.watcher.docker_stop") as mock_stop, \
             patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_closed_issues()

        mock_stop.assert_called_once_with("nightshift-abc")


# ---------------------------------------------------------------------------
# _launch_background tests
# ---------------------------------------------------------------------------

class TestLaunchBackground:
    def test_stores_popen_handle(self, tmp_path):
        """_launch_background should store the Popen handle for later polling."""
        w = _make_watcher(tmp_path)
        # Use a harmless command
        w._launch_background(["true"], "abc")
        assert "abc" in w._background_procs
        proc, log_fh, launch_time = w._background_procs["abc"]
        assert proc is not None
        assert log_fh is not None
        proc.wait()  # clean up

    def test_file_handle_closed_on_launch_failure(self, tmp_path):
        """If Popen fails, the log file handle should be closed."""
        w = _make_watcher(tmp_path)
        with patch("subprocess.Popen", side_effect=OSError("no such file")):
            w._launch_background(["/nonexistent/binary"], "abc")
        assert "abc" not in w._background_procs

    def test_background_procs_initialized(self, tmp_path):
        """HostWatcher should initialize _background_procs as empty dict."""
        w = _make_watcher(tmp_path)
        assert w._background_procs == {}


# ---------------------------------------------------------------------------
# check_background_launches tests
# ---------------------------------------------------------------------------

class TestCheckBackgroundLaunches:
    def test_successful_exit_cleaned_up(self, tmp_path):
        """Process that exits 0 should be cleaned up without reverting status."""
        w = _make_watcher(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()

        assert "review-abc" not in w._background_procs
        log_fh.close.assert_called_once()

    def test_failed_exit_reverts_coder_status(self, tmp_path):
        """Review launch failure (exit code 1) reverts coder from 'reviewing' to 'waiting:review'."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")

        proc = MagicMock()
        proc.poll.return_value = 1
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()

        assert "review-abc" not in w._background_procs
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:review"
        log_fh.close.assert_called_once()

    def test_still_running_within_grace_not_cleaned(self, tmp_path):
        """Process still running within grace period should not be cleaned up."""
        w = _make_watcher(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time())  # just launched

        w.check_background_launches()

        assert "review-abc" in w._background_procs
        log_fh.close.assert_not_called()

    def test_still_running_past_grace_cleaned_up(self, tmp_path):
        """Process still running past grace period has file handle closed and is removed."""
        w = _make_watcher(tmp_path)
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 9999)

        w.check_background_launches()

        assert "review-abc" not in w._background_procs
        log_fh.close.assert_called_once()

    def test_non_review_launch_failure_no_revert(self, tmp_path):
        """Non-review (coder) launch failure doesn't try to revert anything."""
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        proc = MagicMock()
        proc.poll.return_value = 1
        log_fh = MagicMock()
        w._background_procs["abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()

        assert "abc" not in w._background_procs
        # Coder session status should be unchanged (no revert for non-review)
        state = json.loads((w.sessions_dir / "abc" / "state.json").read_text())
        assert state["status"] == "working"

    def test_failed_review_launch_no_coder_dir(self, tmp_path):
        """Review launch failure with missing coder dir should not crash."""
        w = _make_watcher(tmp_path)
        # No coder session "abc" created

        proc = MagicMock()
        proc.poll.return_value = 1
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()  # should not raise

        assert "review-abc" not in w._background_procs

    def test_failed_review_launch_coder_not_in_reviewing(self, tmp_path):
        """If coder is not in 'reviewing' status, no revert should happen."""
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        proc = MagicMock()
        proc.poll.return_value = 1
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()

        # Status should remain unchanged since it wasn't "reviewing"
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:review"


# ---------------------------------------------------------------------------
# Startup cleanup tests
# ---------------------------------------------------------------------------

class TestStartupCleanup:
    """Verify stale review sessions are cleaned up on watcher startup."""

    def test_run_calls_cleanup_stale_review_sessions(self, tmp_path):
        """HostWatcher.run() should call cleanup_stale_review_sessions() on startup."""
        import threading

        w = _make_watcher(tmp_path)
        w.telegram.enabled = False

        # Mock the cleanup method to track if it's called
        cleanup_called = []
        original_cleanup = w.monitor.cleanup_stale_review_sessions
        def mock_cleanup():
            cleanup_called.append(True)
            original_cleanup()
        w.monitor.cleanup_stale_review_sessions = mock_cleanup

        # Create a shutdown event that triggers immediately
        shutdown = threading.Event()
        shutdown.set()

        # Run the watcher - it should call cleanup then exit
        with patch("adapters.trackers.git_bug.repair_lamport_clocks"):
            w.run(shutdown_event=shutdown)

        assert len(cleanup_called) == 1, "cleanup_stale_review_sessions should be called once on startup"


# ---------------------------------------------------------------------------
# auto-start regression tests
# ---------------------------------------------------------------------------

class TestAutoStartRegressions:
    def test_auto_start_after_reject(self, tmp_path):
        """Auto-start should relaunch after cleanup removes the session dir."""
        from core.config import AutoStartConfig

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        w = HostWatcher(sessions, repo, auto_start=True)
        w.telegram.enabled = False
        w._auto_start_config = AutoStartConfig(
            enabled=True,
            label="nightshift",
            poll_interval_s=0,
            max_concurrent=4,
        )

        issue = _make_issue("issue-abc", labels=["nightshift"])
        tracker = MagicMock()
        tracker.list_issues.return_value = [issue]
        w._tracker = tracker

        launched_sids = []
        w.monitor._launch_background = lambda cmd, sid: launched_sids.append(sid) or True

        with patch("host.watcher.session_monitor.post_start"):
            w.monitor.check_new_issues()

        assert launched_sids == ["issue-abc"]

        session_dir = _make_session(
            w.sessions_dir, "issue-abc", status="working", issue_id=issue.id
        )
        with patch("host.watcher.session_monitor.load_workflow") as mock_load_workflow, \
             patch("host.watcher.remove_worktree"):
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_load_workflow.return_value = cfg
            w.monitor.cleanup_session("issue-abc", issue.id, session_dir)

        assert not session_dir.exists()

        with patch("host.watcher.session_monitor.post_start"):
            w.monitor.check_new_issues()

        assert launched_sids == ["issue-abc", "issue-abc"]

    def test_auto_start_after_sighup(self, tmp_path):
        """Config reload should not block auto-start when no session dir exists."""
        from core.config import AutoStartConfig

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        w = HostWatcher(sessions, repo, auto_start=True)
        w.telegram.enabled = False

        issue = _make_issue("issue-abc", labels=["nightshift"])
        tracker = MagicMock()
        tracker.list_issues.return_value = [issue]
        w.monitor.__dict__["_known_issue_ids"] = {issue.id}

        launched_sids = []
        w.monitor._launch_background = lambda cmd, sid: launched_sids.append(sid) or True

        workflow_config = WorkflowConfig(
            auto_start=AutoStartConfig(
                enabled=True,
                label="nightshift",
                poll_interval_s=0,
                max_concurrent=4,
            )
        )
        with patch("host.watcher.host_watcher.load_workflow", return_value=workflow_config), \
             patch("host.watcher.host_watcher.create_tracker", return_value=tracker):
            w.reload_config()

        with patch("host.watcher.session_monitor.post_start"):
            w.monitor.check_new_issues()

        assert launched_sids == ["issue-abc"]


# ---------------------------------------------------------------------------
# reload_config tracker lifecycle tests
# ---------------------------------------------------------------------------

class TestReloadConfigTrackerLifecycle:
    """Test that reload_config properly handles tracker lifecycle (REQ-026)."""

    def test_reload_config_tracker_failure_restores_old(self, tmp_path):
        """When tracker creation fails, the old tracker should be restored."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        # Make create_tracker fail
        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", side_effect=Exception("Connection failed")):
            w.reload_config()

        # Old tracker should be restored
        assert w._tracker is old_tracker

    def test_reload_config_terminates_old_tracker_on_success(self, tmp_path):
        """Old tracker should be terminated when new tracker creation succeeds."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        new_tracker = MagicMock()

        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", return_value=new_tracker):
            w.reload_config()

        # Old tracker should be terminated
        old_tracker.terminate_current.assert_called_once()
        # New tracker should be in place
        assert w._tracker is new_tracker

    def test_reload_config_keeps_old_tracker_alive_on_failure(self, tmp_path):
        """Old tracker should NOT be terminated when new tracker creation fails."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        # Make create_tracker fail
        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", side_effect=Exception("Connection failed")):
            w.reload_config()

        # Old tracker should NOT be terminated - we're still using it
        old_tracker.terminate_current.assert_not_called()
        # Old tracker should be restored
        assert w._tracker is old_tracker
