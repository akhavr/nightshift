"""Tests for GitBugTracker.sync() remote detection."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.trackers.git_bug import GitBugTracker


class TestSyncRemoteDetection:
    """sync() should detect missing remotes and stop retrying."""

    def _make_tracker(self):
        return GitBugTracker(repo_dir="/tmp/fake", sync=True)

    def test_sync_is_disabled_by_default(self):
        """sync() is a no-op unless explicitly enabled."""
        t = GitBugTracker(repo_dir="/tmp/fake")
        with patch.object(t, "_run_interruptible") as mock_ri, \
             patch.object(t, "_run") as mock_run:
            t.sync()
        mock_ri.assert_not_called()
        mock_run.assert_not_called()

    def test_sync_skips_when_no_remote_pull_error(self):
        """sync() detects 'remote not found' and skips all future syncs."""
        t = self._make_tracker()
        with patch.object(t, "_run_interruptible") as mock_ri, \
             patch.object(t, "_run") as mock_run:
            mock_ri.return_value = ("", "Error: remote not found", 1)
            t.sync()
        assert t._has_remote is False
        mock_run.assert_not_called()

    def test_sync_skips_when_unable_to_resolve_url(self):
        """sync() detects 'unable to resolve URL for remote' and skips."""
        t = self._make_tracker()
        with patch.object(t, "_run_interruptible") as mock_ri, \
             patch.object(t, "_run") as mock_run:
            mock_ri.return_value = ("", "Error: unable to resolve URL for remote: <nil>", 1)
            t.sync()
        assert t._has_remote is False
        mock_run.assert_not_called()

    def test_sync_proceeds_when_remote_exists(self):
        """sync() runs pull+push when remote is available."""
        t = self._make_tracker()
        with patch.object(t, "_run_interruptible") as mock_ri, \
             patch.object(t, "_run") as mock_run:
            mock_ri.return_value = ("ok", "", 0)
            t.sync()
        assert t._has_remote is True
        # push should have been called after successful probe
        mock_run.assert_called_once_with("push")

    def test_sync_caches_no_remote(self):
        """After detecting no remote, subsequent sync() calls are no-ops."""
        t = self._make_tracker()
        t._has_remote = False
        with patch.object(t, "_run_interruptible") as mock_ri, \
             patch.object(t, "_run") as mock_run:
            t.sync()
            t.sync()
            t.sync()
        mock_ri.assert_not_called()
        mock_run.assert_not_called()

    def test_sync_caches_has_remote(self):
        """After detecting remote, subsequent syncs use normal pull+push."""
        t = self._make_tracker()
        t._has_remote = True
        with patch.object(t, "_run") as mock_run:
            t.sync()
        assert mock_run.call_count == 2
        mock_run.assert_any_call("pull")
        mock_run.assert_any_call("push")
