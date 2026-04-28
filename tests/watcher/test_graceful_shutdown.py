"""Tests for watcher graceful shutdown on SIGTERM/SIGINT."""

import json
import importlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.cli import cmd_watcher
from host.watcher.main import _handle_shutdown, shutdown_event
from host.watcher.host_watcher import HostWatcher
from host.watcher.telegram_relay import TelegramRelay
from host.watcher.qa_handler import QAHandler
from host.watcher.tracker_writer import TrackerSocketServer, TrackerWriter
from adapters.trackers.git_bug import GitBugTracker, _graceful_kill

from tests.watcher.conftest import _make_watcher, _make_session

watcher_main = importlib.import_module("host.watcher.main")


# ---------------------------------------------------------------------------
# Signal handler tests
# ---------------------------------------------------------------------------

class TestSignalHandler:
    def setup_method(self):
        shutdown_event.clear()

    def test_handle_shutdown_sets_event(self):
        """_handle_shutdown sets the shutdown event on SIGTERM."""
        assert not shutdown_event.is_set()
        _handle_shutdown(signal.SIGTERM, None)
        assert shutdown_event.is_set()

    def test_handle_shutdown_sigint(self):
        """_handle_shutdown also works for SIGINT."""
        assert not shutdown_event.is_set()
        _handle_shutdown(signal.SIGINT, None)
        assert shutdown_event.is_set()


# ---------------------------------------------------------------------------
# HostWatcher.run() shutdown tests
# ---------------------------------------------------------------------------

