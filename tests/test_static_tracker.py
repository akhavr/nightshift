"""Tests for StaticTracker — reads pre-dumped issue data from JSON files."""

import json
import pytest
from pathlib import Path

from core.protocols import TrackerIssue
from adapters.trackers.static import StaticTracker


@pytest.fixture
def session_dir(tmp_path):
    """Create a session dir with issue.json and issues.json."""
    issue = {
        "id": "334b830",
        "identifier": "334b830",
        "title": "Test issue",
        "body": "Test body",
        "status": "open",
        "labels": ["bug"],
        "url": None,
        "priority": None,
        "created_at": None,
        "updated_at": None,
    }
    other_issue = {
        "id": "abc1234def567890",
        "identifier": "abc1234def56",
        "title": "Other issue",
        "body": "Other body",
        "status": "closed",
        "labels": [],
        "url": None,
        "priority": None,
        "created_at": None,
        "updated_at": None,
    }
    (tmp_path / "issue.json").write_text(json.dumps(issue))
    (tmp_path / "issues.json").write_text(json.dumps([issue, other_issue]))
    return tmp_path


def test_get_issue_exact_id(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issue = t.get_issue("334b830")
    assert issue is not None
    assert issue.title == "Test issue"


def test_get_issue_prefix_match(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issue = t.get_issue("334b")
    assert issue is not None
    assert issue.id == "334b830"


def test_get_issue_full_long_id(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issue = t.get_issue("abc1234def567890")
    assert issue is not None
    assert issue.title == "Other issue"


def test_get_issue_by_identifier(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issue = t.get_issue("abc1234def56")
    assert issue is not None
    assert issue.title == "Other issue"


def test_get_issue_not_found(session_dir):
    t = StaticTracker(session_dir=session_dir)
    assert t.get_issue("nonexistent") is None


def test_list_issues_all(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issues = t.list_issues()
    assert len(issues) == 2


def test_list_issues_by_status(session_dir):
    t = StaticTracker(session_dir=session_dir)
    assert len(t.list_issues("open")) == 1
    assert len(t.list_issues("closed")) == 1
    assert len(t.list_issues(["open", "closed"])) == 2


def test_write_ops_are_noop(session_dir):
    t = StaticTracker(session_dir=session_dir)
    # These should not raise
    t.add_comment("334b830", "test comment")
    t.set_status("334b830", "closed")
    t.add_label("334b830", "wip")
    t.remove_label("334b830", "wip")
    t.sync()


def test_empty_session_dir(tmp_path):
    t = StaticTracker(session_dir=tmp_path)
    assert t.get_issue("anything") is None
    assert t.list_issues() == []


def test_get_comments_returns_empty(session_dir):
    t = StaticTracker(session_dir=session_dir)
    assert t.get_comments("334b830") == []
