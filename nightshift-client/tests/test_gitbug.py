"""Tests for git-bug CLI wrapper."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nightshift_client._gitbug import GitBug
from nightshift_client.exceptions import TrackerError


class TestGitBugAdd:
    """Tests for GitBug.add()."""

    def test_gitbug_add_creates_issue(self):
        """add() creates an issue and returns the issue ID."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            issue_id = gb.add("Test title", "Test body", ["bug", "urgent"])

        assert issue_id == "abc123def456"
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "git-bug"
        assert "add" in cmd
        assert "-t" in cmd or "--title" in cmd
        assert "Test title" in cmd
        assert "-m" in cmd or "--message" in cmd
        assert "Test body" in cmd
        assert "-l" in cmd
        assert call_args[1]["cwd"] == "/repo"

    def test_gitbug_add_without_labels(self):
        """add() works without labels."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "def789\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            issue_id = gb.add("Title", "Body")

        assert issue_id == "def789"
        cmd = mock_run.call_args[0][0]
        assert "-l" not in cmd

    def test_gitbug_add_raises_on_failure(self):
        """add() raises TrackerError on CLI failure."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "git-bug: error\n"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(TrackerError, match="git-bug.*failed"):
                gb.add("Title", "Body")


class TestGitBugComment:
    """Tests for GitBug.comment()."""

    def test_gitbug_comment_adds_comment(self):
        """comment() adds a comment to an issue."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            gb.comment("abc123", "My comment text")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git-bug"
        assert "comment" in cmd
        assert "new" in cmd
        assert "abc123" in cmd
        assert "-m" in cmd
        assert "My comment text" in cmd

    def test_gitbug_comment_raises_on_failure(self):
        """comment() raises TrackerError on CLI failure."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error\n"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(TrackerError, match="Failed to add comment"):
                gb.comment("abc123", "Comment")


class TestGitBugLabel:
    """Tests for GitBug.label()."""

    def test_gitbug_label_adds_label(self):
        """label() adds a label to an issue."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            gb.label("abc123", "nightshift")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git-bug"
        assert "label" in cmd
        assert "new" in cmd
        assert "abc123" in cmd
        assert "nightshift" in cmd

    def test_gitbug_label_ignores_already_set(self):
        """label() ignores 'already set' errors (rc=1 with specific message)."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "label already set\n"

        with patch("subprocess.run", return_value=mock_result):
            gb.label("abc123", "nightshift")


class TestGitBugRetry:
    """Tests for lock retry behavior."""

    def test_gitbug_retry_on_lock(self):
        """Operations retry on lock contention with exponential backoff."""
        gb = GitBug(repo_path="/repo")

        locked_result = MagicMock()
        locked_result.returncode = 1
        locked_result.stdout = ""
        locked_result.stderr = "already locked by the process pid 12345\n"

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = "abc123\n"
        success_result.stderr = ""

        with patch("subprocess.run", side_effect=[locked_result, success_result]) as mock_run:
            with patch("time.sleep") as mock_sleep:
                issue_id = gb.add("Title", "Body")

        assert issue_id == "abc123"
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(1)

    def test_gitbug_retry_exponential_backoff(self):
        """Lock retries use exponential backoff (1, 2, 4, 8...)."""
        gb = GitBug(repo_path="/repo")

        locked_result = MagicMock()
        locked_result.returncode = 1
        locked_result.stdout = ""
        locked_result.stderr = "already locked by the process pid 999\n"

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = "xyz789\n"
        success_result.stderr = ""

        with patch(
            "subprocess.run",
            side_effect=[locked_result, locked_result, locked_result, success_result],
        ) as mock_run:
            with patch("time.sleep") as mock_sleep:
                issue_id = gb.add("Title", "Body")

        assert issue_id == "xyz789"
        assert mock_run.call_count == 4
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)
        mock_sleep.assert_any_call(4)

    def test_gitbug_retry_exhausted(self):
        """Raises TrackerError after all retries exhausted."""
        gb = GitBug(repo_path="/repo")

        locked_result = MagicMock()
        locked_result.returncode = 1
        locked_result.stdout = ""
        locked_result.stderr = "already locked by the process pid 999\n"

        with patch("subprocess.run", return_value=locked_result):
            with patch("time.sleep"):
                with pytest.raises(TrackerError, match="lock contention"):
                    gb.add("Title", "Body")


class TestGitBugTimeout:
    """Tests for timeout handling."""

    def test_gitbug_timeout_raises_tracker_error(self):
        """Timeout raises TrackerError."""
        gb = GitBug(repo_path="/repo")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git-bug", 30)):
            with pytest.raises(TrackerError, match="timed out"):
                gb.add("Title", "Body")


class TestGitBugSync:
    """Tests for push/pull sync operations."""

    def test_gitbug_push_syncs(self):
        """push() runs git-bug push."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            gb.push()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["git-bug", "push"]
        assert mock_run.call_args[1]["cwd"] == "/repo"

    def test_gitbug_pull_fetches(self):
        """pull() runs git-bug pull."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            gb.pull()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["git-bug", "pull"]
        assert mock_run.call_args[1]["cwd"] == "/repo"


class TestGitBugList:
    """Tests for listing issues."""

    def test_gitbug_list_returns_issues(self):
        """list() returns parsed issue dicts from git-bug bug ls."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"id": "abc123", "title": "Bug one"}, {"id": "def456", "title": "Bug two"}]'
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            issues = gb.list()

        assert len(issues) == 2
        assert issues[0]["id"] == "abc123"
        assert issues[1]["title"] == "Bug two"
        cmd = mock_run.call_args[0][0]
        assert cmd == ["git-bug", "bug", "-f", "json"]

    def test_gitbug_list_with_label_filter(self):
        """list(labels=["foo"]) passes label filter to git-bug."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"id": "abc123", "title": "Labeled bug"}]'
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            issues = gb.list(labels=["nightshift", "urgent"])

        assert len(issues) == 1
        cmd = mock_run.call_args[0][0]
        assert "git-bug" in cmd
        assert "label:nightshift" in cmd
        assert "label:urgent" in cmd

    def test_gitbug_list_empty_returns_empty_list(self):
        """list() returns empty list when no issues."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            issues = gb.list()

        assert issues == []

    def test_gitbug_list_handles_invalid_json(self):
        """list() raises TrackerError on invalid JSON output."""
        gb = GitBug(repo_path="/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not valid json"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(TrackerError, match="parse"):
                gb.list()