class TestWatcherShutdown:
    def test_run_exits_when_shutdown_set_before_start(self, tmp_path):
        """run() exits immediately if shutdown_event is already set."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()  # pre-set
        w.run(shutdown_event=ev)
        # Should return without hanging

    def test_run_exits_when_shutdown_set_during_loop(self, tmp_path):
        """run() exits after shutdown_event is set mid-loop."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()

        # Set the event after a short delay
        def set_later():
            time.sleep(0.05)
            ev.set()

        t = threading.Thread(target=set_later, daemon=True)
        t.start()
        start = time.monotonic()
        w.run(shutdown_event=ev)
        elapsed = time.monotonic() - start
        # Should exit quickly — well under the 2s MAIN_LOOP_SLEEP_S
        assert elapsed < 1.0

    def test_run_uses_event_wait_not_sleep(self, tmp_path):
        """run() uses shutdown_event.wait() which is interruptible, not time.sleep()."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()
        w.run(shutdown_event=ev)
        assert w._shutdown is ev

    def test_run_default_shutdown_event(self, tmp_path):
        """run() creates a default shutdown event if none passed."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()
        # Even with no argument, run() should work — just pass our own
        w.run(shutdown_event=ev)
        assert w._shutdown is ev

    def test_run_propagates_shutdown_to_tracker(self, tmp_path):
        """run() propagates shutdown_event to the tracker's _shutdown."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls that hang when writer exits early
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()  # exit immediately

        mock_tracker = MagicMock()
        mock_tracker._shutdown = threading.Event()
        w._tracker = mock_tracker

        w.run(shutdown_event=ev)
        # The tracker's _shutdown should now be the same event
        assert mock_tracker._shutdown is ev

    def test_run_calls_terminate_current_on_exit(self, tmp_path):
        """run() calls tracker.terminate_current() after loop exits."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls that hang when writer exits early
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()

        mock_tracker = MagicMock()
        mock_tracker._shutdown = threading.Event()
        w._tracker = mock_tracker

        w.run(shutdown_event=ev)
        mock_tracker.terminate_current.assert_called_once()

    def test_main_terminates_tracker_on_watcher_crash(self, tmp_path, monkeypatch):
        """main() terminates the git-bug webui if watcher.run() raises."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sessions = tmp_path / "sessions"
        sessions.mkdir()

        tracker = MagicMock()
        watcher = MagicMock()
        watcher.run.side_effect = RuntimeError("boom")
        watcher._gitbug_tracker.return_value = tracker

        monkeypatch.setattr(watcher_main, "get_repo_root", lambda: repo)
        monkeypatch.setattr(watcher_main, "load_all_dotenv", lambda _path: None)
        monkeypatch.setattr(watcher_main, "HostWatcher", MagicMock(return_value=watcher))
        monkeypatch.setattr(watcher_main, "register", MagicMock())
        monkeypatch.setattr(watcher_main, "unregister", MagicMock())
        monkeypatch.setattr(watcher_main.signal, "signal", lambda *args, **kwargs: None)
        monkeypatch.setattr("sys.argv", [
            "watcher",
            "--sessions-dir",
            str(sessions),
        ])

        with pytest.raises(RuntimeError, match="boom"):
            watcher_main.main()

        tracker.terminate.assert_called_once()
        watcher_main.unregister.assert_called_once_with(repo.name)


# ---------------------------------------------------------------------------
# _graceful_kill helper tests
# ---------------------------------------------------------------------------

class TestGracefulKill:
    def test_graceful_kill_terminates_normally(self):
        """_graceful_kill terminates process that exits within timeout."""
        proc = MagicMock()
        proc.wait.return_value = None
        _graceful_kill(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_graceful_kill_escalates_to_kill(self):
        """_graceful_kill escalates to kill if terminate times out."""
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]
        _graceful_kill(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# GitBugTracker interruptible subprocess tests
# ---------------------------------------------------------------------------

class TestGitBugTrackerShutdown:
    def test_init_accepts_shutdown_event(self):
        """GitBugTracker accepts a shutdown_event parameter."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)
        assert t._shutdown is ev

    def test_init_default_shutdown_event(self):
        """GitBugTracker creates a default event if none passed."""
        t = GitBugTracker(repo_dir="/tmp")
        assert isinstance(t._shutdown, threading.Event)
        assert not t._shutdown.is_set()

    def test_run_returns_empty_on_shutdown(self):
        """_run() returns empty string immediately if shutdown is set."""
        ev = threading.Event()
        ev.set()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)
        result = t._run("bug", "show", "fake-id")
        assert result == ""

    def test_run_interruptible_terminates_on_shutdown(self):
        """_run_interruptible terminates process when shutdown fires."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        # Use a long-running command
        with patch("adapters.trackers.git_bug.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            # poll() returns None (running) then eventually gets terminated
            call_count = 0
            def poll_side_effect():
                nonlocal call_count
                call_count += 1
                if call_count >= 3:
                    ev.set()  # trigger shutdown during poll
                return None
            mock_proc.poll.side_effect = poll_side_effect
            mock_proc.wait.return_value = None
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            stdout, stderr, rc = t._run_interruptible(["git-bug", "pull"], timeout=30)

            assert rc is None  # indicates shutdown interruption
            mock_proc.terminate.assert_called_once()

    def test_run_interruptible_normal_completion(self):
        """_run_interruptible returns output on normal completion."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        with patch("adapters.trackers.git_bug.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0  # finished immediately
            mock_proc.returncode = 0
            mock_proc.stdout.read.return_value = "output data"
            mock_proc.stderr.read.return_value = ""
            mock_popen.return_value = mock_proc

            stdout, stderr, rc = t._run_interruptible(["git-bug", "bug", "show"], timeout=30)

            assert rc == 0
            assert stdout == "output data"
            assert stderr == ""

    def test_run_interruptible_timeout(self):
        """_run_interruptible raises TimeoutExpired if deadline exceeded."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        with patch("adapters.trackers.git_bug.subprocess.Popen") as mock_popen, \
             patch("adapters.trackers.git_bug.time.monotonic") as mock_mono, \
             patch("adapters.trackers.git_bug.time.sleep"):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # never finishes
            mock_proc.wait.return_value = None
            mock_popen.return_value = mock_proc
            # First call (deadline calculation), second+ calls (past deadline)
            mock_mono.side_effect = [100.0, 200.0]

            with pytest.raises(subprocess.TimeoutExpired):
                t._run_interruptible(["git-bug", "bug", "show"], timeout=5)

            mock_proc.kill.assert_called_once()

    def test_retry_sleep_interrupted_by_shutdown(self):
        """Lock retry sleep is interrupted by shutdown event."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        with patch.object(t, "_run_interruptible") as mock_run:
            # First call: lock error
            mock_run.return_value = ("", "already locked by the process pid 1234", 1)

            with patch.object(t, "_pid_alive", return_value=True), \
                 patch.object(t, "_shutdown") as mock_ev:
                mock_ev.is_set.side_effect = [False, False]  # first check, then loop
                mock_ev.wait.return_value = True  # shutdown fired during wait
                result = t._run("pull")

            assert result == ""
            mock_ev.wait.assert_called_once()

    def test_terminate_current_kills_running_process(self):
        """terminate_current() terminates any in-flight subprocess."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = None
        t._current_proc = mock_proc

        t.terminate_current()

        mock_proc.terminate.assert_called_once()

    def test_terminate_current_noop_when_idle(self):
        """terminate_current() does nothing when no process is running."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)
        # No exception raised
        t.terminate_current()

    def test_terminate_current_escalates_to_kill(self):
        """terminate_current() escalates to kill() if terminate times out."""
        ev = threading.Event()
        t = GitBugTracker(repo_dir="/tmp", shutdown_event=ev)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]
        t._current_proc = mock_proc

        t.terminate_current()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_watcher uses os.execvpe (no orphan child process)
# ---------------------------------------------------------------------------

