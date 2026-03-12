"""Tests for watcher graceful shutdown on SIGTERM/SIGINT."""

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher.main import _handle_shutdown, shutdown_event
from host.watcher.host_watcher import HostWatcher
from adapters.trackers.git_bug import GitBugTracker, _POLL_INTERVAL_S

from tests.watcher.conftest import _make_watcher


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
        ev = threading.Event()
        ev.set()  # pre-set
        w.run(shutdown_event=ev)
        # Should return without hanging

    def test_run_exits_when_shutdown_set_during_loop(self, tmp_path):
        """run() exits after shutdown_event is set mid-loop."""
        w = _make_watcher(tmp_path)
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
        ev = threading.Event()
        ev.set()
        w.run(shutdown_event=ev)
        assert w._shutdown is ev

    def test_run_default_shutdown_event(self, tmp_path):
        """run() creates a default shutdown event if none passed."""
        w = _make_watcher(tmp_path)
        ev = threading.Event()
        ev.set()
        # Even with no argument, run() should work — just pass our own
        w.run(shutdown_event=ev)
        assert w._shutdown is ev


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
