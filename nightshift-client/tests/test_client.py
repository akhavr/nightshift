"""Tests for NightshiftClient."""

from unittest.mock import MagicMock, patch

import pytest

from nightshift_client import NightshiftClient
from nightshift_client.exceptions import AuthError


class TestClientInit:
    """Tests for NightshiftClient initialization."""

    def test_client_init_requires_identity(self):
        """NightshiftClient raises AuthError when identity is not provided."""
        with pytest.raises(AuthError, match="identity"):
            NightshiftClient(repo_path="/repo", identity="")

        with pytest.raises(AuthError, match="identity"):
            NightshiftClient(repo_path="/repo", identity=None)

    def test_client_init_stores_identity(self):
        """NightshiftClient stores identity and repo_path."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")
        assert client.repo_path == "/repo"
        assert client.identity == "user@example.com"


class TestCreateIssue:
    """Tests for NightshiftClient.create_issue()."""

    def test_create_issue_returns_id(self):
        """create_issue() returns the issue ID from GitBug."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "add", return_value="abc123def456") as mock_add:
            issue_id = client.create_issue("Test title", "Test body")

        assert issue_id == "abc123def456"
        mock_add.assert_called_once()

    def test_create_issue_adds_nightshift_label(self):
        """create_issue() automatically adds 'nightshift' label."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "add", return_value="abc123") as mock_add:
            client.create_issue("Title", "Body")

        call_args = mock_add.call_args
        labels = call_args.kwargs.get("labels", [])
        assert "nightshift" in labels

    def test_create_issue_adds_nightshift_to_existing_labels(self):
        """create_issue() adds 'nightshift' to user-provided labels."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "add", return_value="abc123") as mock_add:
            client.create_issue("Title", "Body", labels=["bug", "urgent"])

        labels = mock_add.call_args.kwargs.get("labels", [])
        assert "nightshift" in labels
        assert "bug" in labels
        assert "urgent" in labels

    def test_create_issue_does_not_duplicate_nightshift_label(self):
        """create_issue() doesn't duplicate 'nightshift' if already present."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "add", return_value="abc123") as mock_add:
            client.create_issue("Title", "Body", labels=["nightshift", "bug"])

        labels = mock_add.call_args.kwargs.get("labels", [])
        assert labels.count("nightshift") == 1


class TestPush:
    """Tests for NightshiftClient.push()."""

    def test_push_calls_gitbug(self):
        """push() delegates to GitBug.push()."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "push") as mock_push:
            client.push()

        mock_push.assert_called_once()


class TestCheckState:
    """Tests for NightshiftClient.check_state()."""

    def test_check_state_returns_status(self):
        """check_state() returns the state derived from labels."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift", "status:working"],
            "comments": [],
        }
        with patch.object(client._gitbug, "pull") as mock_pull, \
             patch.object(client._gitbug, "show", return_value=mock_issue):
            state = client.check_state("abc123")

        assert state == "working"

    def test_check_state_fetches_first(self):
        """check_state() calls pull() before reading state."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        call_order = []
        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift"],
            "comments": [],
        }

        def track_pull():
            call_order.append("pull")

        def track_show(issue_id):
            call_order.append("show")
            return mock_issue

        with patch.object(client._gitbug, "pull", side_effect=track_pull), \
             patch.object(client._gitbug, "show", side_effect=track_show):
            client.check_state("abc123")

        assert call_order == ["pull", "show"]


class TestGetIssueInfo:
    """Tests for NightshiftClient.get_issue_info()."""

    def test_get_issue_info_returns_dict(self):
        """get_issue_info() returns dict with expected keys."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift", "status:reviewing"],
            "comments": [],
            "create_time": "2026-04-29T10:00:00Z",
            "edit_time": "2026-04-29T12:00:00Z",
        }
        with patch.object(client._gitbug, "pull"), \
             patch.object(client._gitbug, "show", return_value=mock_issue):
            info = client.get_issue_info("abc123")

        assert isinstance(info, dict)
        assert info["state"] == "reviewing"
        assert info["labels"] == ["nightshift", "status:reviewing"]
        assert "last_comment" in info
        assert "updated_at" in info

    def test_get_issue_info_includes_last_comment(self):
        """get_issue_info() includes the last comment text."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift"],
            "comments": [
                {"message": "First comment", "timestamp": "2026-04-29T10:00:00Z"},
                {"message": "Latest comment", "timestamp": "2026-04-29T12:00:00Z"},
            ],
            "create_time": "2026-04-29T09:00:00Z",
            "edit_time": "2026-04-29T12:00:00Z",
        }
        with patch.object(client._gitbug, "pull"), \
             patch.object(client._gitbug, "show", return_value=mock_issue):
            info = client.get_issue_info("abc123")

        assert info["last_comment"] == "Latest comment"


class TestGetPendingQuestion:
    """Tests for NightshiftClient.get_pending_question()."""

    def test_get_pending_question_returns_text(self):
        """get_pending_question() returns question text when needs-human-input label present."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift", "needs-human-input"],
            "comments": [
                {"message": "First comment", "timestamp": "2026-04-29T10:00:00Z"},
                {"message": "What is the API key?", "timestamp": "2026-04-29T12:00:00Z"},
            ],
        }
        with patch.object(client._gitbug, "pull"), \
             patch.object(client._gitbug, "show", return_value=mock_issue):
            question = client.get_pending_question("abc123")

        assert question == "What is the API key?"

    def test_get_pending_question_none_if_no_question(self):
        """get_pending_question() returns None when no needs-human-input label."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        mock_issue = {
            "id": "abc123",
            "labels": ["nightshift", "status:working"],
            "comments": [
                {"message": "Some comment", "timestamp": "2026-04-29T10:00:00Z"},
            ],
        }
        with patch.object(client._gitbug, "pull"), \
             patch.object(client._gitbug, "show", return_value=mock_issue):
            question = client.get_pending_question("abc123")

        assert question is None


class TestPostAnswer:
    """Tests for NightshiftClient.post_answer()."""

    def test_post_answer_adds_comment(self):
        """post_answer() adds comment via GitBug."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "comment") as mock_comment:
            client.post_answer("abc123", "The API key is 12345")

        mock_comment.assert_called_once_with("abc123", "The API key is 12345")

    def test_post_answer_uses_identity(self):
        """post_answer() uses the configured identity (no explicit identity param needed)."""
        client = NightshiftClient(repo_path="/repo", identity="user@example.com")

        with patch.object(client._gitbug, "comment") as mock_comment:
            client.post_answer("abc123", "Answer here")

        mock_comment.assert_called_once()
        assert client.identity == "user@example.com"
