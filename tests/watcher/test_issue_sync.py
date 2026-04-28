"""Tests for host/watcher/issue_sync.py — bidirectional file-based sync."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import asdict

from core.constants import TRACKER_OUTBOX_FILENAME, TRACKER_OUTBOX_PROCESSING
from core.protocols import TrackerIssue, TrackerComment
import host.watcher.issue_sync as issue_sync_mod
from host.watcher.issue_sync import (
    process_outbox,
    sync_sessions,
    _apply_outbox_entry,
    _validate_outbox_entry,
)

from tests.watcher.conftest import _make_session


VALID_ISSUE_ID = "a1b2c3d4"


@pytest.fixture(autouse=True)
def reset_redump_throttle():
    """Clear the per-session redump throttle between tests."""
    issue_sync_mod._last_redump.clear()
    yield
    issue_sync_mod._last_redump.clear()


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
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with(VALID_ISSUE_ID, "hello")
        # Outbox and processing file should be cleaned up after processing
        assert not (sd / TRACKER_OUTBOX_FILENAME).exists()
        assert not (sd / TRACKER_OUTBOX_PROCESSING).exists()

    def test_processes_multiple_ops(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        lines = [
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "c1"}),
            json.dumps({"op": "label_add", "issue_id": VALID_ISSUE_ID, "label": "wip"}),
            json.dumps({"op": "set_status", "issue_id": VALID_ISSUE_ID, "status": "closed"}),
            json.dumps({"op": "label_rm", "issue_id": VALID_ISSUE_ID, "label": "wip"}),
        ]
        (sd / TRACKER_OUTBOX_FILENAME).write_text("\n".join(lines) + "\n")
        assert process_outbox(sd, mock_tracker) == 4
        mock_tracker.add_comment.assert_called_once()
        mock_tracker.add_label.assert_called_once_with(VALID_ISSUE_ID, "wip")
        mock_tracker.set_status.assert_called_once_with(VALID_ISSUE_ID, "closed")
        mock_tracker.remove_label.assert_called_once_with(VALID_ISSUE_ID, "wip")

    def test_bad_json_line_skipped(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        lines = "NOT JSON\n" + json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "ok"}) + "\n"
        (sd / TRACKER_OUTBOX_FILENAME).write_text(lines)
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once()

    def test_unknown_op_skipped(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "unknown_op", "issue_id": VALID_ISSUE_ID}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 0

    def test_tracker_error_handled(self, tmp_path, mock_tracker):
        sd = _make_session(tmp_path, "abc")
        mock_tracker.add_comment.side_effect = RuntimeError("tracker down")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 0

    def test_crash_recovery_processes_leftover(self, tmp_path, mock_tracker):
        """A .processing file left from a previous crash is processed first."""
        sd = _make_session(tmp_path, "abc")
        # Simulate a leftover .processing file from a previous crash
        (sd / TRACKER_OUTBOX_PROCESSING).write_text(
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "old"}) + "\n"
        )
        # Plus a new outbox entry
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "label_add", "issue_id": VALID_ISSUE_ID, "label": "wip"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 2
        mock_tracker.add_comment.assert_called_once_with(VALID_ISSUE_ID, "old")
        mock_tracker.add_label.assert_called_once_with(VALID_ISSUE_ID, "wip")
        assert not (sd / TRACKER_OUTBOX_PROCESSING).exists()
        assert not (sd / TRACKER_OUTBOX_FILENAME).exists()

    def test_atomic_rename_no_data_loss(self, tmp_path, mock_tracker):
        """After rename, new container writes go to a fresh outbox file."""
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "batch1"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 1
        # Outbox file was renamed and deleted; container can safely create a new one
        assert not (sd / TRACKER_OUTBOX_FILENAME).exists()

    def test_process_outbox_validates_op(self, tmp_path, mock_tracker, caplog):
        with pytest.raises(ValueError, match="Unknown op"):
            _validate_outbox_entry({"op": "bogus", "issue_id": VALID_ISSUE_ID})

        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            "\n".join([
                json.dumps({"op": "bogus", "issue_id": VALID_ISSUE_ID}),
                json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "ok"}),
            ]) + "\n"
        )
        with caplog.at_level("WARNING"):
            assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with(VALID_ISSUE_ID, "ok")
        assert "Invalid outbox entry" in caplog.text
        assert "bogus" in caplog.text

    @pytest.mark.parametrize("issue_id", ["zzzzzzzz", "abc123"])
    def test_process_outbox_validates_issue_id(self, issue_id):
        with pytest.raises(ValueError, match="Invalid issue_id"):
            _validate_outbox_entry({"op": "comment", "issue_id": issue_id})

    def test_process_outbox_accepts_full_issue_id(self, tmp_path, mock_tracker):
        """Full 64-char hex IDs from StaticTracker are valid."""
        full_id = "ca8e754a8413156025a4fa376edfe0d9aa78c58b43babf8acb0ac9ec36394419"
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": full_id, "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with(full_id, "hello")

    def test_process_outbox_accepts_short_issue_id(self, tmp_path, mock_tracker):
        """Short 12-char IDs (the display format) are valid."""
        short_id = "ca8e754a8413"
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": short_id, "text": "hello"}) + "\n"
        )
        assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with(short_id, "hello")

    @pytest.mark.parametrize("entry", [
        {"issue_id": VALID_ISSUE_ID},
        {"op": "comment"},
        {},
    ])
    def test_process_outbox_validates_required_fields(self, entry):
        with pytest.raises(ValueError, match="Missing required fields"):
            _validate_outbox_entry(entry)

    def test_process_outbox_skips_invalid_continues(self, tmp_path, mock_tracker, caplog):
        sd = _make_session(tmp_path, "abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            "\n".join([
                json.dumps({"op": "comment", "issue_id": "nothex!!", "text": "bad"}),
                json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "good"}),
            ]) + "\n"
        )
        with caplog.at_level("WARNING"):
            assert process_outbox(sd, mock_tracker) == 1
        mock_tracker.add_comment.assert_called_once_with(VALID_ISSUE_ID, "good")
        assert "Invalid outbox entry" in caplog.text
        assert "nothex!!" in caplog.text


# ── sync_sessions tests ─────────────────────────────────────

class TestSyncSessions:
    def test_processes_outbox_for_active_session(self, sessions_dir, mock_tracker):
        sd = _make_session(sessions_dir, "abc", status="working", issue_id="issue-abc")
        (sd / TRACKER_OUTBOX_FILENAME).write_text(
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "log"}) + "\n"
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
            json.dumps({"op": "comment", "issue_id": VALID_ISSUE_ID, "text": "done"}) + "\n"
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

    def test_redump_throttled_within_interval(self, sessions_dir, mock_tracker):
        """Second sync_sessions call within ISSUE_REDUMP_INTERVAL_S skips redump."""
        _make_session(sessions_dir, "abc", status="working", issue_id="issue-abc")

        with patch("host.watcher.issue_sync.redump_issue", return_value=True) as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            assert mock_redump.call_count == 1

            # Second call immediately — should be throttled
            sync_sessions(sessions_dir, mock_tracker)
            assert mock_redump.call_count == 1

    def test_redump_fires_after_interval_expires(self, sessions_dir, mock_tracker):
        """After ISSUE_REDUMP_INTERVAL_S passes, redump fires again."""
        _make_session(sessions_dir, "abc", status="working", issue_id="issue-abc")

        with patch("host.watcher.issue_sync.redump_issue", return_value=True) as mock_redump:
            sync_sessions(sessions_dir, mock_tracker)
            assert mock_redump.call_count == 1

            # Expire the throttle by backdating _last_redump
            issue_sync_mod._last_redump["abc"] = 0.0
            sync_sessions(sessions_dir, mock_tracker)
            assert mock_redump.call_count == 2
