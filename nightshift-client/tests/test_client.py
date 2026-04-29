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
