"""Tests for SessionMonitor: orphaned sessions, closed issues, cleanup."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher import HostWatcher
from core.protocols import TrackerIssue, TrackerComment

from tests.watcher.conftest import _make_watcher, _make_session, _make_issue, _make_comment


# ---------------------------------------------------------------------------
# check_orphaned_sessions tests
# ---------------------------------------------------------------------------

class TestCheckOrphanedSessions:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = time.time()
        _make_session(w.sessions_dir, "abc", status="working")
        with patch("host.watcher.docker_container_status") as mock_cs:
            w.monitor.check_orphaned_sessions()
        mock_cs.assert_not_called()

    def test_running_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_orphaned_session_auto_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" in w._recently_launched

    def test_recently_launched_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()  # just launched
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_paused_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value="paused"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_non_active_status_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_grace_period_expired_triggers_resume(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # Put in recently_launched but with old timestamp
        w._recently_launched["abc"] = time.time() - 9999  # way past grace period
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" not in w._recently_launched or w._recently_launched["abc"] > time.time() - 5

    def test_orphaned_coder_session_no_review_step(self, tmp_path):
        """Coder session resume should NOT include --step review."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        assert "--step" not in launched_cmds[0]

    def test_orphaned_review_session_includes_step_review(self, tmp_path):
        """Review session resume MUST include --step review to avoid container name collision."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        step_idx = cmd.index("--step")
        assert cmd[step_idx + 1] == "review"

    def test_orphan_resume_increments_counter(self, tmp_path):
        """Each orphan resume should increment orphan_resumes in state.json."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        state = json.loads((sd / "state.json").read_text())
        assert state["orphan_resumes"] == 1

    def test_orphan_resume_limit_stops_session(self, tmp_path):
        """After MAX_ORPHAN_RESUMES, session should be suspended, not resumed."""
        from host.constants import MAX_ORPHAN_RESUMES
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # Set orphan_resumes to the limit
        state = json.loads((sd / "state.json").read_text())
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)
        tracker = MagicMock()
        w._tracker = tracker

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:too-complex"
        tracker.add_comment.assert_called_once()
        assert "too complex" in tracker.add_comment.call_args[0][1].lower()

    def test_orphan_resume_limit_posts_telegram(self, tmp_path):
        """When limit is hit, a Telegram notification should be sent."""
        from host.constants import MAX_ORPHAN_RESUMES
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)
        tracker = MagicMock()
        w._tracker = tracker
        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        assert any("too complex" in n.lower() for n in notified)

    def test_orphaned_review_session_with_review_md(self, tmp_path):
        """Review session resume should pass --workflow REVIEW.md when it exists."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        # Create REVIEW.md in repo dir
        review_md = w.monitor.repo_dir / "REVIEW.md"
        review_md.write_text("---\nagent:\n  kind: claude-code\n---\n")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd)

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(review_md)


# ---------------------------------------------------------------------------
# check_auth_failures tests
# ---------------------------------------------------------------------------

class TestCheckAuthFailures:
    def test_skipped_within_retry_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = time.time()
        _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        w.monitor.check_auth_failures()
        assert launched == []

    def test_auth_failure_session_retried(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        w.monitor.check_auth_failures()

        assert "abc" in launched
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"

    def test_non_auth_failure_not_retried(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:stall", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid)

        w.monitor.check_auth_failures()
        assert launched == []

    def test_auth_retry_sets_recently_launched(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        w.monitor._launch_background = lambda cmd, sid: None

        w.monitor.check_auth_failures()
        assert "abc" in w._recently_launched

    def test_auth_retry_review_session_includes_step(self, tmp_path):
        """Review sessions should get --step review on auth retry."""
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="suspended:auth-failure", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd)

        w.monitor.check_auth_failures()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        step_idx = cmd.index("--step")
        assert cmd[step_idx + 1] == "review"


# ---------------------------------------------------------------------------
# check_closed_issues tests
# ---------------------------------------------------------------------------

class TestCheckClosedIssues:
    def test_skipped_within_poll_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = time.time()
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        w._tracker = tracker

        w.monitor.check_closed_issues()

        tracker.get_issue.assert_not_called()

    def test_working_session_cleaned_when_issue_closed(self, tmp_path):
        """Working sessions should also be cleaned up when the issue is closed."""
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        with patch("host.watcher.docker_stop"), \
             patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_closed_issues()

        assert "abc" in cleaned

    def test_closed_issue_triggers_cleanup(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        with patch("host.watcher.docker_stop"), \
             patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_closed_issues()

        assert "abc" in cleaned

    def test_cleanup_deferred_when_container_still_running(self, tmp_path):
        """If docker_stop fails to stop the container, cleanup is deferred."""
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sdir: cleaned.append(sid)

        with patch("host.watcher.docker_stop"), \
             patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_closed_issues()

        assert cleaned == []
        # Session dir should still exist
        assert sd.exists()

    def test_cleanup_deferred_when_container_paused(self, tmp_path):
        """Paused containers should also defer cleanup."""
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="closed")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sdir: cleaned.append(sid)

        with patch("host.watcher.docker_stop"), \
             patch("host.watcher.docker_container_status", return_value="paused"):
            w.monitor.check_closed_issues()

        assert cleaned == []

    def test_open_issue_not_cleaned(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        issue = _make_issue("issue-abc", status="open")
        tracker.get_issue.return_value = issue
        w._tracker = tracker

        cleaned = []
        w.monitor.cleanup_session = lambda sid, iid, sd: cleaned.append(sid)

        w.monitor.check_closed_issues()

        assert cleaned == []

    def test_tracker_failure_handled(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_closed_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        tracker = MagicMock()
        tracker.get_issue.side_effect = RuntimeError("tracker down")
        w._tracker = tracker

        # should not raise
        w.monitor.check_closed_issues()


# ---------------------------------------------------------------------------
# cleanup_session tests
# ---------------------------------------------------------------------------

class TestCleanupSession:
    def test_removes_session_dir_and_clears_tracking(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg
            w.monitor.cleanup_session("abc", "issue-abc", sd)

        mock_rmtree.assert_called_once_with(sd)
        assert "abc" not in w._recently_launched

    def test_cleanup_failure_logged_not_raised(self, tmp_path):
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc")

        with patch("core.config.load_workflow", side_effect=RuntimeError("boom")):
            # should not raise
            w.monitor.cleanup_session("abc", "issue-abc", sd)
