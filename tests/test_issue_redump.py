"""Tests for host/issue_dump.py — redump_issue for live sync."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import asdict

from core.protocols import TrackerIssue, TrackerComment
from host.issue_dump import redump_issue


@pytest.fixture
def mock_tracker():
    tracker = MagicMock()
    tracker.get_issue.return_value = TrackerIssue(
        id="issue-1", identifier="issue-1", title="Bug", body="desc",
        status="open", labels=["bug"],
    )
    tracker.get_comments.return_value = [
        TrackerComment(author="alice", body="comment 1", created_at="2025-01-01"),
        TrackerComment(author="bob", body="comment 2", created_at="2025-01-02"),
    ]
    return tracker


def test_redump_writes_issue_with_comments(tmp_path, mock_tracker):
    assert redump_issue(mock_tracker, "issue-1", tmp_path) is True
    data = json.loads((tmp_path / "issue.json").read_text())
    assert data["id"] == "issue-1"
    assert data["title"] == "Bug"
    assert len(data["comments"]) == 2
    assert data["comments"][0]["author"] == "alice"
    assert data["comments"][1]["body"] == "comment 2"


def test_redump_atomic_write(tmp_path, mock_tracker):
    """The .tmp file should not persist — rename is atomic."""
    redump_issue(mock_tracker, "issue-1", tmp_path)
    assert not (tmp_path / "issue.json.tmp").exists()
    assert (tmp_path / "issue.json").exists()


def test_redump_issue_not_found(tmp_path):
    tracker = MagicMock()
    tracker.get_issue.return_value = None
    assert redump_issue(tracker, "nonexistent", tmp_path) is False
    assert not (tmp_path / "issue.json").exists()


def test_redump_overwrites_existing(tmp_path, mock_tracker):
    (tmp_path / "issue.json").write_text('{"old": true}')
    redump_issue(mock_tracker, "issue-1", tmp_path)
    data = json.loads((tmp_path / "issue.json").read_text())
    assert data["id"] == "issue-1"
    assert "old" not in data


def test_redump_comments_fetch_failure(tmp_path):
    """If get_comments fails, issue is still written with empty comments."""
    tracker = MagicMock()
    tracker.get_issue.return_value = TrackerIssue(
        id="issue-1", identifier="issue-1", title="T", body="B", status="open",
    )
    tracker.get_comments.side_effect = RuntimeError("tracker error")
    assert redump_issue(tracker, "issue-1", tmp_path) is True
    data = json.loads((tmp_path / "issue.json").read_text())
    assert data["comments"] == []
