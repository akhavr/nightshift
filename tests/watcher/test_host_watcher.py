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
from host.watcher.host_watcher import RecentlyLaunchedDict
from host.constants import RECENTLY_LAUNCHED_FILENAME, ORPHAN_GRACE_PERIOD_S, LOCK_TIMEOUT_S
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

    def test_successful_review_completion_processes_verdict(self, tmp_path):
        """Successful review exit (rc=0) should extract and process the verdict."""
        w = _make_watcher(tmp_path)
        # Create coder session in "reviewing" status
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        # Create review session with "approve" verdict in conversation
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        conv_log = review_dir / "conversation.jsonl"
        conv_log.write_text(json.dumps({"content": "@nightshift approve"}) + "\n")

        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        # Mock cleanup_review_session to verify it's called
        with patch.object(w.reviews, 'cleanup_review_session') as mock_cleanup:
            w.check_background_launches()

        assert "review-abc" not in w._background_procs
        # Coder session should transition to waiting:human-review
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "waiting:human-review"
        log_fh.close.assert_called_once()

    def test_successful_review_completion_cleans_up_session(self, tmp_path):
        """Successful review exit should clean up the review session."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        conv_log = review_dir / "conversation.jsonl"
        conv_log.write_text(json.dumps({"content": "@nightshift approve"}) + "\n")

        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        cleanup_called = []
        original_cleanup = w.reviews.cleanup_review_session
        def mock_cleanup(sid, session_dir):
            cleanup_called.append(sid)
        w.reviews.cleanup_review_session = mock_cleanup

        w.check_background_launches()

        assert cleanup_called == ["review-abc"]

    def test_successful_review_completion_revise_verdict(self, tmp_path):
        """Successful review with 'revise' verdict resumes coder session."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        conv_log = review_dir / "conversation.jsonl"
        conv_log.write_text(json.dumps({"content": "@nightshift revise fix the bug"}) + "\n")

        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        # Mock the background launch to prevent actual subprocess (must return True for success)
        launched = []
        def mock_launch(cmd, sid):
            launched.append(sid)
            return True
        w._launch_background = mock_launch
        w.reviews.verdicts._launch_background = mock_launch

        w.check_background_launches()

        # Coder session should transition to working and be relaunched
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "working"
        assert "abc" in launched

    def test_successful_review_no_verdict_no_action(self, tmp_path):
        """Successful review exit with no verdict should still clean up."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        review_dir = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        # No conversation log or verdict

        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["review-abc"] = (proc, log_fh, time.time() - 5)

        cleanup_called = []
        original_cleanup = w.reviews.cleanup_review_session
        def mock_cleanup(sid, session_dir):
            cleanup_called.append(sid)
        w.reviews.cleanup_review_session = mock_cleanup

        w.check_background_launches()

        # Should still clean up
        assert "review-abc" not in w._background_procs
        assert cleanup_called == ["review-abc"]
        # Coder stays in reviewing (orphan detector will handle it later)
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "reviewing"

    def test_successful_coder_completion_no_action(self, tmp_path):
        """Successful coder exit (non-review) should just clean up tracking."""
        w = _make_watcher(tmp_path)
        coder_dir = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        proc = MagicMock()
        proc.poll.return_value = 0
        log_fh = MagicMock()
        w._background_procs["abc"] = (proc, log_fh, time.time() - 5)

        w.check_background_launches()

        # Session should be removed from tracking
        assert "abc" not in w._background_procs
        log_fh.close.assert_called_once()
        # Status unchanged - container sets its own status
        state = json.loads((coder_dir / "state.json").read_text())
        assert state["status"] == "working"


# ---------------------------------------------------------------------------
# Startup cleanup tests
# ---------------------------------------------------------------------------

class TestStartupCleanup:
    """Verify stale review sessions are cleaned up on watcher startup."""

    def test_startup_cleans_stale_blocked(self, tmp_path):
        """HostWatcher.run() should call cleanup_stale_blocked_labels on startup."""
        import threading

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        w = HostWatcher(sessions, repo, auto_start=False)

        # Track if cleanup was called
        cleanup_called = False

        def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        w.monitor.cleanup_stale_blocked_labels = mock_cleanup
        w.monitor.cleanup_stale_review_sessions = lambda: None

        # Pre-set shutdown so we exit immediately after startup
        shutdown_event = threading.Event()
        shutdown_event.set()

        with patch("host.watcher.host_watcher.repair_lamport_clocks"):
            w.run(shutdown_event=shutdown_event)

        assert cleanup_called is True

    def test_run_calls_cleanup_stale_review_sessions(self, tmp_path):
        """HostWatcher.run() should call cleanup_stale_review_sessions() on startup."""
        import threading

        w = _make_watcher(tmp_path)
        w.telegram.enabled = False

        # Mock the cleanup methods to track if they're called
        cleanup_called = []
        original_cleanup = w.monitor.cleanup_stale_review_sessions
        def mock_cleanup():
            cleanup_called.append(True)
            original_cleanup()
        w.monitor.cleanup_stale_review_sessions = mock_cleanup
        # Also mock cleanup_stale_blocked_labels to avoid tracker calls
        w.monitor.cleanup_stale_blocked_labels = lambda: None

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

    def test_reload_config_terminates_old_before_new(self, tmp_path):
        """Old tracker is terminated before new tracker creation to release locks."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        # Track call order
        call_order = []

        def record_terminate():
            call_order.append("terminate")

        def record_create(*args, **kwargs):
            call_order.append("create")
            return MagicMock()

        old_tracker.terminate_current.side_effect = record_terminate

        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", side_effect=record_create), \
             patch("host.watcher.host_watcher.time.sleep"):
            w.reload_config()

        # Terminate should be called BEFORE create
        assert call_order == ["terminate", "create"]

    def test_reload_waits_for_old_tracker(self, tmp_path):
        """reload_config waits for old tracker termination before creating new."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        sleep_calls = []

        def record_sleep(duration):
            sleep_calls.append(duration)

        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", return_value=MagicMock()), \
             patch("host.watcher.host_watcher.time.sleep", side_effect=record_sleep):
            w.reload_config()

        # Should sleep after termination (0.5s as per TRACKER_TERMINATION_WAIT_S)
        from host.constants import TRACKER_TERMINATION_WAIT_S
        assert TRACKER_TERMINATION_WAIT_S in sleep_calls
        old_tracker.terminate_current.assert_called_once()

    def test_reload_retries_tracker_creation(self, tmp_path):
        """reload_config retries tracker creation with exponential backoff."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        # Fail first two attempts, succeed on third
        attempt_count = [0]
        new_tracker = MagicMock()

        def failing_create(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                raise Exception(f"Transient failure {attempt_count[0]}")
            return new_tracker

        sleep_calls = []

        def record_sleep(duration):
            sleep_calls.append(duration)

        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", side_effect=failing_create), \
             patch("host.watcher.host_watcher.time.sleep", side_effect=record_sleep):
            w.reload_config()

        # Should have tried 3 times
        assert attempt_count[0] == 3
        # New tracker should be installed
        assert w._tracker is new_tracker
        # Backoff sleeps: 0.5s (after termination), 0.5s (retry 1), 1.0s (retry 2)
        from host.constants import TRACKER_TERMINATION_WAIT_S, TRACKER_RELOAD_BACKOFF_BASE_S
        assert TRACKER_TERMINATION_WAIT_S in sleep_calls
        assert TRACKER_RELOAD_BACKOFF_BASE_S in sleep_calls  # First retry
        assert TRACKER_RELOAD_BACKOFF_BASE_S * 2 in sleep_calls  # Second retry

    def test_reload_restores_old_tracker_after_all_retries_fail(self, tmp_path):
        """When all retry attempts fail, old tracker object is restored."""
        w = _make_watcher(tmp_path)

        old_tracker = MagicMock()
        old_tracker.terminate_current = MagicMock()
        w._tracker = old_tracker
        w._config = WorkflowConfig()

        with patch("host.watcher.host_watcher.load_workflow", return_value=WorkflowConfig()), \
             patch("host.watcher.host_watcher.create_tracker", side_effect=Exception("Persistent failure")), \
             patch("host.watcher.host_watcher.time.sleep"):
            w.reload_config()

        # Old tracker object should be restored (even though its subprocess was terminated)
        assert w._tracker is old_tracker


# ---------------------------------------------------------------------------
# _recently_launched persistence tests
# ---------------------------------------------------------------------------

class TestRecentlyLaunchedPersistence:
    """Test that _recently_launched dict survives watcher restarts."""

    def test_recently_launched_persisted_on_add(self, tmp_path):
        """Adding to _recently_launched writes to recently_launched.json."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent  # .nightshift is parent of sessions

        w = HostWatcher(sessions, repo)
        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME

        # Initially, file may not exist or be empty
        w._recently_launched["test-session"] = time.time()

        # File should now exist with the entry
        assert persist_file.exists()
        data = json.loads(persist_file.read_text())
        assert "test-session" in data

    def test_recently_launched_loaded_on_startup(self, tmp_path):
        """Watcher startup loads recently_launched.json."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent

        # Pre-create the persistence file with a recent entry
        now = time.time()
        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME
        persist_file.write_text(json.dumps({"preloaded-session": now}))

        # Create watcher - should load existing entries
        w = HostWatcher(sessions, repo)

        assert "preloaded-session" in w._recently_launched
        assert w._recently_launched["preloaded-session"] == now

    def test_recently_launched_pruned_on_load(self, tmp_path):
        """Entries older than ORPHAN_GRACE_PERIOD_S are removed on load."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent

        now = time.time()
        # One recent entry, one stale entry
        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME
        persist_file.write_text(json.dumps({
            "recent-session": now - 10,  # 10s ago - should survive
            "stale-session": now - ORPHAN_GRACE_PERIOD_S - 100,  # Way past grace period
        }))

        # Create watcher - should prune stale entries
        w = HostWatcher(sessions, repo)

        assert "recent-session" in w._recently_launched
        assert "stale-session" not in w._recently_launched

        # Pruned state should be persisted
        data = json.loads(persist_file.read_text())
        assert "recent-session" in data
        assert "stale-session" not in data

    def test_recently_launched_pop_persists(self, tmp_path):
        """Removing entries via pop() persists the change."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent

        w = HostWatcher(sessions, repo)
        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME

        w._recently_launched["session-a"] = time.time()
        w._recently_launched["session-b"] = time.time()

        # Pop one entry
        w._recently_launched.pop("session-a", None)

        # File should reflect the removal
        data = json.loads(persist_file.read_text())
        assert "session-a" not in data
        assert "session-b" in data

    def test_recently_launched_del_persists(self, tmp_path):
        """Removing entries via del persists the change."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent

        w = HostWatcher(sessions, repo)
        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME

        w._recently_launched["session-x"] = time.time()

        del w._recently_launched["session-x"]

        data = json.loads(persist_file.read_text())
        assert "session-x" not in data

    def test_recently_launched_handles_corrupt_file(self, tmp_path):
        """Watcher should handle corrupt JSON file gracefully."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = sessions.parent

        persist_file = nightshift_dir / RECENTLY_LAUNCHED_FILENAME
        persist_file.write_text("not valid json {{{")

        # Should not crash - starts with empty dict
        w = HostWatcher(sessions, repo)
        assert w._recently_launched == {}

    def test_recently_launched_handles_missing_file(self, tmp_path):
        """Watcher should work when no persistence file exists."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        # No pre-existing file
        w = HostWatcher(sessions, repo)
        assert w._recently_launched == {}


# ---------------------------------------------------------------------------
# Worktree integrity guardrail tests
# ---------------------------------------------------------------------------

class TestWorktreeIntegrityGuardrail:
    """Test that watcher halts when .git/worktrees/ is deleted with active sessions."""

    def _make_watcher_with_nightshift_layout(self, tmp_path):
        """Build a watcher with proper .nightshift/sessions layout."""
        import threading
        repo = tmp_path / "repo"
        repo.mkdir()
        nightshift_dir = repo / ".nightshift"
        nightshift_dir.mkdir()
        sessions = nightshift_dir / "sessions"
        sessions.mkdir()
        w = HostWatcher(sessions, repo, auto_start=False)
        w.telegram.enabled = False
        w._shutdown = threading.Event()
        return w

    def test_halts_on_missing_worktrees(self, tmp_path):
        """Watcher should halt when .git/worktrees/ is missing with active sessions."""
        w = self._make_watcher_with_nightshift_layout(tmp_path)

        # Create .git directory but NOT .git/worktrees/
        git_dir = w.repo_dir / ".git"
        git_dir.mkdir(parents=True)
        # .git/worktrees/ intentionally NOT created

        # Create an active session
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Check should return False and set shutdown
        result = w._check_worktree_integrity()

        assert result is False
        assert w._shutdown.is_set()

    def test_no_halt_when_no_active_sessions(self, tmp_path):
        """Watcher should NOT halt when no active sessions exist (even if worktrees missing)."""
        w = self._make_watcher_with_nightshift_layout(tmp_path)

        # Create .git directory but NOT .git/worktrees/
        git_dir = w.repo_dir / ".git"
        git_dir.mkdir(parents=True)
        # .git/worktrees/ intentionally NOT created

        # Create a session in non-active status
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        # Check should return True (pass) and NOT set shutdown
        result = w._check_worktree_integrity()

        assert result is True
        assert not w._shutdown.is_set()

    def test_no_halt_when_worktrees_exists(self, tmp_path):
        """Watcher should NOT halt when .git/worktrees/ exists with active sessions."""
        w = self._make_watcher_with_nightshift_layout(tmp_path)

        # Create .git/worktrees/ directory
        worktrees_dir = w.repo_dir / ".git" / "worktrees"
        worktrees_dir.mkdir(parents=True)

        # Create an active session
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Check should return True (pass) and NOT set shutdown
        result = w._check_worktree_integrity()

        assert result is True
        assert not w._shutdown.is_set()

    def test_halts_on_reviewing_status(self, tmp_path):
        """Watcher should halt when .git/worktrees/ missing with 'reviewing' status."""
        w = self._make_watcher_with_nightshift_layout(tmp_path)

        git_dir = w.repo_dir / ".git"
        git_dir.mkdir(parents=True)

        # Create session in 'reviewing' status (also considered active)
        _make_session(w.sessions_dir, "review-abc", status="reviewing", issue_id="issue-abc")

        result = w._check_worktree_integrity()

        assert result is False
        assert w._shutdown.is_set()

    def test_halts_on_starting_status(self, tmp_path):
        """Watcher should halt when .git/worktrees/ missing with 'starting' status."""
        w = self._make_watcher_with_nightshift_layout(tmp_path)

        git_dir = w.repo_dir / ".git"
        git_dir.mkdir(parents=True)

        # Create session in 'starting' status
        _make_session(w.sessions_dir, "abc", status="starting", issue_id="issue-abc")

        result = w._check_worktree_integrity()

        assert result is False
        assert w._shutdown.is_set()


# ---------------------------------------------------------------------------
# Tracker lock guardrail tests
# ---------------------------------------------------------------------------

class TestTrackerLockGuardrail:
    """Test that watcher warns when git-bug lock is stuck."""

    def test_warns_on_stuck_lock(self, tmp_path, caplog):
        """Watcher should warn when git-bug lock is held too long."""
        import logging

        w = _make_watcher(tmp_path)

        # Create a stale lock file (older than LOCK_TIMEOUT_S)
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        lock_file.write_text("")

        # Set mtime to be older than LOCK_TIMEOUT_S
        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        import os
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" in caplog.text
        assert "may be stuck" in caplog.text

    def test_no_warn_on_fresh_lock(self, tmp_path, caplog):
        """Watcher should not warn when git-bug lock is fresh."""
        import logging

        w = _make_watcher(tmp_path)

        # Create a fresh lock file (newer than LOCK_TIMEOUT_S)
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        lock_file.write_text("")

        # mtime is already fresh (just created)

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" not in caplog.text

    def test_no_warn_when_lock_missing(self, tmp_path, caplog):
        """Watcher should not warn when git-bug lock file doesn't exist."""
        import logging

        w = _make_watcher(tmp_path)

        # No lock file created

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" not in caplog.text

    def test_lock_warning_shows_pid(self, tmp_path, caplog):
        """Lock warning should show the holder PID from the lock file."""
        import logging
        import os

        w = _make_watcher(tmp_path)

        # Create a stale lock file with a PID
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        lock_file.write_text("12345\n")

        # Set mtime to be older than LOCK_TIMEOUT_S
        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" in caplog.text
        assert "pid 12345" in caplog.text

    def test_lock_warning_shows_process_name(self, tmp_path, caplog):
        """Lock warning should show the process command line."""
        import logging
        import os

        w = _make_watcher(tmp_path)

        # Create a stale lock file with current PID (so ps will find it)
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        current_pid = os.getpid()
        lock_file.write_text(f"{current_pid}\n")

        # Set mtime to be older than LOCK_TIMEOUT_S
        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" in caplog.text
        assert f"pid {current_pid}" in caplog.text
        # Should show the process name (python in this case)
        assert "python" in caplog.text.lower() or "pytest" in caplog.text.lower()

    def test_lock_warning_shows_parent_process(self, tmp_path, caplog):
        """Lock warning should show the parent process info."""
        import logging
        import os

        w = _make_watcher(tmp_path)

        # Create a stale lock file with current PID
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        current_pid = os.getpid()
        lock_file.write_text(f"{current_pid}\n")

        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        # Should mention parent process
        assert "parent" in caplog.text.lower()

    def test_lock_warning_suppressed_for_own_child(self, tmp_path, caplog):
        """Watcher should not warn when the lock holder is its own child."""
        import logging
        import os
        from unittest.mock import Mock

        w = _make_watcher(tmp_path)

        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        lock_file.write_text("769398\n")

        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        current_pid = os.getpid()
        mock_ps = Mock()
        mock_ps.return_value.stdout = f"git-bug webui --no-open --h {current_pid}\n"

        with patch("host.watcher.host_watcher.subprocess.run", mock_ps), \
             caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        assert "git-bug lock held for" not in caplog.text
        mock_ps.assert_called_once_with(
            ["ps", "-p", "769398", "-o", "args=,ppid="],
            capture_output=True,
            text=True,
        )

    def test_lock_warning_handles_invalid_pid(self, tmp_path, caplog):
        """Lock warning should handle non-numeric lock file content gracefully."""
        import logging
        import os

        w = _make_watcher(tmp_path)

        # Create a stale lock file with invalid content
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        lock_file.write_text("not-a-pid\n")

        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        # Should still warn, just without PID details
        assert "git-bug lock held for" in caplog.text
        assert "may be stuck" in caplog.text

    def test_lock_warning_handles_dead_process(self, tmp_path, caplog):
        """Lock warning should handle a PID that no longer exists."""
        import logging
        import os

        w = _make_watcher(tmp_path)

        # Create a stale lock file with a PID that doesn't exist
        lock_dir = w.repo_dir / ".git" / "git-bug"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "lock"
        # Use a very high PID that almost certainly doesn't exist
        lock_file.write_text("999999999\n")

        stale_time = time.time() - LOCK_TIMEOUT_S - 10
        os.utime(lock_file, (stale_time, stale_time))

        with caplog.at_level(logging.WARNING):
            w._check_tracker_lock()

        # Should warn with PID but note process is unknown/dead
        assert "git-bug lock held for" in caplog.text
        assert "pid 999999999" in caplog.text


# ---------------------------------------------------------------------------
# Disk space guardrail tests
# ---------------------------------------------------------------------------

class TestDiskSpaceGuardrail:
    """Test that watcher halts when disk space is low."""

    def test_halts_on_low_disk(self, tmp_path, caplog):
        """Watcher should halt when free disk space is below MIN_FREE_GB."""
        import logging
        import threading
        from unittest.mock import patch

        w = _make_watcher(tmp_path)
        w._shutdown = threading.Event()

        # Mock os.statvfs to return low disk space (0.5 GB free)
        mock_statvfs = MagicMock()
        mock_statvfs.f_bavail = 500  # blocks available
        mock_statvfs.f_frsize = 1024 * 1024  # 1 MB block size -> 500 MB total

        with patch("os.statvfs", return_value=mock_statvfs):
            with caplog.at_level(logging.CRITICAL):
                result = w._check_disk_space()

        assert result is False
        assert w._shutdown.is_set()
        assert "Disk space low" in caplog.text
        assert "Halting" in caplog.text

    def test_continues_on_sufficient_disk(self, tmp_path, caplog):
        """Watcher should continue when free disk space is above MIN_FREE_GB."""
        import logging
        import threading
        from unittest.mock import patch

        w = _make_watcher(tmp_path)
        w._shutdown = threading.Event()

        # Mock os.statvfs to return sufficient disk space (5 GB free)
        mock_statvfs = MagicMock()
        mock_statvfs.f_bavail = 5000  # blocks available
        mock_statvfs.f_frsize = 1024 * 1024  # 1 MB block size -> 5 GB total

        with patch("os.statvfs", return_value=mock_statvfs):
            with caplog.at_level(logging.CRITICAL):
                result = w._check_disk_space()

        assert result is True
        assert not w._shutdown.is_set()
        assert "Disk space low" not in caplog.text


# ---------------------------------------------------------------------------
# Config watchdog tests
# ---------------------------------------------------------------------------

class TestConfigWatchdog:
    """Test that config watchdog detects .git/config modifications."""

    def test_config_watchdog_starts_on_init(self, tmp_path):
        """ConfigWatchdog should be created and started in HostWatcher.__init__."""
        from unittest.mock import patch, MagicMock
        from host.watcher.config_watchdog import ConfigWatchdog

        # Mock ConfigWatchdog to track instantiation
        with patch("host.watcher.host_watcher.ConfigWatchdog") as MockWatchdog:
            mock_instance = MagicMock()
            MockWatchdog.return_value = mock_instance

            w = _make_watcher(tmp_path)

            # Verify ConfigWatchdog was created with .git/config path
            MockWatchdog.assert_called_once()
            call_args = MockWatchdog.call_args
            config_path = call_args[0][0]
            assert str(config_path).endswith(".git/config")
            # Verify start() was called
            mock_instance.start.assert_called_once()

    def test_config_watchdog_logs_modification(self, tmp_path, caplog):
        """ConfigWatchdog should log WARNING when .git/config is modified."""
        import logging
        import threading
        import time
        from unittest.mock import patch
        from host.watcher.config_watchdog import ConfigWatchdog

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        config_file = git_dir / "config"
        config_file.write_text("[core]\n\trepositoryformatversion = 0\n")

        shutdown = threading.Event()
        # Use faster poll interval for testing
        with patch("host.watcher.config_watchdog.CONFIG_POLL_INTERVAL_S", 0.05):
            watchdog = ConfigWatchdog(config_file, shutdown)
            watchdog.start()

            # Give it time to record initial mtime
            time.sleep(0.1)

            # Modify the config file
            with caplog.at_level(logging.WARNING):
                time.sleep(0.05)  # Ensure mtime changes
                config_file.write_text("[core]\n\tworktree = /workspace\n")
                # Wait for watchdog to detect change
                time.sleep(0.2)

            shutdown.set()
            watchdog.join(timeout=1.0)

        assert "config modified" in caplog.text.lower() or ".git/config" in caplog.text
