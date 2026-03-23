"""Tests for GitBugTracker lock retry and stale lock detection.

Verifies:
- Lock error triggers retry with backoff
- Stale lock (dead PID) is detected and cleared
- Live PID lock is NOT removed
- Constants are used correctly
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.trackers.git_bug import GitBugTracker
from core.constants import LOCK_RETRY_ATTEMPTS, LOCK_RETRY_BASE_DELAY_S


LOCK_ERROR_PID_42 = "Error: the repository you want to access is already locked by the process pid 42"
LOCK_ERROR_PID_999 = "Error: the repository you want to access is already locked by the process pid 999"


@pytest.fixture
def tracker():
    """Create a GitBugTracker with a fake repo dir for testing."""
    return GitBugTracker(repo_dir="/tmp/fake")


class TestExtractLockPid:
    """_extract_lock_pid parses the PID from git-bug lock error messages."""

    def test_extracts_pid(self):
        assert GitBugTracker._extract_lock_pid(LOCK_ERROR_PID_42) == 42

    def test_extracts_large_pid(self):
        assert GitBugTracker._extract_lock_pid(
            "already locked by the process pid 123456"
        ) == 123456

    def test_returns_none_on_no_match(self):
        assert GitBugTracker._extract_lock_pid("some other error") is None

    def test_returns_none_on_empty(self):
        assert GitBugTracker._extract_lock_pid("") is None


class TestPidAlive:
    """_pid_alive checks whether a process is still running."""

    def test_alive_process(self):
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None  # no exception = alive
            assert GitBugTracker._pid_alive(42) is True
        mock_kill.assert_called_once_with(42, 0)

    def test_dead_process(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            assert GitBugTracker._pid_alive(42) is False

    def test_permission_error_means_alive(self):
        """If we can't signal it due to permissions, the process exists."""
        with patch("os.kill", side_effect=PermissionError):
            assert GitBugTracker._pid_alive(42) is True


class TestLockRetry:
    """_run() retries on lock errors with backoff."""

    def test_retries_on_lock_then_succeeds(self, tracker):
        """Lock on first attempt, success on second."""
        t = tracker
        call_count = 0

        def fake_run_interruptible(cmd, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ("", LOCK_ERROR_PID_42, 1)
            return ("output", "", 0)

        with patch.object(t, "_run_interruptible", side_effect=fake_run_interruptible), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t._shutdown, "wait", return_value=False):
            result = t._run("bug", "show", "abc")

        assert result == "output"
        assert call_count == 2

    def test_retries_up_to_max_attempts(self, tracker):
        """All attempts hit lock error with live PID — returns empty."""
        t = tracker

        with patch.object(t, "_run_interruptible",
                          return_value=("", LOCK_ERROR_PID_42, 1)), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t._shutdown, "wait", return_value=False):
            result = t._run("bug", "show", "abc")

        assert result == ""

    def test_retry_uses_exponential_backoff(self, tracker):
        """Retry delay doubles each attempt (exponential backoff)."""
        t = tracker
        call_count = 0

        def fake_run_interruptible(cmd, timeout):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return ("", LOCK_ERROR_PID_42, 1)
            return ("ok", "", 0)

        with patch.object(t, "_run_interruptible", side_effect=fake_run_interruptible), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t._shutdown, "wait", return_value=False) as mock_wait:
            t._run("bug", "show", "abc")

        # Should have called wait with exponential delays: 1, 2, 4
        delays = [call.kwargs["timeout"] for call in mock_wait.call_args_list]
        assert delays == [
            LOCK_RETRY_BASE_DELAY_S * (2 ** 0),  # 1s
            LOCK_RETRY_BASE_DELAY_S * (2 ** 1),  # 2s
            LOCK_RETRY_BASE_DELAY_S * (2 ** 2),  # 4s
        ]

    def test_shutdown_during_retry_returns_empty(self, tracker):
        """If shutdown fires during retry sleep, return immediately."""
        t = tracker

        with patch.object(t, "_run_interruptible",
                          return_value=("", LOCK_ERROR_PID_42, 1)), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t._shutdown, "wait", return_value=True):  # shutdown!
            result = t._run("bug", "show", "abc")

        assert result == ""


