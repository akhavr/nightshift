"""Tests for the git-bug GraphQL tracker adapter."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.protocols import SHORT_ID_LEN, TrackerComment, TrackerIssue
from adapters.trackers.git_bug_graphql import GitBugGraphQLError


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


def bug_payload(issue_id="abc123", status="open"):
    return {
        "id": issue_id,
        "title": "Fix lock contention",
        "status": status,
        "createdAt": "2026-04-01T00:00:00Z",
        "lastEdit": "2026-04-02T00:00:00Z",
        "labels": {"nodes": [{"name": "nightshift"}, {"name": "bug"}]},
        "comments": {
            "nodes": [
                {
                    "author": {"displayName": "Alice"},
                    "message": "body text",
                    "createdAt": "2026-04-01T00:00:00Z",
                },
                {
                    "author": {"name": "Bob"},
                    "message": "follow up",
                    "createdAt": "2026-04-02T00:00:00Z",
                },
            ]
        },
    }


@pytest.fixture
def graphql_tracker(monkeypatch):
    from adapters.trackers import git_bug_graphql

    proc = FakeProcess()
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(git_bug_graphql.subprocess, "Popen", popen)
    monkeypatch.setattr(git_bug_graphql.requests, "head", MagicMock(return_value=FakeResponse()))
    return git_bug_graphql.GitBugGraphQLTracker(repo_dir="/repo"), popen, proc


def test_graphql_tracker_starts_webui_subprocess(graphql_tracker):
    tracker, popen, _proc = graphql_tracker

    popen.assert_called_once()
    cmd = popen.call_args.args[0]
    assert cmd[:4] == ["git-bug", "webui", "--no-open", "--host"]
    assert "127.0.0.1" in cmd
    assert "--port" in cmd
    assert popen.call_args.kwargs["cwd"] == "/repo"
    tracker.shutdown()


def test_graphql_tracker_waits_for_ready(monkeypatch):
    from adapters.trackers import git_bug_graphql

    proc = FakeProcess()
    monkeypatch.setattr(git_bug_graphql.subprocess, "Popen", MagicMock(return_value=proc))
    head = MagicMock(side_effect=[RuntimeError("not ready"), FakeResponse()])
    monkeypatch.setattr(git_bug_graphql.requests, "head", head)

    tracker = git_bug_graphql.GitBugGraphQLTracker(repo_dir="/repo")

    assert head.call_count == 2
    assert head.call_args.args[0].endswith("/graphql")
    tracker.shutdown()


def test_get_issue_returns_tracker_issue(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    monkeypatch.setattr(
        tracker,
        "_query",
        MagicMock(return_value={"repository": {"bug": bug_payload("abc123")}}),
    )

    issue = tracker.get_issue("abc123")

    assert issue == TrackerIssue(
        id="abc123",
        identifier="abc123"[:SHORT_ID_LEN],
        title="Fix lock contention",
        body="body text",
        status="open",
        labels=["nightshift", "bug"],
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-02T00:00:00Z",
    )
    tracker.shutdown()


def test_get_comments_returns_list(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    monkeypatch.setattr(
        tracker,
        "_query",
        MagicMock(return_value={"repository": {"bug": bug_payload("abc123")}}),
    )

    comments = tracker.get_comments("abc123")

    assert comments == [
        TrackerComment(
            author="Alice",
            body="body text",
            created_at="2026-04-01T00:00:00Z",
        ),
        TrackerComment(
            author="Bob",
            body="follow up",
            created_at="2026-04-02T00:00:00Z",
        ),
    ]
    tracker.shutdown()


def test_add_comment_sends_mutation(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={})
    monkeypatch.setattr(tracker, "_query", query)

    tracker.add_comment("abc123", "text")

    assert "bugAddComment" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "body": "text"}
    tracker.shutdown()


def test_create_issue_sends_mutation(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={"bugCreate": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    issue_id = tracker.create_issue("Fix lock contention", "body text")

    assert issue_id == "abc123"
    assert "bugCreate" in query.call_args.args[0]
    assert query.call_args.args[1] == {
        "title": "Fix lock contention",
        "message": "body text",
    }
    tracker.shutdown()


def test_add_label_sends_mutation(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={})
    monkeypatch.setattr(tracker, "_query", query)

    tracker.add_label("abc123", "foo")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": ["foo"], "removed": []}
    tracker.shutdown()


def test_remove_label_sends_mutation(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={})
    monkeypatch.setattr(tracker, "_query", query)

    tracker.remove_label("abc123", "foo")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert "Removed" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": [], "removed": ["foo"]}
    tracker.shutdown()


def test_set_status_open(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={})
    monkeypatch.setattr(tracker, "_query", query)

    tracker.set_status("abc123", "open")

    assert "bugStatusOpen" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123"}
    tracker.shutdown()


def test_set_status_closed(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    query = MagicMock(return_value={})
    monkeypatch.setattr(tracker, "_query", query)

    tracker.set_status("abc123", "closed")

    assert "bugStatusClose" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123"}
    tracker.shutdown()


def test_terminate_terminates_webui(graphql_tracker):
    tracker, _popen, proc = graphql_tracker

    tracker.terminate()

    assert proc.terminated is True
    assert proc.wait_calls == 1


def test_shutdown_aliases_terminate(graphql_tracker):
    tracker, _popen, proc = graphql_tracker

    tracker.shutdown()

    assert proc.terminated is True
    assert proc.wait_calls == 1


def test_list_issues_queries_all(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    monkeypatch.setattr(
        tracker,
        "_query",
        MagicMock(
            return_value={
                "repository": {
                    "allBugs": {
                        "nodes": [
                            bug_payload("abc123", "open"),
                            bug_payload("def456", "closed"),
                        ]
                    }
                }
            }
        ),
    )

    issues = tracker.list_issues()

    assert [issue.id for issue in issues] == ["abc123", "def456"]
    assert all(isinstance(issue, TrackerIssue) for issue in issues)
    tracker.shutdown()


def test_stale_cache_auto_recovery(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    bug = bug_payload("abc123", "open")
    bug_nodes = MagicMock(side_effect=[
        RuntimeError("git-bug GraphQL error: [{'message': \"bug doesn't exist\", 'path': ['repository', 'allBugs', 'nodes', 0, 'comments']}]"),
        [bug],
    ])
    monkeypatch.setattr(tracker, "_bug_nodes", bug_nodes)
    rebuild_cache = MagicMock()
    monkeypatch.setattr(tracker, "rebuild_cache", rebuild_cache)

    issues = tracker.list_issues()

    assert [issue.id for issue in issues] == ["abc123"]
    rebuild_cache.assert_called_once()
    assert bug_nodes.call_count == 2
    tracker.shutdown()


def test_stale_cache_recovery_logs_bug_id(graphql_tracker, monkeypatch, caplog):
    tracker, _popen, _proc = graphql_tracker
    bug = bug_payload("abc123", "open")
    error = GitBugGraphQLError(
        [{"message": "bug doesn't exist"}],
        {"repository": {"allBugs": {"nodes": [{"id": "abc123"}]}}},
    )
    bug_nodes = MagicMock(side_effect=[error, [bug]])
    monkeypatch.setattr(tracker, "_bug_nodes", bug_nodes)
    monkeypatch.setattr(tracker, "rebuild_cache", MagicMock())

    with caplog.at_level("WARNING"):
        tracker.list_issues()

    assert "bug=abc123" in caplog.text
    tracker.shutdown()


def test_query_posts_to_graphql(graphql_tracker, monkeypatch):
    tracker, _popen, _proc = graphql_tracker
    post = MagicMock(return_value=FakeResponse({"data": {"ok": True}}))
    monkeypatch.setattr("adapters.trackers.git_bug_graphql.requests.post", post)

    result = tracker._query("query { ok }", {"id": "abc123"})

    assert result == {"ok": True}
    post.assert_called_once_with(
        tracker.graphql_url,
        json={"query": "query { ok }", "variables": {"id": "abc123"}},
        timeout=tracker.request_timeout_s,
    )
    tracker.shutdown()


def test_run_raw_warns_when_locked(graphql_tracker, monkeypatch, caplog):
    """CLI fallback warns about lock conflict when webui is dead."""
    tracker, _popen, proc = graphql_tracker
    proc.poll = MagicMock(return_value=0)  # webui process is dead
    run = MagicMock(
        return_value=MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: the repository you want to access is already locked by the process pid 1234",
        )
    )
    monkeypatch.setattr("adapters.trackers.git_bug_graphql.subprocess.run", run)

    with caplog.at_level("WARNING"):
        result = tracker.run_raw("bug", "show", "abc123")

    assert result == ""
    assert "already locked" in caplog.text
    assert "webui" in caplog.text
    tracker.shutdown()


def test_run_raw_routes_through_graphql_when_alive(graphql_tracker, monkeypatch):
    """When webui is alive, run_raw routes supported commands through GraphQL."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugAddComment": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "comment", "new", "abc123", "-m", "hello world")

    assert "bugAddComment" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "body": "hello world"}
    tracker.shutdown()


