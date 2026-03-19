"""Tests for host/watcher/issue_sync.py — bidirectional file-based sync."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import asdict

from core.constants import TRACKER_OUTBOX_FILENAME
from core.protocols import TrackerIssue, TrackerComment
from host.watcher.issue_sync import process_outbox, sync_sessions, _apply_outbox_entry

from tests.watcher.conftest import _make_session


@pytest.fixture
def sessions_dir(tmp_path):
    sd = tmp_path / "sessions"
    sd.mkdir()
    return sd


@pytest.fixture
def mock_tracker():
    tracker = MagicMock()
    tracker.get_issue.return_value = TrackerIssue(
        id="issue-abc", identifier="issue-abc", title="T", body="B", status="open",
    )
    tracker.get_comments.return_value = [
        TrackerComment(author="human", body="looks good", created_at=None),
    ]
    tracker.list_issues.return_value = []
    return tracker


# ── process_outbox tests ─────────────────────────────────────

class TestProcessOutbox:
    def test_no_outbox_file(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        assert process_outbox(sd, mock_tracker) == 0

    def test_empty_outbox(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text("")
        assert process_outbox(sd, mock_tracker) == 0

    def test_processes_comment(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": "i1", "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with("i1", "hello")
        # Outbox should be truncated after processing
        assert (sd / TRACKER_OUTBOX_FILENAME).read_text() == ""

    def test_processes_multiple_ops(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        lines = [
            json.dumps({"op": "comment", "issue_id": "i1", "text": "c1"}),
            json.dumps({"op": "label_add", "issue_id": "i1", "label": "wip"}),
            json.dumps({"op": "set_status", "issue_id": "i1", "status": "closed"}),
            json.dumps({"op": "label_rm", "issue_id": "i1", "label": "wip"}),
        ]
        (sd / TRACKER_OUTBOX_FILENAME).write_text("\n".join(lines) + "\n")
        assert process_outbox(sd, mock_tracker) == 4
        mock_tracker.add_comment.assert_called_once()
        mock_tracker.add_label.assert_called_once_with("i1", "wip")
        mock_tracker.set_status.assert_called_once_with("i1", "closed")
        mock_tracker.remove_label.assert_called_once_with("i1", "wip")

    def test_bad_json_line_skipped(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        lines = "NOT JSON\n" + json.dumps({"op": "comment", "issue_id": "i1", "text": "ok"}) + "\n"
        (sd / TRACKER_OUTBOX_FILENAME).write_text(lines)
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once()

    def test_unknown_op_skipped(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "unknown_op", "issue_id": "i1"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 0

    def test_tracker_error_handled(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        mock_tracker.add_comment.side_effect = RuntimeError("tracker down")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": "i1", "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 0


# ── sync_sessions tests ─────────────────────────────────────

class TestSyncSessions:
    def test_processes_outbox_for_active_session(self, sessions_dir, mock_tracker):
        sd = _make_session(sessions_dir, "abc", status="working", issue_id="issue-abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": "issue-abc", "text": "log"}) + "\n"
        )
        with patch("host.watcher.issue_sync.redump_issue", return_value=True) as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            mock_tracker.add_comment.assert_called_once()
            mock_redump.assert_called_once_with(mock_tracker, "issue-abc", sd)

    def test_skips_redump_for_non_working(self, sessions_dir, mock_tracker):
        sd = _make_session(sessions_dir, "abc", status="done:pending-review", issue_id="issue-abc")
        with patch("host.watcher.issue_sync.redump_issue") as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            mock_redump.assert_not_called()

    def test_processes_outbox_even_for_non_working(self, sessions_dir, mock_tracker):
        """Outbox is processed for any session that has one, even non-working."""
        sd = _make_session(sessions_dir, "abc", status="done:pending-review", issue_id="issue-abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": "issue-abc", "text": "done"}) + "\n"
        )
        with patch("host.watcher.issue_sync.redump_issue"):
            sync_sessions(sessions_dir, mock_tracker)
            mock_tracker.add_comment.assert_called_once()

    def test_missing_sessions_dir(self, tmp_path, mock_tracker):
        missing = tmp_path / "nonexistent"
        sync_sessions(missing, mock_tracker)  # Should not raise

    def test_skips_session_without_issue_id(self, sessions_dir, mock_tracker):
        sd = sessions_dir / "abc"
        sd.mkdir()
        (sd / "state.json").write_text(json.dumps({"status": "working"}))
        with patch("host.watcher.issue_sync.redump_issue") as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            mock_redump.assert_not_called()

    def test_redumps_for_all_active_statuses(self, sessions_dir, mock_tracker):
        """Verifies re-dump happens for working, starting, and waiting:answer."""
        for i, status in enumerate(["working", "starting", "waiting:answer"]):
            sid = f"s{i}"
            _make_session(sessions_dir, sid, status=status, issue_id=f"issue-{sid}")

        with patch("host.watcher.issue_sync.redump_issue", return_value=True) as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            assert mock_redump.call_count == 3
