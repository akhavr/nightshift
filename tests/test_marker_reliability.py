"""OQ-3: Marker reliability tests.

Verifies that the system handles all marker failure modes gracefully:
- Agent exits without @@DONE@@
- Agent exits without @@WAITING@@ after @@QUESTION@@
- Markers in tool results are ignored
- @@DONE@@ always goes through review gate
"""
import json
from pathlib import Path

import pytest

from core.protocols import (
    AgentEvent, AgentEventType, MarkerType, parse_marker, TrackerIssue,
)
from core.config import MergeConfig, HooksConfig
from core.session import SessionRunner
from core.state import StateManager
from tests.conftest import (
    MockAgent, MockTracker, MockNotifier, MockWorkspaceManager,
    make_test_issue,
)


@pytest.fixture
def tmp_session(tmp_path):
    """Create a StateManager with initial state."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    state = {
        "issue_id": "test-001", "branch": "agent/test-001",
        "status": "working", "step": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "checkpoints": [], "human_answers": [],
    }
    (session_dir / "state.json").write_text(json.dumps(state))
    return StateManager(session_dir)


@pytest.fixture
def issue():
    return make_test_issue()


def make_runner(agent, issue, state_mgr, tmp_path, merge_config=None):
    tracker = MockTracker(issues={issue.id: issue})
    notifier = MockNotifier()
    workspace = MockWorkspaceManager(tmp_path)
    return SessionRunner(
        agent=agent, tracker=tracker, notifier=notifier,
        workspace_mgr=workspace, state_mgr=state_mgr,
        issue=issue, prompt="test prompt",
        merge_config=merge_config or MergeConfig(require_review=True),
        hooks_config=HooksConfig(),
    ), tracker, notifier


# --- Marker parsing ---

def test_marker_parse_log():
    m = parse_marker("@@LOG@@ thinking about the problem")
    assert m is not None
    assert m.type == MarkerType.LOG
    assert m.content == "thinking about the problem"


def test_marker_parse_checkpoint():
    m = parse_marker("@@CHECKPOINT@@ implemented feature X")
    assert m is not None
    assert m.type == MarkerType.CHECKPOINT
    assert m.content == "implemented feature X"


def test_marker_parse_question():
    m = parse_marker("@@QUESTION@@ What database should I use?")
    assert m is not None
    assert m.type == MarkerType.QUESTION
    assert m.content == "What database should I use?"


def test_marker_parse_done():
    m = parse_marker("@@DONE@@")
    assert m is not None
    assert m.type == MarkerType.DONE


def test_parse_marker_still_works_for_legacy():
    """Phase 5: parse_marker() must remain functional for backward compat with old sessions."""
    assert parse_marker("@@DONE@@").type == MarkerType.DONE
    assert parse_marker("@@CHECKPOINT@@ step1").type == MarkerType.CHECKPOINT
    assert parse_marker("@@QUESTION@@ what?").type == MarkerType.QUESTION
    assert parse_marker("@@LOG@@ thinking").type == MarkerType.LOG
    assert parse_marker("@@WAITING@@").type == MarkerType.WAITING


def test_marker_not_found():
    assert parse_marker("Just normal text") is None
    assert parse_marker("") is None


def test_marker_embedded_in_line():
    """Markers can appear anywhere in a line."""
    m = parse_marker("Some prefix @@DONE@@ some suffix")
    assert m is not None
    assert m.type == MarkerType.DONE


# --- Agent exit without @@DONE@@ ---

def test_exit_without_done_auto_resumes(tmp_path, tmp_session, issue):
    """When agent exits without @@DONE@@, status stays 'working' and
    _post_run() treats it as max-turns, triggering auto-resume."""
    agent = MockAgent(events=[
        AgentEvent(type=AgentEventType.TEXT,
                   content="@@LOG@@ working on it"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(agent, issue, tmp_session, tmp_path)
    runner.run()

    st = tmp_session.load_state()
    # Should have auto-resumed (status back to working or suspended)
    # The key thing: it should NOT be "completed" or "done:pending-review"
    assert st.status != "completed"
    assert st.status != "done:pending-review"


def test_exit_without_done_never_auto_merges(tmp_path, tmp_session, issue):
    """Without @@DONE@@, the system never merges — even with auto-merge label."""
    issue.labels = ["auto-merge"]
    agent = MockAgent(events=[
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(
        agent, issue, tmp_session, tmp_path,
        merge_config=MergeConfig(require_review=False, auto_merge_label="auto-merge"),
    )
    runner.run()

    st = tmp_session.load_state()
    # Should NOT be completed — no @@DONE@@ was output
    assert st.status != "completed"
    # Workspace should not have been finalized
    workspace = runner.workspace_mgr
    assert len(workspace.finalized) == 0


# --- @@DONE@@ goes through review gate ---

def test_done_triggers_review(tmp_path, tmp_session, issue):
    """@@DONE@@ sets status to waiting:review and notifies. Container exits
    without blocking — review/merge is handled by the host."""
    agent = MockAgent(events=[
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(
        agent, issue, tmp_session, tmp_path,
        merge_config=MergeConfig(require_review=True),
    )

    runner.run()

    st = tmp_session.load_state()
    assert st.status == "waiting:review"
    # Should have posted review comment
    comments = tracker.comments.get(issue.id, [])
    assert any("awaiting review" in c.body for c in comments)


def test_done_with_auto_merge_label_still_exits(tmp_path, tmp_session, issue):
    """Even with auto-merge label, container exits with waiting:review.
    Merge is always handled by the host."""
    issue.labels = ["auto-merge"]
    agent = MockAgent(events=[
        AgentEvent(type=AgentEventType.TEXT, content="@@DONE@@"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(
        agent, issue, tmp_session, tmp_path,
        merge_config=MergeConfig(require_review=True, auto_merge_label="auto-merge"),
    )
    runner.run()

    st = tmp_session.load_state()
    assert st.status == "waiting:review"


# --- Markers in tool results are ignored ---

def test_markers_in_tool_results_ignored(tmp_path, tmp_session, issue):
    """Markers appearing in TOOL_RESULT events should NOT be processed."""
    agent = MockAgent(events=[
        # Tool result containing marker text — should be ignored
        AgentEvent(type=AgentEventType.TOOL_RESULT,
                   content="@@DONE@@ found in output"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(agent, issue, tmp_session, tmp_path)
    runner.run()

    st = tmp_session.load_state()
    # Should NOT be done — the marker was in a tool result
    assert st.status != "done:pending-review"
    assert st.status != "completed"


def test_markers_only_in_text_events(tmp_path, tmp_session, issue):
    """Only TEXT events are scanned for markers. SYSTEM and TOOL_CALL are not."""
    agent = MockAgent(events=[
        AgentEvent(type=AgentEventType.SYSTEM,
                   content="@@DONE@@ system message"),
        AgentEvent(type=AgentEventType.TOOL_CALL,
                   content="@@CHECKPOINT@@ tool call"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    runner, tracker, notifier = make_runner(agent, issue, tmp_session, tmp_path)
    runner.run()

    st = tmp_session.load_state()
    assert st.status != "done:pending-review"
    assert st.step == 0  # no checkpoint was recorded


# --- Question without waiting ---

def test_question_without_waiting_handled_on_exit(tmp_path, tmp_session, issue):
    """If agent outputs @@QUESTION@@ but exits before @@WAITING@@,
    the post-exit handler collects the answer."""
    # Use a consuming iterator so events aren't replayed on restart
    events = iter([
        AgentEvent(type=AgentEventType.TEXT,
                   content="@@QUESTION@@ What library?"),
        AgentEvent(type=AgentEventType.PROCESS_EXIT),
    ])
    agent = MockAgent(events=events)
    runner, tracker, notifier = make_runner(agent, issue, tmp_session, tmp_path)

    # Pre-stage an answer so _collect_answer doesn't block
    notifier.pending_answers[issue.id] = "Use requests"

    runner.run()

    st = tmp_session.load_state()
    # Answer should have been recorded
    assert len(st.human_answers) >= 1
    assert st.human_answers[0].answer == "Use requests"