def test_run_raw_falls_back_to_cli_when_dead(graphql_tracker, monkeypatch):
    """When webui is dead, run_raw falls back to CLI subprocess."""
    tracker, _popen, proc = graphql_tracker
    proc.poll = MagicMock(return_value=0)  # process exited
    run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="comment added", stderr="")
    )
    monkeypatch.setattr("adapters.trackers.git_bug_graphql.subprocess.run", run)

    result = tracker.run_raw("bug", "comment", "new", "abc123", "-m", "hello world")

    run.assert_called_once()
    assert result == "comment added"
    tracker.shutdown()


def test_run_raw_add_creates_issue_via_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug add' routes through GraphQL create_issue when alive."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugCreate": {"bug": {"id": "newbug123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "add", "-t", "Fix the bug", "-m", "Details here")

    assert "bugCreate" in query.call_args.args[0]
    assert query.call_args.args[1] == {"title": "Fix the bug", "message": "Details here"}
    assert "newbug123" in result
    tracker.shutdown()


def test_run_raw_label_new_via_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug label new' routes through GraphQL when alive."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugChangeLabels": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "label", "new", "abc123", "nightshift")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": ["nightshift"], "removed": []}
    tracker.shutdown()


def test_run_raw_label_rm_via_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug label rm' routes through GraphQL when alive."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugChangeLabels": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "label", "rm", "abc123", "nightshift")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": [], "removed": ["nightshift"]}
    tracker.shutdown()


