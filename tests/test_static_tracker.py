"""Tests for StaticTracker — reads pre-dumped issue data from JSON files."""

import json
import os
import time
import pytest
from pathlib import Path

from core.constants import TRACKER_OUTBOX_FILENAME
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


def test_write_ops_append_to_outbox(session_dir):
    t = StaticTracker(session_dir=session_dir)
    t.add_comment("334b830", "test comment")
    t.set_status("334b830", "closed")
    t.add_label("334b830", "wip")
    t.remove_label("334b830", "wip")
    t.sync()  # sync is still a no-op

    outbox = session_dir / TRACKER_OUTBOX_FILENAME
    assert outbox.exists()
    lines = [json.loads(l) for l in outbox.read_text().strip().splitlines()]
    assert len(lines) == 4
    assert lines[0] == {"op": "comment", "issue_id": "334b830", "text": "test comment"}
    assert lines[1] == {"op": "set_status", "issue_id": "334b830", "status": "closed"}
    assert lines[2] == {"op": "label_add", "issue_id": "334b830", "label": "wip"}
    assert lines[3] == {"op": "label_rm", "issue_id": "334b830", "label": "wip"}


def test_empty_session_dir(tmp_path):
    t = StaticTracker(session_dir=tmp_path)
    assert t.get_issue("anything") is None
    assert t.list_issues() == []


def test_get_comments_returns_empty_without_comments(session_dir):
    t = StaticTracker(session_dir=session_dir)
    assert t.get_comments("334b830") == []


def test_get_comments_returns_loaded_comments(tmp_path):
    issue = {
        "id": "abc123",
        "title": "Test",
        "body": "",
        "status": "open",
        "comments": [
            {"author": "alice", "body": "first comment", "created_at": "2025-01-01"},
            {"author": "bob", "body": "second comment", "created_at": "2025-01-02"},
        ],
    }
    (tmp_path / "issue.json").write_text(json.dumps(issue))
    t = StaticTracker(session_dir=tmp_path)
    comments = t.get_comments("abc123")
    assert len(comments) == 2
    assert comments[0].author == "alice"
    assert comments[1].body == "second comment"


# ── reload() tests ───────────────────────────────────────────

def test_reload_no_change(session_dir):
    t = StaticTracker(session_dir=session_dir)
    new_comments = t.reload()
    assert new_comments == []


def test_reload_detects_new_comments(session_dir):
    t = StaticTracker(session_dir=session_dir)

    # Update issue.json with comments and bump mtime
    issue_path = session_dir / "issue.json"
    data = json.loads(issue_path.read_text())
    data["comments"] = [
        {"author": "human", "body": "Please also fix X", "created_at": "2025-03-01"},
    ]
    # Ensure mtime changes (some filesystems have 1s granularity)
    time.sleep(0.05)
    issue_path.write_text(json.dumps(data))
    # Force mtime to be clearly different
    os.utime(issue_path, (time.time() + 1, time.time() + 1))

    new_comments = t.reload()
    assert len(new_comments) == 1
    assert new_comments[0].author == "human"
    assert new_comments[0].body == "Please also fix X"


def test_reload_returns_only_new_comments(tmp_path):
    """Only comments added since the last reload are returned."""
    issue = {
        "id": "abc",
        "title": "T",
        "body": "",
        "status": "open",
        "comments": [
            {"author": "a", "body": "old comment", "created_at": None},
        ],
    }
    (tmp_path / "issue.json").write_text(json.dumps(issue))
    t = StaticTracker(session_dir=tmp_path)

    # First reload — issue already had 1 comment at load time, so no new ones
    assert t.reload() == []

    # Add a second comment
    issue["comments"].append({"author": "b", "body": "new one", "created_at": None})
    issue_path = tmp_path / "issue.json"
    issue_path.write_text(json.dumps(issue))
    os.utime(issue_path, (time.time() + 2, time.time() + 2))

    new = t.reload()
    assert len(new) == 1
    assert new[0].body == "new one"


def test_reload_missing_file(tmp_path):
    t = StaticTracker(session_dir=tmp_path)
    assert t.reload() == []


def test_reload_bad_json(session_dir):
    t = StaticTracker(session_dir=session_dir)
    issue_path = session_dir / "issue.json"
    issue_path.write_text("NOT JSON")
    os.utime(issue_path, (time.time() + 1, time.time() + 1))
    # Should not raise, just return empty
    assert t.reload() == []