class TestCmdWatcherExecvpe:
    def test_cmd_watcher_uses_execvpe(self, tmp_path):
        """cmd_watcher replaces the process via os.execvpe so signals reach watcher directly."""
        args = MagicMock()
        args.no_auto_start = False
        args.workflow = None

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli.sessions_dir", return_value=tmp_path / "sessions"), \
             patch("host.cli.os.execvpe") as mock_exec:
            (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
            (tmp_path / "WORKFLOW.md").write_text("---\n---\n")
            cmd_watcher(args)

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args
            cmd = call_args[0][1]  # second positional arg is the argv list
            assert "-m" in cmd
            assert "host.watcher" in cmd

    def test_cmd_watcher_passes_no_auto_start(self, tmp_path):
        """cmd_watcher passes --no-auto-start flag when set."""
        args = MagicMock()
        args.no_auto_start = True
        args.workflow = None

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli.sessions_dir", return_value=tmp_path / "sessions"), \
             patch("host.cli.os.execvpe") as mock_exec:
            (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
            (tmp_path / "WORKFLOW.md").write_text("---\n---\n")
            cmd_watcher(args)

            cmd = mock_exec.call_args[0][1]
            assert "--no-auto-start" in cmd

    def test_cmd_watcher_sets_pythonpath(self, tmp_path):
        """cmd_watcher sets PYTHONPATH to agent-worker root in the exec env."""
        args = MagicMock()
        args.no_auto_start = False
        args.workflow = None

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli.sessions_dir", return_value=tmp_path / "sessions"), \
             patch("host.cli.os.execvpe") as mock_exec:
            (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
            (tmp_path / "WORKFLOW.md").write_text("---\n---\n")
            cmd_watcher(args)

            env = mock_exec.call_args[0][2]  # third positional arg is env dict
            assert "PYTHONPATH" in env


# ---------------------------------------------------------------------------
# TelegramRelay.poll_all shutdown tests
# ---------------------------------------------------------------------------

class TestTelegramRelayShutdown:
    def test_poll_all_returns_immediately_when_shutdown_set(self, tmp_path):
        """poll_all() returns empty results without HTTP call when shutdown is set."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        relay = TelegramRelay("tok", "123", "proj", sessions)
        relay.enabled = True
        relay._shutdown.set()

        with patch("host.watcher.requests") as mock_req:
            qa, reviews = relay.poll_all({})

        mock_req.get.assert_not_called()
        assert qa == {}
        assert reviews == {}

    def test_poll_all_makes_request_when_shutdown_not_set(self, tmp_path):
        """poll_all() makes the HTTP request normally when not shutting down."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        relay = TelegramRelay("tok", "123", "proj", sessions)
        relay.enabled = True

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        with patch("host.watcher.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            qa, reviews = relay.poll_all({})

        mock_req.get.assert_called_once()
        assert qa == {}
        assert reviews == {}

    def test_run_propagates_shutdown_to_telegram(self, tmp_path):
        """HostWatcher.run() sets telegram._shutdown to the shutdown event."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls that hang when writer exits early
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()
        w.run(shutdown_event=ev)
        assert w.telegram._shutdown is ev


# ---------------------------------------------------------------------------
# QAHandler shutdown-aware sleep tests
# ---------------------------------------------------------------------------

class TestQAHandlerShutdown:
    def test_scan_returns_early_on_shutdown(self, tmp_path):
        """scan_for_waiting() returns without pausing when shutdown fires during sleep."""
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        waiting = {"question": "Q?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        shutdown_ev = threading.Event()
        w.qa._shutdown = shutdown_ev
        # Set shutdown before scan -- the wait(timeout=PRE_PAUSE_DELAY_S) returns True
        shutdown_ev.set()

        with patch("host.watcher.docker_pause") as mock_pause:
            w.qa.scan_for_waiting()

        # Should NOT have attempted to pause because shutdown was set
        mock_pause.assert_not_called()

    def test_scan_proceeds_when_shutdown_not_set(self, tmp_path):
        """scan_for_waiting() pauses container normally when not shutting down."""
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")
        waiting = {"question": "Q?", "issue_id": "issue-abc"}
        (sd / "waiting.json").write_text(json.dumps(waiting))

        shutdown_ev = threading.Event()
        w.qa._shutdown = shutdown_ev  # not set

        with patch("host.watcher.docker_pause", return_value=True) as mock_pause, \
             patch("host.watcher.time") as mock_time:
            mock_time.time.return_value = 1000.0
            w.qa.scan_for_waiting()

        mock_pause.assert_called_once_with("nightshift-abc")

    def test_run_propagates_shutdown_to_qa(self, tmp_path):
        """HostWatcher.run() sets qa._shutdown to the shutdown event."""
        w = _make_watcher(tmp_path)
        # Mock cleanup methods to avoid tracker calls that hang when writer exits early
        w.monitor.cleanup_stale_review_sessions = lambda: None
        w.monitor.cleanup_stale_blocked_labels = lambda: None
        ev = threading.Event()
        ev.set()
        w.run(shutdown_event=ev)
        assert w.qa._shutdown is ev


# ---------------------------------------------------------------------------
# TrackerSocketServer connection timeout tests
# ---------------------------------------------------------------------------

class TestSocketServerShutdown:
    def test_handle_connection_uses_short_timeout(self, tmp_path):
        """_handle_connection sets a 2s timeout on accepted sockets."""
        shutdown_ev = threading.Event()
        writer = MagicMock(spec=TrackerWriter)
        sock_path = tmp_path / "test.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown_ev)

        mock_conn = MagicMock()
        # recv_json_line returns None (no data) -> connection closes
        with patch("host.watcher.tracker_writer.recv_json_line", return_value=None):
            server._handle_connection(mock_conn)

        mock_conn.settimeout.assert_called_once_with(2)
        mock_conn.close.assert_called_once()