def test_run_raw_status_close_via_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug status close' routes through GraphQL when alive."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugStatusClose": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "status", "close", "abc123")

    assert "bugStatusClose" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123"}
    tracker.shutdown()


def test_run_raw_status_open_via_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug status open' routes through GraphQL when alive."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugStatusOpen": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "status", "open", "abc123")

    assert "bugStatusOpen" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123"}
    tracker.shutdown()


def test_run_raw_unsupported_command_raises_when_webui_alive(graphql_tracker):
    """Unsupported commands raise error when webui is alive (no silent CLI fallback)."""
    tracker, _popen, proc = graphql_tracker

    with pytest.raises(RuntimeError) as exc:
        tracker.run_raw("bug", "select", "abc123")

    assert "not recognized" in str(exc.value)
    tracker.shutdown()


def test_is_webui_alive_returns_true_when_running(graphql_tracker):
    """_is_webui_alive returns True when webui process is running."""
    tracker, _popen, proc = graphql_tracker
    assert tracker._is_webui_alive() is True
    tracker.shutdown()


def test_is_webui_alive_returns_false_when_exited(graphql_tracker):
    """_is_webui_alive returns False when webui process has exited."""
    tracker, _popen, proc = graphql_tracker
    proc.poll = MagicMock(return_value=0)
    assert tracker._is_webui_alive() is False
    tracker.shutdown()