class TestStaleLockDetection:
    """After detecting a dead PID, stale locks are cleared and command retried."""

    def test_dead_pid_clears_lock_and_retries(self, tracker):
        """Dead PID triggers lock cleanup, then retries immediately."""
        t = tracker
        call_count = 0

        def fake_run_interruptible(cmd, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ("", LOCK_ERROR_PID_999, 1)
            return ("success", "", 0)

        with patch.object(t, "_run_interruptible", side_effect=fake_run_interruptible), \
             patch.object(t, "_pid_alive", return_value=False) as mock_alive, \
             patch.object(t, "_clear_stale_lock") as mock_clear:
            result = t._run("bug", "show", "abc")

        assert result == "success"
        mock_alive.assert_called_once_with(999)
        mock_clear.assert_called_once()
        assert call_count == 2

    def test_dead_pid_no_backoff_delay(self, tracker):
        """Stale lock retry is immediate — no wait() call."""
        t = tracker
        call_count = 0

        def fake_run_interruptible(cmd, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ("", LOCK_ERROR_PID_999, 1)
            return ("ok", "", 0)

        with patch.object(t, "_run_interruptible", side_effect=fake_run_interruptible), \
             patch.object(t, "_pid_alive", return_value=False), \
             patch.object(t, "_clear_stale_lock"), \
             patch.object(t._shutdown, "wait") as mock_wait:
            t._run("bug", "show", "abc")

        # wait should NOT be called — stale lock retry is immediate
        mock_wait.assert_not_called()

    def test_live_pid_lock_not_cleared(self, tracker):
        """When the locking process is alive, do NOT clear the lock."""
        t = tracker

        with patch.object(t, "_run_interruptible",
                          return_value=("", LOCK_ERROR_PID_42, 1)), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t, "_clear_stale_lock") as mock_clear, \
             patch.object(t._shutdown, "wait", return_value=False):
            t._run("bug", "show", "abc")

        mock_clear.assert_not_called()


class TestClearStaleLock:
    """_clear_stale_lock removes lock files under .git/."""

    def test_removes_git_bug_lock_files(self, tmp_path):
        """Lock files with 'git-bug' or 'bug' in path are removed."""
        git_dir = tmp_path / ".git" / "git-bug"
        git_dir.mkdir(parents=True)
        lock_file = git_dir / "git-bug-cache.lock"
        lock_file.touch()

        t = GitBugTracker(repo_dir=str(tmp_path))
        t._clear_stale_lock()

        assert not lock_file.exists()

    def test_ignores_unrelated_lock_files(self, tmp_path):
        """Lock files not related to git operations are left alone."""
        # Use a subdirectory with no 'bug' in its name to avoid false matches
        repo = tmp_path / "myrepo"
        git_dir = repo / ".git" / "refs"
        git_dir.mkdir(parents=True)
        lock_file = git_dir / "heads.lock"
        lock_file.touch()

        t = GitBugTracker(repo_dir=str(repo))
        t._clear_stale_lock()

        assert lock_file.exists()


class TestConstants:
    """Verify constants are importable and have expected values."""

    def test_lock_retry_attempts_value(self):
        assert LOCK_RETRY_ATTEMPTS == 6

    def test_lock_retry_base_delay_value(self):
        assert LOCK_RETRY_BASE_DELAY_S == 1

    def test_constants_used_in_run_loop(self, tracker):
        """_run iterates exactly LOCK_RETRY_ATTEMPTS times on persistent lock."""
        t = tracker
        calls = []

        def fake_run_interruptible(cmd, timeout):
            calls.append(1)
            return ("", LOCK_ERROR_PID_42, 1)

        with patch.object(t, "_run_interruptible", side_effect=fake_run_interruptible), \
             patch.object(t, "_pid_alive", return_value=True), \
             patch.object(t._shutdown, "wait", return_value=False):
            t._run("bug", "show", "abc")

        assert len(calls) == LOCK_RETRY_ATTEMPTS
