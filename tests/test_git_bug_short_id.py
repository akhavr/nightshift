"""Tests for GitBugTracker short ID truncation.

git-bug only accepts short prefix IDs (7-12 chars), not full 64-char hashes.
GitBugTracker must truncate issue_id before passing to git-bug CLI commands.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.trackers.git_bug import GitBugTracker
from core.protocols import SHORT_ID_LEN

FULL_HASH = "ddabba1413cc6b8e1b3e2bb2308e5e5b8e8e994b6c7b7bb8e29b879dc5f6bfb9"
SHORT_HASH = FULL_HASH[:SHORT_ID_LEN]


class TestShortIdTruncation:
    """All git-bug CLI calls must use the short ID, not the full hash."""

    def _make_tracker(self):
        return GitBugTracker(repo_dir="/tmp/fake")

    def test_short_static_method(self):
        assert GitBugTracker._short(FULL_HASH) == SHORT_HASH

    def test_short_already_short(self):
        """If ID is already short, _short() returns it unchanged."""
        assert GitBugTracker._short(SHORT_HASH) == SHORT_HASH

    def test_set_status_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run") as mock_run:
            t.set_status(FULL_HASH, "closed")
        mock_run.assert_called_once_with("bug", "status", "close", SHORT_HASH)

    def test_add_comment_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run") as mock_run:
            t.add_comment(FULL_HASH, "Done")
        mock_run.assert_called_once_with("bug", "comment", "new", SHORT_HASH, "-m", "Done")

    def test_add_label_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run") as mock_run:
            t.add_label(FULL_HASH, "nightshift")
        mock_run.assert_called_once_with(
            "bug", "label", "new", SHORT_HASH, "nightshift", ignore_rc={1},
        )

    def test_remove_label_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run") as mock_run:
            t.remove_label(FULL_HASH, "nightshift")
        mock_run.assert_called_once_with(
            "bug", "label", "rm", SHORT_HASH, "nightshift", ignore_rc={1},
        )

    def test_get_issue_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run", return_value="") as mock_run:
            t.get_issue(FULL_HASH)
        mock_run.assert_called_once_with("bug", "show", SHORT_HASH, "-f", "json")

    def test_get_comments_uses_short_id(self):
        t = self._make_tracker()
        with patch.object(t, "_run", return_value="") as mock_run:
            t.get_comments(FULL_HASH)
        mock_run.assert_called_once_with("bug", "show", SHORT_HASH, "-f", "json")