def test_label_add_alias_routes_to_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug label add' routes through GraphQL just like 'bug label new'."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugChangeLabels": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "label", "add", "abc123", "nightshift")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": ["nightshift"], "removed": []}
    tracker.shutdown()


def test_label_remove_alias_routes_to_graphql(graphql_tracker, monkeypatch):
    """run_raw 'bug label remove' routes through GraphQL just like 'bug label rm'."""
    tracker, _popen, proc = graphql_tracker
    query = MagicMock(return_value={"bugChangeLabels": {"bug": {"id": "abc123"}}})
    monkeypatch.setattr(tracker, "_query", query)

    result = tracker.run_raw("bug", "label", "remove", "abc123", "nightshift")

    assert "bugChangeLabels" in query.call_args.args[0]
    assert query.call_args.args[1] == {"id": "abc123", "added": [], "removed": ["nightshift"]}
    tracker.shutdown()


def test_cli_only_command_raises_clear_error(graphql_tracker):
    """CLI-only commands raise clear error when webui is alive."""
    tracker, _popen, proc = graphql_tracker

    with pytest.raises(RuntimeError) as exc:
        tracker.run_raw("pull")

    assert "no GraphQL equivalent" in str(exc.value)
    assert "webui" in str(exc.value)
    tracker.shutdown()


def test_unrecognized_command_raises_clear_error(graphql_tracker):
    """Unrecognized commands raise clear error when webui is alive."""
    tracker, _popen, proc = graphql_tracker

    with pytest.raises(RuntimeError) as exc:
        tracker.run_raw("bug", "unknown_cmd", "abc123")

    assert "not recognized" in str(exc.value)
    tracker.shutdown()


def test_parse_raw_command_bug_show(graphql_tracker):
    """_parse_raw_command parses 'bug show <id>' correctly."""
    tracker, _popen, _proc = graphql_tracker
    result = tracker._parse_raw_command(("bug", "show", "abc123"))
    assert result is not None
    method, args = result
    assert method == "show_issue"
    assert args == ("abc123", None)
    tracker.shutdown()


def test_parse_raw_command_bug_show_format_json(graphql_tracker):
    """_parse_raw_command parses 'bug show <id> -f json' correctly."""
    tracker, _popen, _proc = graphql_tracker
    result = tracker._parse_raw_command(("bug", "show", "abc123", "-f", "json"))
    assert result is not None
    method, args = result
    assert method == "show_issue"
    assert args == ("abc123", "json")
    tracker.shutdown()


def test_parse_raw_command_bug_show_format_long(graphql_tracker):
    """_parse_raw_command parses 'bug show <id> --format json' correctly."""
    tracker, _popen, _proc = graphql_tracker
    result = tracker._parse_raw_command(("bug", "show", "abc123", "--format", "json"))
    assert result is not None
    method, args = result
    assert method == "show_issue"
    assert args == ("abc123", "json")
    tracker.shutdown()


def test_graphql_router_bug_show_default(graphql_tracker, monkeypatch):
    """Router executes 'bug show' via GraphQL, returns default formatted output."""
    tracker, _popen, _proc = graphql_tracker
    monkeypatch.setattr(
        tracker,
        "_query",
        MagicMock(return_value={"repository": {"bug": bug_payload("abc123")}}),
    )

    result = tracker.run_raw("bug", "show", "abc123")

    assert "abc123" in result
    assert "Fix lock contention" in result
    assert "open" in result.lower()
    tracker.shutdown()


def test_graphql_router_bug_show_json(graphql_tracker, monkeypatch):
    """Router executes 'bug show -f json' via GraphQL, returns JSON output."""
    import json
    tracker, _popen, _proc = graphql_tracker
    monkeypatch.setattr(
        tracker,
        "_query",
        MagicMock(return_value={"repository": {"bug": bug_payload("abc123")}}),
    )

    result = tracker.run_raw("bug", "show", "abc123", "-f", "json")

    data = json.loads(result)
    assert data["id"] == "abc123"
    assert data["title"] == "Fix lock contention"
    assert data["status"] == "open"
    assert "labels" in data
    assert "comments" in data
    tracker.shutdown()
