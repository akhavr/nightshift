"""Tests for core/tracker_ipc.py — IPC protocol serialization and execution."""

import json
import pytest

from core.protocols import TrackerIssue, TrackerComment
from core.tracker_ipc import (
    TrackerRequest, TrackerResponse,
    serialize_tracker_issue, deserialize_tracker_issue,
    serialize_tracker_comment, deserialize_tracker_comment,
    execute_tracker_method,
)
from tests.conftest import MockTracker, make_test_issue


class TestTrackerRequest:
    def test_round_trip(self):
        req = TrackerRequest(method="get_issue", args={"issue_id": "abc"}, id="test-1")
        json_str = req.to_json()
        parsed = TrackerRequest.from_json(json_str)
        assert parsed.method == "get_issue"
        assert parsed.args == {"issue_id": "abc"}
        assert parsed.id == "test-1"

    def test_auto_id(self):
        req = TrackerRequest(method="sync")
        assert req.id  # UUID is auto-generated

    def test_json_valid(self):
        req = TrackerRequest(method="add_comment", args={"issue_id": "x", "body": "hello"})
        data = json.loads(req.to_json())
        assert data["method"] == "add_comment"
        assert data["args"]["body"] == "hello"


class TestTrackerResponse:
    def test_round_trip_ok(self):
        resp = TrackerResponse(id="r1", ok=True, result={"id": "abc"})
        parsed = TrackerResponse.from_json(resp.to_json())
        assert parsed.ok is True
        assert parsed.result == {"id": "abc"}
        assert parsed.error == ""

    def test_round_trip_error(self):
        resp = TrackerResponse(id="r2", ok=False, error="not found")
        parsed = TrackerResponse.from_json(resp.to_json())
        assert parsed.ok is False
        assert parsed.error == "not found"


class TestSerialization:
    def test_serialize_issue(self):
        issue = make_test_issue(issue_id="abc123", title="Test issue")
        d = serialize_tracker_issue(issue)
        assert d["id"] == "abc123"
        assert d["title"] == "Test issue"

    def test_deserialize_issue(self):
        d = {"id": "abc", "identifier": "abc", "title": "T", "body": "B",
             "status": "open", "labels": ["bug"]}
        issue = deserialize_tracker_issue(d)
        assert isinstance(issue, TrackerIssue)
        assert issue.title == "T"
        assert issue.labels == ["bug"]

    def test_serialize_none(self):
        assert serialize_tracker_issue(None) is None

    def test_deserialize_none(self):
        assert deserialize_tracker_issue(None) is None

    def test_serialize_comment(self):
        c = TrackerComment(author="alice", body="lgtm")
        d = serialize_tracker_comment(c)
        assert d["author"] == "alice"
        assert d["body"] == "lgtm"

    def test_deserialize_comment(self):
        d = {"author": "bob", "body": "fix typo"}
        c = deserialize_tracker_comment(d)
        assert isinstance(c, TrackerComment)
        assert c.author == "bob"


class TestExecuteTrackerMethod:
    def setup_method(self):
        issue = make_test_issue(issue_id="i1", title="Bug")
        self.tracker = MockTracker(issues={"i1": issue})
        self.tracker.comments["i1"] = [TrackerComment(author="a", body="hello")]

    def test_get_issue(self):
        req = TrackerRequest(method="get_issue", args={"issue_id": "i1"}, id="t1")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert resp.result["title"] == "Bug"

    def test_get_issue_not_found(self):
        req = TrackerRequest(method="get_issue", args={"issue_id": "nope"}, id="t2")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert resp.result is None

    def test_list_issues(self):
        req = TrackerRequest(method="list_issues", args={}, id="t3")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert len(resp.result) == 1

    def test_list_issues_with_status(self):
        req = TrackerRequest(method="list_issues", args={"status": "closed"}, id="t4")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert len(resp.result) == 0

    def test_get_comments(self):
        req = TrackerRequest(method="get_comments", args={"issue_id": "i1"}, id="t5")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert len(resp.result) == 1
        assert resp.result[0]["body"] == "hello"

    def test_add_comment(self):
        req = TrackerRequest(method="add_comment",
                             args={"issue_id": "i1", "body": "new"}, id="t6")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert len(self.tracker.comments["i1"]) == 2

    def test_set_status(self):
        req = TrackerRequest(method="set_status",
                             args={"issue_id": "i1", "status": "closed"}, id="t7")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert self.tracker.issues["i1"].status == "closed"

    def test_add_label(self):
        req = TrackerRequest(method="add_label",
                             args={"issue_id": "i1", "label": "urgent"}, id="t8")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert "urgent" in self.tracker.issues["i1"].labels

    def test_remove_label(self):
        self.tracker.issues["i1"].labels.append("remove-me")
        req = TrackerRequest(method="remove_label",
                             args={"issue_id": "i1", "label": "remove-me"}, id="t9")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert "remove-me" not in self.tracker.issues["i1"].labels

    def test_sync(self):
        req = TrackerRequest(method="sync", args={}, id="t10")
        resp = execute_tracker_method(self.tracker, req)
        assert resp.ok
        assert self.tracker.synced == 1

    def test_unknown_method(self):
        req = TrackerRequest(method="bogus", args={}, id="t11")
        resp = execute_tracker_method(self.tracker, req)
        assert not resp.ok
        assert "Unknown method" in resp.error

    def test_exception_handling(self):
        """Exceptions in tracker methods produce error responses, not crashes."""
        class BrokenTracker:
            def get_issue(self, issue_id):
                raise RuntimeError("disk on fire")

        req = TrackerRequest(method="get_issue", args={"issue_id": "x"}, id="t12")
        resp = execute_tracker_method(BrokenTracker(), req)
        assert not resp.ok
        assert "disk on fire" in resp.error
