"""Shared helpers for watcher tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment


def _make_watcher(tmp_path, tg_enabled=False):
    """Build a HostWatcher with a sessions dir and Telegram disabled."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    w = HostWatcher(sessions, repo, auto_start=False)
    w.telegram.enabled = tg_enabled
    w.telegram.token = "tok" if tg_enabled else ""
    w.telegram.chat_id = "123" if tg_enabled else ""
    return w


def _make_session(sessions_dir, sid, status="working", issue_id=None):
    """Create a minimal session directory with state.json."""
    sd = sessions_dir / sid
    sd.mkdir(exist_ok=True)
    state = {
        "issue_id": issue_id or f"issue-{sid}",
        "branch": f"agent/{sid}",
        "status": status,
        "step": 1,
        "checkpoints": [],
        "human_answers": [],
    }
    (sd / "state.json").write_text(json.dumps(state))
    return sd


def _make_issue(issue_id, title="Test Issue", labels=None, status="open"):
    return TrackerIssue(
        id=issue_id,
        identifier=issue_id[:12],
        title=title,
        body="",
        status=status,
        labels=labels or [],
    )


def _make_comment(body, author="human"):
    return TrackerComment(author=author, body=body)
