"""Tests for the automated review step feature.

Tests cover:
- Config: max_rounds parsing
- Review command: approve parsing
- Launch: --step flag for session/branch naming
- Watcher: auto-review detection, reviewer verdict handling
- CLI: review artifact cleanup
- Prompts: extra template variables
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.config import load_workflow, WorkflowConfig
from core.review import parse_nightshift_command, strip_nightshift_command
from core.prompts import render_template
from core.protocols import TrackerIssue


# --- Config: max_rounds ---

def test_max_rounds_default():
    config = WorkflowConfig()
    assert config.review.max_rounds == 3


def test_max_rounds_from_yaml(tmp_path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\nreview:\n  max_rounds: 5\n---\nPrompt here\n")
    config = load_workflow(wf)
    assert config.review.max_rounds == 5


def test_max_rounds_default_when_not_specified(tmp_path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\nagent:\n  kind: claude-code\n---\nPrompt\n")
    config = load_workflow(wf)
    assert config.review.max_rounds == 3


# --- Review command: approve ---

def test_parse_approve():
    assert parse_nightshift_command("@nightshift approve") == "approve"


def test_parse_approve_case_insensitive():
    assert parse_nightshift_command("@Nightshift Approve") == "approve"


def test_parse_approve_with_context():
    text = "All tests pass. Code looks good. @nightshift approve"
    assert parse_nightshift_command(text) == "approve"


def test_parse_approve_in_code_block():
    text = "```\n@nightshift approve\n```"
    assert parse_nightshift_command(text) is None


def test_strip_approve_command():
    text = "Looks good to me. @nightshift approve"
    assert strip_nightshift_command(text) == "Looks good to me."


# --- Prompts: extra template variables ---

def test_render_template_with_extra_vars():
    template = "Diff: {{ diff }}\nBase: {{ base_branch }}\nAgent: {{ agent_branch }}"
    issue = TrackerIssue(
        id="test-001", identifier="test-001",
        title="Test", body="Body", status="open", labels=[],
    )
    result = render_template(
        template, issue=issue,
        diff="+ added line", base_branch="master", agent_branch="agent/abc123",
    )
    assert "+ added line" in result
    assert "master" in result
    assert "agent/abc123" in result


def test_render_template_extra_vars_unused():
    """Extra vars that aren't referenced in template should not cause errors."""
    template = "Issue: {{ issue.title }}"
    issue = TrackerIssue(
        id="test-001", identifier="test-001",
        title="Test", body="Body", status="open", labels=[],
    )
    result = render_template(template, issue=issue, diff="some diff")
    assert "Test" in result


# --- Watcher: auto-review detection ---

def test_watcher_detects_waiting_review_with_review_md(tmp_path):
    """When coder is waiting:review and REVIEW.md exists, watcher should launch reviewer."""
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    # Create REVIEW.md
    (repo / "REVIEW.md").write_text("---\nagent:\n  kind: claude-code\n---\nReview prompt\n")

    # Create coder session in waiting:review
    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "waiting:review",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    watcher = HostWatcher(sessions, repo, auto_start=False)

    # Mock _launch_background to capture the command
    launched = []
    watcher.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid))

    watcher.reviews.check_for_auto_review()

    # Verify reviewer was launched
    assert len(launched) == 1
    cmd, sid = launched[0]
    assert sid == "review-abc123"
    assert "--step" in cmd
    assert "review" in cmd
    assert "--coder-session" in cmd
    assert "abc123" in cmd

    # Verify coder status changed to reviewing
    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "reviewing"


def test_watcher_skips_review_without_review_md(tmp_path):
    """Without REVIEW.md, watcher should not launch reviewer."""
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()
    # No REVIEW.md

    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "waiting:review",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    watcher = HostWatcher(sessions, repo, auto_start=False)
    launched = []
    watcher.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid))

    watcher.reviews.check_for_auto_review()

    # No reviewer launched
    assert len(launched) == 0

    # Coder status unchanged
    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "waiting:review"


def test_watcher_max_rounds_escalation(tmp_path):
    """After max_rounds, watcher should escalate to human review."""
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    (repo / "REVIEW.md").write_text("---\nreview:\n  max_rounds: 2\n---\nReview\n")

    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "waiting:review",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    watcher = HostWatcher(sessions, repo, auto_start=False)
    watcher.reviews._rounds["abc123"] = 2  # Already at max

    launched = []
    watcher.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid))
    watcher.telegram.notify = MagicMock()

    watcher.reviews.check_for_auto_review()

    # No reviewer launched
    assert len(launched) == 0

    # Coder escalated to waiting:human-review
    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "waiting:human-review"


def test_watcher_skips_review_sessions(tmp_path):
    """Watcher should not try to review review- sessions."""
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    (repo / "REVIEW.md").write_text("---\n---\nReview\n")

    # Create a review session (should be skipped)
    review_dir = sessions / "review-abc123"
    review_dir.mkdir()
    (review_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "review/abc123",
        "status": "waiting:review",
        "step": 1,
        "checkpoints": [],
        "human_answers": [],
    }))

    watcher = HostWatcher(sessions, repo, auto_start=False)
    launched = []
    watcher.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid))

    watcher.reviews.check_for_auto_review()

    assert len(launched) == 0


# --- Watcher: reviewer verdict extraction ---

