"""Tests for SessionMonitor: orphaned sessions, closed issues, cleanup."""

import json
import sys
import time
from datetime import datetime, timezone, timedelta
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
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_orphaned_session_auto_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" in w._recently_launched

    def test_recently_launched_session_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        w._recently_launched["abc"] = time.time()  # just launched
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_paused_container_not_resumed(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value="paused"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_non_active_status_skipped(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_coder_waiting_review_not_treated_as_orphan(self, tmp_path):
        """Coder session in waiting:review is expected — NOT an orphan."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_review_session_waiting_review_no_container_is_orphan(self, tmp_path):
        """Review session with waiting:review, no completed_at, and no container -> orphaned."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert "review-abc" in launched
        state = json.loads((sd / "state.json").read_text())
        assert state["orphan_resumes"] == 1

    def test_review_session_completed_at_not_orphan(self, tmp_path):
        """Review session with waiting:review and completed_at set -> NOT an orphan.

        This is the core fix for the race condition: a review session that
        completed normally (@@DONE@@ -> notify_done -> completed_at) should
        not be misclassified as orphaned just because its container exited.
        """
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        # Simulate notify_done() having set completed_at
        state = json.loads((sd / "state.json").read_text())
        state["completed_at"] = "2026-03-25T00:00:00+00:00"
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_review_session_no_completed_at_still_orphan(self, tmp_path):
        """Review session with waiting:review but no completed_at -> still an orphan (crashed)."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert "review-abc" in launched

    def test_review_session_waiting_review_running_container_not_orphan(self, tmp_path):
        """Review session with waiting:review and running container -> NOT orphaned."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="waiting:review", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_orphaned_sessions()

        assert launched == []

    def test_grace_period_expired_triggers_resume(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # Put in recently_launched but with old timestamp
        w._recently_launched["abc"] = time.time() - 9999  # way past grace period
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert "abc" in launched
        assert "abc" not in w._recently_launched or w._recently_launched["abc"] > time.time() - 5

    def test_orphaned_coder_session_no_review_step(self, tmp_path):
        """Coder session resume should NOT include --step review."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        assert "--step" not in launched_cmds[0]

    def test_orphaned_review_session_includes_step_review(self, tmp_path):
        """Review session resume MUST include --step review to avoid container name collision."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
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
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
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
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:too-complex"
        tracker.add_comment.assert_called_once()
        assert "too complex" in tracker.add_comment.call_args[0][1].lower()

    def test_coder_session_with_completed_at_not_orphan(self, tmp_path):
        """Coder sessions with completed_at set should not be orphan-resumed."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["completed_at"] = "2026-04-23T00:00:00+00:00"
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"
        assert state.get("orphan_resumes", 0) == 0

    def test_coder_session_completed_at_race_condition(self, tmp_path):
        """A completed coder session should not hit the orphan limit during status-update race."""
        from host.constants import MAX_ORPHAN_RESUMES

        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["completed_at"] = "2026-04-23T00:00:00+00:00"
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"
        tracker.add_comment.assert_not_called()

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
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker
        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        assert any("too complex" in n.lower() for n in notified)

    def test_review_session_orphan_limit_sets_review_failed(self, tmp_path):
        """Review session hitting orphan limit should be suspended:review-failed, not too-complex."""
        from host.constants import MAX_ORPHAN_RESUMES, REVIEW_SESSION_PREFIX
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sid = "abc"
        review_sid = f"{REVIEW_SESSION_PREFIX}{coder_sid}"
        # Create coder session in 'reviewing' status
        coder_sd = _make_session(w.sessions_dir, coder_sid, status="reviewing", issue_id="issue-abc")
        # Create review session at the orphan limit
        review_sd = _make_session(w.sessions_dir, review_sid, status="working", issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (review_sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        # Review session should be suspended:review-failed
        review_state = json.loads((review_sd / "state.json").read_text())
        assert review_state["status"] == "suspended:review-failed"
        # Coder session should transition to waiting:human-review
        coder_state = json.loads((coder_sd / "state.json").read_text())
        assert coder_state["status"] == "waiting:human-review"

    def test_review_session_orphan_limit_posts_fallback_message(self, tmp_path):
        """Review session orphan limit should post human-review fallback, not too-complex."""
        from host.constants import MAX_ORPHAN_RESUMES, REVIEW_SESSION_PREFIX
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_orphan_check = 0.0
        coder_sid = "abc"
        review_sid = f"{REVIEW_SESSION_PREFIX}{coder_sid}"
        _make_session(w.sessions_dir, coder_sid, status="reviewing", issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, review_sid, status="working", issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (review_sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker
        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        # Telegram message should mention human review, not too-complex
        assert any("human review" in n.lower() for n in notified)
        assert not any("too complex" in n.lower() for n in notified)
        # Tracker comment should mention auto-review failed
        tracker.add_comment.assert_called_once()
        comment_text = tracker.add_comment.call_args[0][1]
        assert "auto-review failed" in comment_text.lower() or "review failed" in comment_text.lower()
        assert "sub-tasks" not in comment_text.lower()

    def test_coder_session_orphan_limit_still_too_complex(self, tmp_path):
        """Coder (non-review) session orphan limit should still be suspended:too-complex."""
        from host.constants import MAX_ORPHAN_RESUMES
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["orphan_resumes"] = MAX_ORPHAN_RESUMES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True
        tracker = MagicMock()
        w._tracker = tracker

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:too-complex"

    def test_orphaned_review_session_with_review_md(self, tmp_path):
        """Review session resume should pass --workflow REVIEW.md when it exists."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        # Create REVIEW.md in repo dir
        review_md = w.monitor.repo_dir / "REVIEW.md"
        review_md.write_text("---\nagent:\n  kind: claude-code\n---\n")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
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
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_auth_failures()
        assert launched == []

    def test_auth_failure_session_retried(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_auth_failures()

        assert "abc" in launched
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"

    def test_non_auth_failure_not_retried(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:stall", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_auth_failures()
        assert launched == []

    def test_auth_retry_sets_recently_launched(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_auth_failures()
        assert "abc" in w._recently_launched

    def test_auth_retry_review_session_includes_step(self, tmp_path):
        """Review sessions should get --step review on auth retry."""
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="suspended:auth-failure", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        w.monitor.check_auth_failures()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        step_idx = cmd.index("--step")
        assert cmd[step_idx + 1] == "review"

    def test_auth_retry_increments_counter(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_auth_failures()

        state = json.loads((sd / "state.json").read_text())
        assert state["auth_retries"] == 1

    def test_auth_retry_limit_stops_retrying(self, tmp_path):
        """After MAX_AUTH_RETRIES, session becomes suspended:auth-failure-permanent."""
        from host.constants import MAX_AUTH_RETRIES
        w = _make_watcher(tmp_path)
        w.monitor._last_auth_retry_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["auth_retries"] = MAX_AUTH_RETRIES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_auth_failures()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:auth-failure-permanent"

    def test_auth_retry_limit_notifies_telegram(self, tmp_path):
        from host.constants import MAX_AUTH_RETRIES
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_auth_retry_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:auth-failure", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["auth_retries"] = MAX_AUTH_RETRIES
        (sd / "state.json").write_text(json.dumps(state))

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_auth_failures()

        assert any("giving up" in n.lower() for n in notified)


# ---------------------------------------------------------------------------
# check_provider_outages tests
# ---------------------------------------------------------------------------

class TestCheckProviderOutages:
    """Tests for watcher-based provider outage retry (suspended:provider-overload)."""

    def test_skipped_within_retry_interval(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = time.time()
        _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_provider_outages()
        assert launched == []

    def test_provider_outage_session_retried(self, tmp_path):
        """Provider-overload session should be retried after interval."""
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_provider_outages()

        assert "abc" in launched
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "working"

    def test_non_provider_overload_not_retried(self, tmp_path):
        """Sessions with other suspended statuses should not be retried."""
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:stall", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_provider_outages()
        assert launched == []

    def test_provider_outage_retry_sets_recently_launched(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_provider_outages()
        assert "abc" in w._recently_launched

    def test_provider_outage_retry_increments_counter(self, tmp_path):
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_provider_outages()

        state = json.loads((sd / "state.json").read_text())
        assert state["overload_resumes"] == 1

    def test_provider_outage_retry_limit_stops_retrying(self, tmp_path):
        """After MAX_PROVIDER_OUTAGE_RETRIES, session becomes suspended:provider-overload-permanent."""
        from host.constants import MAX_PROVIDER_OUTAGE_RETRIES
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["overload_resumes"] = MAX_PROVIDER_OUTAGE_RETRIES
        (sd / "state.json").write_text(json.dumps(state))

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        w.monitor.check_provider_outages()

        assert launched == []
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:provider-overload-permanent"

    def test_provider_outage_retry_limit_notifies_telegram(self, tmp_path):
        from host.constants import MAX_PROVIDER_OUTAGE_RETRIES
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_provider_outage_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="suspended:provider-overload", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["overload_resumes"] = MAX_PROVIDER_OUTAGE_RETRIES
        (sd / "state.json").write_text(json.dumps(state))

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)
        w.monitor._launch_background = lambda cmd, sid: True

        w.monitor.check_provider_outages()

        assert any("giving up" in n.lower() for n in notified)

    def test_provider_outage_review_session_includes_step(self, tmp_path):
        """Review sessions should get --step review on provider outage retry."""
        w = _make_watcher(tmp_path)
        w.monitor._last_provider_outage_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="suspended:provider-overload", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        w.monitor.check_provider_outages()

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


# ---------------------------------------------------------------------------
# --workflow passthrough tests
# ---------------------------------------------------------------------------

class TestWorkflowPassthrough:
    """Ensure --workflow is passed to launch.py in all code paths."""

    def test_orphan_resume_passes_workflow(self, tmp_path):
        """Orphan resume for a coder session should include --workflow."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(w.monitor.workflow_path)

    def test_orphan_resume_passes_custom_workflow(self, tmp_path):
        """Orphan resume uses the custom workflow_path, not default."""
        from host.watcher import HostWatcher
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        custom_wf = tmp_path / "custom" / "MY_WORKFLOW.md"
        custom_wf.parent.mkdir()
        custom_wf.write_text("---\nagent:\n  kind: claude-code\n---\nPrompt")
        w = HostWatcher(sessions, repo, auto_start=False, workflow_path=custom_wf)
        w.telegram.enabled = False
        w.monitor._last_orphan_check = 0.0
        _make_session(sessions, "abc", status="working", issue_id="issue-abc")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(custom_wf)

    def test_auto_start_passes_workflow(self, tmp_path):
        """Auto-start should include --workflow in the launched command."""
        from host.watcher import HostWatcher
        from core.config import AutoStartConfig

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        w = HostWatcher(sessions, repo, auto_start=True)
        w.telegram.enabled = False
        asc = AutoStartConfig(enabled=True, label="nightshift",
                              poll_interval_s=0, max_concurrent=4)
        w._auto_start_config = asc

        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
        ]
        w._tracker = tracker

        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        w.monitor.check_new_issues()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(w.monitor.workflow_path)

    def test_auto_start_passes_custom_workflow(self, tmp_path):
        """Auto-start uses the custom workflow_path when configured."""
        from host.watcher import HostWatcher
        from core.config import AutoStartConfig

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        custom_wf = tmp_path / "custom" / "MY_WORKFLOW.md"
        custom_wf.parent.mkdir()
        custom_wf.write_text("---\nagent:\n  kind: claude-code\n---\nPrompt")
        w = HostWatcher(sessions, repo, auto_start=True, workflow_path=custom_wf)
        w.telegram.enabled = False
        asc = AutoStartConfig(enabled=True, label="nightshift",
                              poll_interval_s=0, max_concurrent=4)
        w._auto_start_config = asc

        tracker = MagicMock()
        tracker.list_issues.return_value = [
            _make_issue("id-1", labels=["nightshift"]),
        ]
        w._tracker = tracker

        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        w.monitor.check_new_issues()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(custom_wf)

    def test_review_session_gets_review_md_override(self, tmp_path):
        """Review sessions should get REVIEW.md, not the workflow_path."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        review_md = w.monitor.repo_dir / "REVIEW.md"
        review_md.write_text("---\nagent:\n  kind: claude-code\n---\n")
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(review_md)

    def test_review_session_falls_back_to_workflow_path_without_review_md(self, tmp_path):
        """Review sessions without REVIEW.md should fall back to workflow_path."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "review-abc", status="working", issue_id="issue-abc")
        # No REVIEW.md created
        launched_cmds = []
        w.monitor._launch_background = lambda cmd, sid: launched_cmds.append(cmd) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        assert len(launched_cmds) == 1
        cmd = launched_cmds[0]
        wf_idx = cmd.index("--workflow")
        assert cmd[wf_idx + 1] == str(w.monitor.workflow_path)


# ---------------------------------------------------------------------------
# "reviewing" status recovery tests
# ---------------------------------------------------------------------------

class TestReviewingStatusRecovery:
    """Coder sessions stuck in 'reviewing' with no review container are reverted."""

    def test_reviewing_no_review_container_reverts_to_waiting_review(self, tmp_path):
        """Coder stuck in 'reviewing' with no review container -> revert."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:review"
        # Should NOT have tried to resume the session as an orphan
        assert launched == []

    def test_reviewing_with_running_review_container_left_alone(self, tmp_path):
        """Coder in 'reviewing' with running review container -> no change."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        def mock_status(container):
            if "review-" in container:
                return "running"
            return None

        with patch("host.watcher.docker_container_status", side_effect=mock_status):
            w.monitor.check_orphaned_sessions()

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "reviewing"  # unchanged
        assert launched == []

    def test_reviewing_with_recently_launched_review_left_alone(self, tmp_path):
        """Coder in 'reviewing' with recently launched review -> no change (grace period)."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        w._recently_launched["review-abc"] = time.time()  # just launched
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "reviewing"  # unchanged
        assert launched == []

    def test_reviewing_with_expired_recently_launched_reverts(self, tmp_path):
        """Coder in 'reviewing' with expired grace period -> revert."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")
        w._recently_launched["review-abc"] = time.time() - 9999  # expired
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "waiting:review"

    def test_review_session_in_reviewing_not_affected(self, tmp_path):
        """Review session (not coder) in 'reviewing' status is NOT handled here."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "review-abc", status="reviewing", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((sd / "state.json").read_text())
        # Review sessions in "reviewing" are not handled by the new code path
        assert state["status"] == "reviewing"
        assert launched == []


# ---------------------------------------------------------------------------
# Verdict recovery tests — coder stuck in "reviewing" with completed review
# ---------------------------------------------------------------------------

def _make_completed_review_session(sessions_dir, coder_sid, verdict="approve",
                                   issue_id="issue-abc"):
    """Create a review session dir that looks like a completed review with a verdict."""
    review_sid = f"review-{coder_sid}"
    review_dir = sessions_dir / review_sid
    review_dir.mkdir(exist_ok=True)

    state = {
        "issue_id": issue_id,
        "branch": f"review/{coder_sid}",
        "status": "waiting:review",
        "step": 1,
        "checkpoints": [],
        "human_answers": [],
        "completed_at": "2026-04-04T10:00:00",
    }
    (review_dir / "state.json").write_text(json.dumps(state))

    # Write a conversation log with the verdict
    entry = {"role": "assistant", "content": f"@nightshift {verdict}"}
    (review_dir / "conversation.jsonl").write_text(json.dumps(entry) + "\n")

    return review_dir


class TestVerdictRecovery:
    """When coder is 'reviewing' and review completed with a verdict,
    recover the verdict instead of blindly reverting to waiting:review."""

    def test_reviewing_with_completed_review_processes_verdict_approve(self, tmp_path):
        """Coder transitions from reviewing to waiting:human-review on approve verdict."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")
        _make_completed_review_session(w.sessions_dir, "abc", verdict="approve")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((coder_sd / "state.json").read_text())
        # Approve verdict -> waiting:human-review (not waiting:review)
        assert state["status"] == "waiting:human-review"
        assert launched == []

    def test_reviewing_with_completed_review_processes_verdict_revise(self, tmp_path):
        """Coder transitions from reviewing to working on revise verdict."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")
        _make_completed_review_session(w.sessions_dir, "abc", verdict="revise")
        launched = []
        # VerdictHandler launches via ReviewOrchestrator's _launch_background
        w.reviews.verdicts._launch_background = lambda cmd, sid: launched.append(sid) or True
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((coder_sd / "state.json").read_text())
        # Revise verdict -> working (coder resumed)
        assert state["status"] == "working"
        assert "abc" in launched  # coder was relaunched

    def test_reviewing_with_running_review_not_touched(self, tmp_path):
        """Coder stays in reviewing when review container is still running."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")
        _make_completed_review_session(w.sessions_dir, "abc", verdict="approve")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        def mock_status(container):
            if "review-" in container:
                return "running"
            return None

        with patch("host.watcher.docker_container_status", side_effect=mock_status):
            w.monitor.check_orphaned_sessions()

        state = json.loads((coder_sd / "state.json").read_text())
        assert state["status"] == "reviewing"  # unchanged
        assert launched == []

    def test_reviewing_no_verdict_reverts_to_waiting_review(self, tmp_path):
        """When review completed but has no verdict, fall back to reverting."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")

        # Create completed review session but with no verdict in conversation
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()
        review_state = {
            "issue_id": "issue-abc",
            "branch": "review/abc",
            "status": "waiting:review",
            "step": 1,
            "checkpoints": [],
            "human_answers": [],
            "completed_at": "2026-04-04T10:00:00",
        }
        (review_dir / "state.json").write_text(json.dumps(review_state))
        # Empty conversation — no verdict
        (review_dir / "conversation.jsonl").write_text("")

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((coder_sd / "state.json").read_text())
        # No verdict found -> falls back to reverting
        assert state["status"] == "waiting:review"
        assert launched == []

    def test_reviewing_review_not_completed_no_review_session_reverts(self, tmp_path):
        """When no review session exists at all, revert to waiting:review."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        coder_sd = _make_session(w.sessions_dir, "abc", status="reviewing",
                                 issue_id="issue-abc")
        # No review session dir at all
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        state = json.loads((coder_sd / "state.json").read_text())
        # No review session -> falls back to reverting
        assert state["status"] == "waiting:review"
        assert launched == []


# ---------------------------------------------------------------------------
# Stale review session cleanup tests
# ---------------------------------------------------------------------------

class TestStaleReviewSessionCleanup:
    """Startup cleanup for review sessions with completed_at set but not yet cleaned up."""

    def test_startup_cleans_stale_review_sessions(self, tmp_path):
        """On startup, review sessions with completed_at set should be cleaned up.

        This is the fix for the race condition where the watcher restarts after
        a review container exits but before cleanup_review_session() is called.
        Without this fix, the watcher loops trying to launch a new review and
        fails with 'session already exists'.
        """
        w = _make_watcher(tmp_path)
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")

        # Set completed_at to simulate a review that finished but wasn't cleaned up
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (review_sd / "state.json").write_text(json.dumps(state))

        # The review session should be cleaned up on startup
        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            w.monitor.cleanup_stale_review_sessions()

        # Review session should be removed
        mock_rmtree.assert_called()
        assert any(str(review_sd) in str(call) for call in mock_rmtree.call_args_list)

    def test_startup_does_not_clean_incomplete_review_sessions(self, tmp_path):
        """Review sessions without completed_at should NOT be cleaned up on startup."""
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review",
                      issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        # No completed_at set

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            w.monitor.cleanup_stale_review_sessions()

        # Review session should NOT be removed
        mock_rmtree.assert_not_called()
        assert review_sd.exists()

    def test_startup_does_not_clean_coder_sessions(self, tmp_path):
        """Coder sessions (non-review) should NOT be cleaned even with completed_at."""
        w = _make_watcher(tmp_path)
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")
        # Set completed_at on coder session (unusual but possible)
        state = json.loads((coder_sd / "state.json").read_text())
        state["completed_at"] = "2026-04-21T16:15:19.552265+00:00"
        (coder_sd / "state.json").write_text(json.dumps(state))

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree"), \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            w.monitor.cleanup_stale_review_sessions()

        # Coder session should NOT be removed
        mock_rmtree.assert_not_called()
        assert coder_sd.exists()


# ---------------------------------------------------------------------------
# Completed review cleanup after verdict tests
# ---------------------------------------------------------------------------

class TestCompletedReviewCleanupAfterVerdict:
    """Auto-cleanup of completed review sessions after verdict processing."""

    def _completed_at(self, seconds_ago: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()

    def test_cleanup_completed_review_after_verdict(self, tmp_path):
        """Completed review sessions should be archived after the coder transitions."""
        w = _make_watcher(tmp_path)
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:human-review",
                                 issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = self._completed_at(120)
        (review_sd / "state.json").write_text(json.dumps(state))

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree") as mock_remove_worktree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            cleaned = w.monitor.cleanup_completed_review_sessions()

        assert cleaned is True
        assert not review_sd.exists()
        archive_dir = w.monitor.repo_dir / ".nightshift" / "archive" / "review-abc"
        assert archive_dir.exists()
        assert (archive_dir / "state.json").exists()
        mock_remove_worktree.assert_called_once()
        assert coder_sd.exists()

    def test_no_cleanup_active_review(self, tmp_path):
        """Active review sessions must not be cleaned up."""
        w = _make_watcher(tmp_path)
        _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = self._completed_at(120)
        (review_sd / "state.json").write_text(json.dumps(state))

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree") as mock_remove_worktree, \
             patch("host.watcher.shutil.rmtree") as mock_rmtree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            cleaned = w.monitor.cleanup_completed_review_sessions()

        assert cleaned is False
        assert review_sd.exists()
        mock_remove_worktree.assert_not_called()
        mock_rmtree.assert_not_called()

    def test_cleanup_only_when_coder_transitioned(self, tmp_path):
        """Completed review cleanup waits until the coder has left waiting:review."""
        w = _make_watcher(tmp_path)
        coder_sd = _make_session(w.sessions_dir, "abc", status="waiting:review",
                                 issue_id="issue-abc")
        review_sd = _make_session(w.sessions_dir, "review-abc", status="waiting:review",
                                  issue_id="issue-abc")
        state = json.loads((review_sd / "state.json").read_text())
        state["completed_at"] = self._completed_at(120)
        (review_sd / "state.json").write_text(json.dumps(state))

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree") as mock_remove_worktree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            cleaned = w.monitor.cleanup_completed_review_sessions()

        assert cleaned is False
        assert review_sd.exists()
        mock_remove_worktree.assert_not_called()

        state = json.loads((coder_sd / "state.json").read_text())
        state["status"] = "working"
        (coder_sd / "state.json").write_text(json.dumps(state))

        with patch("core.config.load_workflow") as mock_lw, \
             patch("host.watcher.remove_worktree") as mock_remove_worktree:
            cfg = MagicMock()
            cfg.workspace.root = ".worktrees"
            mock_lw.return_value = cfg

            cleaned = w.monitor.cleanup_completed_review_sessions()

        assert cleaned is True
        assert not review_sd.exists()
        mock_remove_worktree.assert_called_once()


# ---------------------------------------------------------------------------
# Orphan with @@DONE@@ marker tests
# ---------------------------------------------------------------------------

class TestOrphanWithDoneMarker:
    """When an orphan has @@DONE@@ in conversation but no completed_at,
    it should transition to done:pending-review instead of resuming."""

    def test_orphan_with_done_marker_transitions_to_review(self, tmp_path):
        """Session with @@DONE@@ in conversation but no completed_at transitions to done:pending-review."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create conversation.jsonl with @@DONE@@ marker
        conv_entry = {"role": "assistant", "content": "Task complete @@DONE@@"}
        (sd / "conversation.jsonl").write_text(json.dumps(conv_entry) + "\n")

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        # Should NOT resume
        assert launched == []
        # Should transition to done:pending-review
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "done:pending-review"

    def test_orphan_without_done_marker_resumes_normally(self, tmp_path):
        """Session without @@DONE@@ in conversation should be resumed as before."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create conversation.jsonl without @@DONE@@ marker
        conv_entry = {"role": "assistant", "content": "Still working on it..."}
        (sd / "conversation.jsonl").write_text(json.dumps(conv_entry) + "\n")

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        # Should resume normally
        assert "abc" in launched
        # Status should NOT change to done:pending-review
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] != "done:pending-review"

    def test_orphan_with_done_marker_and_signal_file_transitions(self, tmp_path):
        """Session with signal/done file should also transition to done:pending-review."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create signal/done file (alternative signal mechanism)
        (sd / "signal").mkdir()
        (sd / "signal" / "done").write_text("")

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        # Should NOT resume
        assert launched == []
        # Should transition to done:pending-review
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "done:pending-review"

    def test_orphan_no_conversation_file_resumes_normally(self, tmp_path):
        """Session without conversation.jsonl should be resumed as before."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # No conversation.jsonl file
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        # Should resume normally
        assert "abc" in launched

    def test_orphan_done_marker_logs_transition(self, tmp_path, caplog):
        """When transitioning to done:pending-review, it should log the action."""
        import logging
        caplog.set_level(logging.INFO)
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create conversation.jsonl with @@DONE@@ marker
        conv_entry = {"role": "assistant", "content": "Task complete @@DONE@@"}
        (sd / "conversation.jsonl").write_text(json.dumps(conv_entry) + "\n")

        w.monitor._launch_background = lambda cmd, sid: True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        # Should log the transition
        assert any("@@DONE@@" in record.message and "done:pending-review" in record.message
                   for record in caplog.records)


# ---------------------------------------------------------------------------
# Revise-pending marker tests
# ---------------------------------------------------------------------------

class TestRevisePendingMarker:
    """When an orphan has revise-pending.json, retry the revise launch."""

    def test_orphan_with_revise_pending_retries_launch(self, tmp_path):
        """Session with revise-pending.json should retry the revise launch."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")

        # Create revise-pending.json marker
        review_dir = w.sessions_dir / "review-abc"
        review_dir.mkdir()
        (review_dir / "conversation.jsonl").write_text(
            json.dumps({"content": "@nightshift revise"}) + "\n"
        )
        marker_data = {"issue_id": "issue-abc", "review_dir": str(review_dir)}
        (sd / "revise-pending.json").write_text(json.dumps(marker_data))

        # Create conversation.jsonl with @@DONE@@ (from previous run)
        (sd / "conversation.jsonl").write_text(
            json.dumps({"type": "assistant", "content": "@@DONE@@"}) + "\n"
        )

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        # Should resume via revise handler
        assert "abc" in launched
        # Marker should be removed after successful retry
        assert not (sd / "revise-pending.json").exists()
        # Status should NOT be done:pending-review
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] != "done:pending-review"

    def test_orphan_with_revise_pending_launch_failure_keeps_marker(self, tmp_path):
        """When revise retry launch fails, marker should be kept for next attempt."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="reviewing", issue_id="issue-abc")

        # Create revise-pending.json marker
        marker_data = {"issue_id": "issue-abc", "review_dir": str(tmp_path / "review-abc")}
        (sd / "revise-pending.json").write_text(json.dumps(marker_data))

        # Simulate launch failure
        w.monitor._launch_background = lambda cmd, sid: False

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()

        # Marker should still exist
        assert (sd / "revise-pending.json").exists()
        # Status should NOT transition to done:pending-review
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] != "done:pending-review"


# ---------------------------------------------------------------------------
# Branch verification before resume tests
# ---------------------------------------------------------------------------

class TestBranchVerification:
    """Verify agent branch exists before resuming a session."""

    def test_detects_missing_branch(self, tmp_path):
        """_verify_branch_exists returns False when the branch is missing."""
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())

        # Mock git rev-parse to return non-zero (branch doesn't exist)
        mock_result = MagicMock()
        mock_result.returncode = 128
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = w.monitor._verify_branch_exists(state)

        assert result is False
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "git" in call_args[0][0]
        assert "rev-parse" in call_args[0][0]
        assert "refs/heads/agent/abc" in call_args[0][0]

    def test_detects_existing_branch(self, tmp_path):
        """_verify_branch_exists returns True when the branch exists."""
        w = _make_watcher(tmp_path)
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())

        # Mock git rev-parse to return zero (branch exists)
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = w.monitor._verify_branch_exists(state)

        assert result is True

    def test_skips_resume_on_missing_branch(self, tmp_path):
        """Orphan resume is skipped when the agent branch is missing."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        # Mock git rev-parse to return non-zero (branch doesn't exist)
        mock_result = MagicMock()
        mock_result.returncode = 128

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch("subprocess.run", return_value=mock_result):
            w.monitor.check_orphaned_sessions()

        # Should NOT resume because branch is missing
        assert launched == []
        # Session status should be updated to suspended:branch-missing
        state = json.loads((sd / "state.json").read_text())
        assert state["status"] == "suspended:branch-missing"

    def test_resumes_when_branch_exists(self, tmp_path):
        """Orphan resume proceeds when the agent branch exists."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        # Mock git rev-parse to return zero (branch exists)
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("host.watcher.docker_container_status", return_value=None), \
             patch("subprocess.run", return_value=mock_result):
            w.monitor.check_orphaned_sessions()

        # Should resume because branch exists
        assert "abc" in launched


# ---------------------------------------------------------------------------
# Zombie container detection tests
# ---------------------------------------------------------------------------

class TestZombieContainerDetection:
    """Detect containers that are running but stuck (no events for extended time)."""

    def test_detects_stuck_container(self, tmp_path, caplog):
        """Container running but no events for > stall_timeout * 2 -> warning logged."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create an old raw-output.log (stale for 700s with default 300s stall_timeout)
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        import os
        old_time = time.time() - 700
        os.utime(raw_log, (old_time, old_time))

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should have logged a warning about the stuck container
        assert any("may be stuck" in record.message and "abc" in record.message
                   for record in caplog.records)

    def test_no_alert_on_active_container(self, tmp_path, caplog):
        """Container running with recent events -> no warning."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create a recent raw-output.log (updated just now)
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        # mtime is now by default

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should NOT have logged any warning about stuck container
        assert not any("may be stuck" in record.message
                       for record in caplog.records)

    def test_no_alert_when_container_not_running(self, tmp_path, caplog):
        """Container not running (handled by orphan detector) -> no zombie alert."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create an old raw-output.log
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        import os
        old_time = time.time() - 700
        os.utime(raw_log, (old_time, old_time))

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_zombie_containers()

        # Should NOT log zombie warning (orphan detector handles non-running containers)
        assert not any("may be stuck" in record.message
                       for record in caplog.records)

    def test_no_alert_without_raw_output_log(self, tmp_path, caplog):
        """No raw-output.log file -> no zombie alert (just started)."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        # No raw-output.log created

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should NOT log zombie warning
        assert not any("may be stuck" in record.message
                       for record in caplog.records)

    def test_skipped_within_check_interval(self, tmp_path, caplog):
        """Zombie check skipped if called within the check interval."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = time.time()  # just checked
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create an old raw-output.log
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        import os
        old_time = time.time() - 700
        os.utime(raw_log, (old_time, old_time))

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should NOT log warning because we're within the check interval
        assert not any("may be stuck" in record.message
                       for record in caplog.records)

    def test_non_working_status_skipped(self, tmp_path, caplog):
        """Sessions not in working/starting status are skipped."""
        import logging
        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_zombie_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="waiting:review", issue_id="issue-abc")

        # Create an old raw-output.log
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        import os
        old_time = time.time() - 700
        os.utime(raw_log, (old_time, old_time))

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should NOT log zombie warning for non-working session
        assert not any("may be stuck" in record.message
                       for record in caplog.records)

    def test_notifies_telegram_on_stuck_container(self, tmp_path):
        """Stuck container detection should also notify via Telegram."""
        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_zombie_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        # Create an old raw-output.log
        raw_log = sd / "raw-output.log"
        raw_log.write_text("some output\n")
        import os
        old_time = time.time() - 700
        os.utime(raw_log, (old_time, old_time))

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        with patch("host.watcher.docker_container_status", return_value="running"):
            w.monitor.check_zombie_containers()

        # Should have sent a Telegram notification
        assert any("stuck" in n.lower() or "zombie" in n.lower() for n in notified)


# ---------------------------------------------------------------------------
# Session directory size monitoring tests
# ---------------------------------------------------------------------------

class TestSessionSizeMonitoring:
    """Warn when session directories grow too large."""

    def test_check_session_size_warns_on_threshold(self, tmp_path, caplog, monkeypatch):
        """Sessions exceeding the warning threshold should log a warning."""
        import logging
        from host.constants import SIZE_WARNING_THRESHOLD_MB

        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path)
        w.monitor._last_session_size_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        def fake_walk(path):
            assert path == sd
            yield (str(path), [], ["raw-output.log"])

        monkeypatch.setattr("host.watcher.session_monitor.os.walk", fake_walk)
        monkeypatch.setattr(
            "host.watcher.session_monitor.os.path.getsize",
            lambda path: (SIZE_WARNING_THRESHOLD_MB + 1) * 1024 * 1024,
        )

        w.monitor.check_session_sizes()

        assert any("session size" in record.message.lower() and "abc" in record.message
                   for record in caplog.records)

    def test_check_session_size_alerts_on_critical(self, tmp_path, monkeypatch):
        """Sessions exceeding the critical threshold should send a Telegram alert."""
        from host.constants import SIZE_CRITICAL_THRESHOLD_MB

        w = _make_watcher(tmp_path, tg_enabled=True)
        w.monitor._last_session_size_check = 0.0
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")

        def fake_walk(path):
            assert path == sd
            yield (str(path), [], ["raw-output.log"])

        monkeypatch.setattr("host.watcher.session_monitor.os.walk", fake_walk)
        monkeypatch.setattr(
            "host.watcher.session_monitor.os.path.getsize",
            lambda path: (SIZE_CRITICAL_THRESHOLD_MB + 1) * 1024 * 1024,
        )

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        w.monitor.check_session_sizes()

        assert any("session size" in msg.lower() and "abc" in msg for msg in notified)


# ---------------------------------------------------------------------------
# Runaway resume monitoring tests
# ---------------------------------------------------------------------------

class TestRunawayResumeMonitoring:
    """Warn before sessions hit the hard orphan-resume limit."""

    def test_detect_runaway_resumes_warns_at_threshold(self, tmp_path, caplog):
        """Sessions at the warning threshold should log and notify once."""
        import logging
        from host.constants import RUNAWAY_RESUME_WARNING_THRESHOLD

        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path, tg_enabled=True)
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["orphan_resumes"] = RUNAWAY_RESUME_WARNING_THRESHOLD
        (sd / "state.json").write_text(json.dumps(state))

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        w.monitor.check_runaway_sessions()

        assert any(
            record.name == "watcher"
            and "runaway" in record.message.lower()
            and "abc" in record.message
            for record in caplog.records
        )
        assert any("abc" in msg for msg in notified)

    def test_detect_runaway_no_alert_below_threshold(self, tmp_path, caplog):
        """Sessions below the warning threshold should not alert."""
        import logging
        from host.constants import RUNAWAY_RESUME_WARNING_THRESHOLD

        caplog.set_level(logging.WARNING)
        w = _make_watcher(tmp_path, tg_enabled=True)
        sd = _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        state = json.loads((sd / "state.json").read_text())
        state["orphan_resumes"] = RUNAWAY_RESUME_WARNING_THRESHOLD - 1
        (sd / "state.json").write_text(json.dumps(state))

        notified = []
        w.telegram.notify = lambda msg, **kw: notified.append(msg)

        w.monitor.check_runaway_sessions()

        assert not any(
            record.name == "watcher"
            and "runaway" in record.message.lower()
            and "abc" in record.message
            for record in caplog.records
        )
        assert notified == []

    def test_detect_runaway_checks_all_sessions(self, tmp_path, monkeypatch):
        """check_runaway_sessions should inspect every active session."""
        w = _make_watcher(tmp_path, tg_enabled=True)
        checked = []

        sessions = [
            (w.sessions_dir / "abc", {"status": "working", "orphan_resumes": 5}),
            (w.sessions_dir / "def", {"status": "starting", "orphan_resumes": 6}),
            (w.sessions_dir / "ghi", {"status": "waiting:review", "orphan_resumes": 7}),
        ]

        monkeypatch.setattr(w.monitor, "iter_session_states", lambda: sessions)

        def fake_check(session_dir, sid, state, now):
            checked.append(sid)

        monkeypatch.setattr(w.monitor, "_check_session_for_runaway", fake_check, raising=False)

        w.monitor.check_runaway_sessions()

        assert checked == ["abc", "def"]


# ---------------------------------------------------------------------------
# Missing session directory handling tests
# ---------------------------------------------------------------------------

class TestMissingSessionDirHandled:
    """Watcher should handle missing session directories gracefully.

    Bug scenario:
    1. Issue is accepted (code merged)
    2. Tracker update fails (lock contention)
    3. Session directory is cleaned up
    4. Watcher sees open issue with nightshift label
    5. Tries to resume → crash with FileNotFoundError
    """

    def test_orphan_check_skips_missing_state_json(self, tmp_path):
        """Orphan check should skip if state.json doesn't exist."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0

        # Create session directory but no state.json
        sd = w.sessions_dir / "abc"
        sd.mkdir()
        assert not (sd / "state.json").exists()

        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        with patch("host.watcher.docker_container_status", return_value=None):
            w.monitor.check_orphaned_sessions()  # should not crash

        assert launched == []

    def test_resume_session_skips_missing_dir(self, tmp_path):
        """_resume_session should handle missing session directory gracefully."""
        w = _make_watcher(tmp_path)
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        # Session directory does NOT exist
        missing_dir = w.sessions_dir / "abc"
        assert not missing_dir.exists()

        # Should not crash - will try to post lifecycle comment but skip on error
        w.monitor._resume_session("abc", "issue-abc", reason="test")

        # Should still launch (the launch itself will handle missing session)
        assert "abc" in launched


# ---------------------------------------------------------------------------
# SSM-5: StateManager usage tests
# ---------------------------------------------------------------------------

class TestStateManagerUsage:
    """Verify session_monitor uses StateManager instead of raw JSON reads."""

    def test_orphan_check_uses_state_manager(self, tmp_path):
        """Orphan detection should use StateManager for loading state."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        state_managers_loaded = []

        # Patch StateManager to track when it's loaded
        original_state_manager = __import__('core.state', fromlist=['StateManager']).StateManager
        class TrackedStateManager(original_state_manager):
            def __init__(self, session_dir):
                state_managers_loaded.append(str(session_dir))
                super().__init__(session_dir)

        with patch("host.watcher.session_monitor.StateManager", TrackedStateManager), \
             patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        # StateManager should have been used to load the session state
        assert any("abc" in path for path in state_managers_loaded), \
            f"StateManager should be used for session 'abc', got: {state_managers_loaded}"

    def test_consistent_status_read(self, tmp_path):
        """Status should be read via StateManager.status property for SSM consistency."""
        w = _make_watcher(tmp_path)
        w.monitor._last_orphan_check = 0.0
        _make_session(w.sessions_dir, "abc", status="working", issue_id="issue-abc")
        launched = []
        w.monitor._launch_background = lambda cmd, sid: launched.append(sid) or True

        status_reads = []

        # Patch StateManager.status property to track reads
        original_state_manager = __import__('core.state', fromlist=['StateManager']).StateManager
        class TrackedStateManager(original_state_manager):
            @property
            def status(self):
                result = super().status
                status_reads.append(result)
                return result

        with patch("host.watcher.session_monitor.StateManager", TrackedStateManager), \
             patch("host.watcher.docker_container_status", return_value=None), \
             patch.object(w.monitor, "_verify_branch_exists", return_value=True):
            w.monitor.check_orphaned_sessions()

        # Status should have been read via the status property
        assert "working" in status_reads, \
            f"Status should be read via StateManager.status, got: {status_reads}"
