"""Tests that assistant text blocks are written to conversation.jsonl.

Verifies the fix for: @nightshift commands in assistant output must appear
in conversation.jsonl so that _extract_reviewer_verdict() can find them.
"""

import json
from pathlib import Path

import pytest

from core.protocols import AgentEvent, AgentEventType, TrackerIssue, Workspace
from core.state import StateManager, SessionState
from core.session import SessionRunner
from tests.conftest import MockAgent, MockTracker, MockNotifier, MockWorkspaceManager, make_test_issue


def _setup_session(tmp_path, events):
    """Create a SessionRunner with given agent events and return (runner, state_mgr)."""
    issue = make_test_issue()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    state_mgr = StateManager(session_dir)
    # Initialize state file
    state = SessionState(issue_id=issue.id, branch="agent/test")
    state_mgr._write(state)

    agent = MockAgent(events)
    tracker = MockTracker({issue.id: issue})
    notifier = MockNotifier()
    workspace_mgr = MockWorkspaceManager(tmp_path)

    runner = SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=workspace_mgr, state_mgr=state_mgr,
        issue=issue, prompt="Fix the bug",
    )
    workspace = Workspace(path=tmp_path / "ws", branch="agent/test", is_new=False)
    (tmp_path / "ws").mkdir()
    return runner, state_mgr, workspace


def test_assistant_text_written_to_conversation_log(tmp_path):
    """Assistant text blocks should appear in conversation.jsonl."""
    events = [
        AgentEvent(type=AgentEventType.TEXT, content="Hello, I'll fix the bug.", raw="raw1"),
        AgentEvent(type=AgentEventType.TEXT, content="@@LOG@@ Working on it", raw="raw2"),
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@", raw="raw3"),
    ]
    runner, state_mgr, workspace = _setup_session(tmp_path, events)
    runner.run(workspace=workspace)

    entries = [json.loads(line) for line in state_mgr.conversation_log.read_text().strip().splitlines()]
    assistant_entries = [e for e in entries if e["role"] == "assistant"]

    assert len(assistant_entries) == 3
    assert assistant_entries[0]["content"] == "Hello, I'll fix the bug."


def test_nightshift_command_in_conversation_log(tmp_path):
    """@nightshift approve/revise commands in assistant text must be in conversation.jsonl."""
    events = [
        AgentEvent(type=AgentEventType.TEXT, content="@@LOG@@ Reviewing code", raw="raw1"),
        AgentEvent(
            type=AgentEventType.TEXT,
            content="Tests pass. Code looks good. @nightshift approve",
            raw="raw2",
        ),
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@", raw="raw3"),
    ]
    runner, state_mgr, workspace = _setup_session(tmp_path, events)
    runner.run(workspace=workspace)

    entries = [json.loads(line) for line in state_mgr.conversation_log.read_text().strip().splitlines()]

    # Find entries containing @nightshift
    nightshift_entries = [e for e in entries if "@nightshift" in e.get("content", "")]
    assert len(nightshift_entries) >= 1
    assert any(e["role"] == "assistant" for e in nightshift_entries)


def test_reviewer_verdict_extractable_from_assistant_text(tmp_path):
    """End-to-end: _extract_reviewer_verdict should find @nightshift in assistant entries."""
    from host.watcher import HostWatcher

    events = [
        AgentEvent(type=AgentEventType.TEXT, content="@@LOG@@ Checking tests", raw="raw1"),
        AgentEvent(
            type=AgentEventType.TEXT,
            content="All tests pass. Code quality is good.\n\n@nightshift approve",
            raw="raw2",
        ),
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@", raw="raw3"),
    ]
    runner, state_mgr, workspace = _setup_session(tmp_path, events)
    runner.run(workspace=workspace)

    # Now use the watcher's extraction on the resulting conversation.jsonl
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()
    watcher = HostWatcher(sessions, repo, auto_start=False)

    verdict = watcher._extract_reviewer_verdict(state_mgr.conversation_log, "test-001")
    assert verdict == "approve"


def test_revise_verdict_extractable(tmp_path):
    """_extract_reviewer_verdict should find @nightshift revise in assistant entries."""
    from host.watcher import HostWatcher

    events = [
        AgentEvent(
            type=AgentEventType.TEXT,
            content="Error handling is missing. @nightshift revise",
            raw="raw1",
        ),
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@", raw="raw2"),
    ]
    runner, state_mgr, workspace = _setup_session(tmp_path, events)
    runner.run(workspace=workspace)

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()
    watcher = HostWatcher(sessions, repo, auto_start=False)

    verdict = watcher._extract_reviewer_verdict(state_mgr.conversation_log, "test-001")
    assert verdict == "revise"