def test_extract_reviewer_verdict_approve(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    watcher = HostWatcher(sessions, repo, auto_start=False)

    conv_log = tmp_path / "conversation.jsonl"
    conv_log.write_text(
        json.dumps({"role": "thought", "content": "Looking at the code..."}) + "\n"
        + json.dumps({"role": "thought", "content": "Tests pass. @nightshift approve"}) + "\n"
    )

    verdict = watcher.reviews.extract_reviewer_verdict(conv_log, "issue-123")
    assert verdict == "approve"


def test_extract_reviewer_verdict_revise(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    watcher = HostWatcher(sessions, repo, auto_start=False)

    conv_log = tmp_path / "conversation.jsonl"
    conv_log.write_text(
        json.dumps({"role": "thought", "content": "Tests fail. Fix error handling. @nightshift revise"}) + "\n"
    )

    verdict = watcher.reviews.extract_reviewer_verdict(conv_log, "issue-123")
    assert verdict == "revise"


def test_extract_reviewer_verdict_none(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    watcher = HostWatcher(sessions, repo, auto_start=False)

    conv_log = tmp_path / "conversation.jsonl"
    conv_log.write_text(
        json.dumps({"role": "thought", "content": "Still reviewing..."}) + "\n"
    )

    verdict = watcher.reviews.extract_reviewer_verdict(conv_log, "issue-123")
    assert verdict is None


# --- Watcher: handle reviewer approve ---

def test_handle_reviewer_approve(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "reviewing",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    watcher = HostWatcher(sessions, repo, auto_start=False)
    watcher.telegram.notify = MagicMock()

    watcher.reviews.handle_reviewer_approve("abc123", coder_dir, "issue-abc123456789")

    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "waiting:human-review"


# --- Watcher: handle reviewer revise ---

def test_handle_reviewer_revise(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "reviewing",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    review_dir = sessions / "review-abc123"
    review_dir.mkdir()
    (review_dir / "conversation.jsonl").write_text(
        json.dumps({"role": "thought", "content": "Tests fail in module X. @nightshift revise"}) + "\n"
    )

    watcher = HostWatcher(sessions, repo, auto_start=False)
    watcher.telegram.notify = MagicMock()
    launched = []
    watcher.reviews._launch_background = lambda cmd, sid: launched.append((cmd, sid))

    watcher.reviews.handle_reviewer_revise("abc123", coder_dir, "issue-abc123456789", review_dir)

    # Coder status should be working
    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "working"

    # Resume prompt should be written
    assert (coder_dir / "resume-prompt.md").exists()
    prompt = (coder_dir / "resume-prompt.md").read_text()
    assert "Tests fail in module X" in prompt

    # Coder should be relaunched
    assert len(launched) == 1
    assert "--resume" in launched[0][0]


# --- Watcher: full reviewer done flow ---

def test_check_reviewer_done_approve(tmp_path):
    from host.watcher import HostWatcher

    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    sessions.mkdir()
    repo.mkdir()

    # Coder session in reviewing state
    coder_dir = sessions / "abc123"
    coder_dir.mkdir()
    (coder_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "agent/abc123",
        "status": "reviewing",
        "step": 3,
        "checkpoints": [],
        "human_answers": [],
    }))

    # Reviewer session is done (waiting:review status after @@DONE@@)
    review_dir = sessions / "review-abc123"
    review_dir.mkdir()
    (review_dir / "state.json").write_text(json.dumps({
        "issue_id": "issue-abc123456789",
        "branch": "review/abc123",
        "status": "waiting:review",
        "step": 1,
        "checkpoints": [],
        "human_answers": [],
    }))
    (review_dir / "conversation.jsonl").write_text(
        json.dumps({"role": "thought", "content": "All good. @nightshift approve"}) + "\n"
    )

    watcher = HostWatcher(sessions, repo, auto_start=False)
    watcher.telegram.notify = MagicMock()
    # Mock cleanup to avoid git operations
    watcher.reviews.cleanup_review_session = MagicMock()

    watcher.reviews.check_reviewer_done()

    # Coder should be waiting:human-review
    state = json.loads((coder_dir / "state.json").read_text())
    assert state["status"] == "waiting:human-review"

    # Cleanup should have been called
    watcher.reviews.cleanup_review_session.assert_called_once()


# --- Launch: step flag naming ---

def test_step_session_naming():
    """Verify step-based naming convention."""
    short_id = "abc123456789"
    step = "review"

    session_name = f"{step}-{short_id}"
    branch = f"{step}/{short_id}"

    assert session_name == "review-abc123456789"
    assert branch == "review/abc123456789"


def test_no_step_session_naming():
    """Verify default naming convention."""
    short_id = "abc123456789"

    session_name = short_id
    branch = f"agent/{short_id}"

    assert session_name == "abc123456789"
    assert branch == "agent/abc123456789"


# --- CLI: review artifact cleanup ---

def test_cleanup_review_artifacts(tmp_path):
    """Test that _cleanup_review_artifacts removes review session dir."""
    from host.cli import _cleanup_review_artifacts

    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = repo / ".nightshift" / "sessions"
    sessions.mkdir(parents=True)

    # Create review session dir
    review_session = sessions / "review-abc123"
    review_session.mkdir()
    (review_session / "state.json").write_text("{}")

    config = WorkflowConfig()
    config.workspace.root = ".worktrees"

    # Mock git operations
    with patch("subprocess.run"):
        _cleanup_review_artifacts(repo, "abc123", config)

    assert not review_session.exists()
