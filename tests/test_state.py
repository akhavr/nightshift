"""Tests for core/state.py — atomic session state operations."""

import json
import threading
import time
from pathlib import Path

import pytest

from core.state import StateManager, SessionState
from core.state_machine import InvalidTransition


class TestMarkDone:
    """Tests for the atomic mark_done() method."""

    def test_mark_done_sets_both_atomically(self, tmp_path):
        """mark_done() should set status and completed_at in a single write."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        sm.mark_done("waiting:review")

        final = sm.load_state()
        assert final.status == "waiting:review"
        assert final.completed_at != ""

    def test_mark_done_single_file_write(self, tmp_path, monkeypatch):
        """mark_done() should only write once (not load+write twice)."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        write_count = [0]
        original_write = sm._write

        def counting_write(st):
            write_count[0] += 1
            original_write(st)

        monkeypatch.setattr(sm, "_write", counting_write)

        sm.mark_done("waiting:review")
        assert write_count[0] == 1

    def test_concurrent_read_during_mark_done(self, tmp_path):
        """Even with concurrent readers, mark_done() should not lose fields.

        This test simulates the race condition where a watcher reads and writes
        state between two separate update calls. With mark_done(), both fields
        are set atomically so the watcher can't see a partial state.
        """
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        results = {"status": None, "completed_at": None}

        def reader():
            for _ in range(100):
                st = sm.load_state()
                if st.status == "waiting:review" or st.completed_at:
                    results["status"] = st.status
                    results["completed_at"] = st.completed_at
                    break
                time.sleep(0.001)

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

        sm.mark_done("waiting:review")

        reader_thread.join(timeout=1)

        if results["status"] == "waiting:review":
            assert results["completed_at"] != "", \
                "Race detected: status changed but completed_at empty"
        if results["completed_at"]:
            assert results["status"] == "waiting:review", \
                "Race detected: completed_at set but status not updated"

    def test_mark_done_preserves_other_fields(self, tmp_path):
        """mark_done() should not lose other state fields."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)
        sm.add_checkpoint("did stuff", 1, "abc1234")
        sm.add_qa("question?", "answer!")

        sm.mark_done("waiting:review")

        final = sm.load_state()
        assert final.issue_id == "abc123"
        assert final.branch == "agent/abc123"
        assert len(final.checkpoints) == 1
        assert len(final.human_answers) == 1



class TestPersistentSSM:
    """Tests for SSM-2: StateManager owns a persistent SSM instance."""

    def test_state_manager_has_ssm_instance(self, tmp_path):
        """StateManager has _ssm attribute that is a SessionStateMachine."""
        from core.state_machine import SessionStateMachine

        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        # Trigger SSM initialization by calling load_state
        sm.load_state()

        assert hasattr(sm, "_ssm")
        assert isinstance(sm._ssm, SessionStateMachine)

    def test_status_property_reads_from_ssm(self, tmp_path):
        """StateManager.status property returns self._ssm.state."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        # Access status property
        status = sm.status

        assert status == "working"
        # Verify it came from SSM, not direct file read
        assert sm._ssm is not None
        assert sm._ssm.state == status

    def test_update_status_uses_persistent_ssm(self, tmp_path, monkeypatch):
        """update_status() calls self._ssm.transition() (not creating new SSM)."""
        from core.state_machine import SessionStateMachine

        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="starting")
        sm._write(state)

        # Initialize SSM by loading state
        sm.load_state()
        original_ssm = sm._ssm

        # Track transition calls
        transition_calls = []
        original_transition = original_ssm.transition

        def tracking_transition(to_state):
            transition_calls.append(to_state)
            original_transition(to_state)

        monkeypatch.setattr(original_ssm, "transition", tracking_transition)

        # Call update_status
        sm.update_status("working")

        # Verify persistent SSM was used
        assert sm._ssm is original_ssm  # Same instance
        assert "working" in transition_calls  # transition() was called


class TestUpdateStatusValidation:
    """Tests for SSM-validated status transitions."""

    def test_update_status_rejects_invalid_transition(self, tmp_path):
        """update_status() should raise InvalidTransition for disallowed transition."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="starting")
        sm._write(state)

        with pytest.raises(InvalidTransition) as exc_info:
            sm.update_status("accepted")
        assert "starting" in str(exc_info.value)
        assert "accepted" in str(exc_info.value)

        final = sm.load_state()
        assert final.status == "starting"

    def test_update_status_accepts_valid_transition(self, tmp_path):
        """update_status() should succeed for valid transitions."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="starting")
        sm._write(state)

        sm.update_status("working")

        final = sm.load_state()
        assert final.status == "working"

    def test_update_status_working_to_question(self, tmp_path):
        """working -> waiting:question should succeed."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        sm.update_status("waiting:question")

        assert sm.load_state().status == "waiting:question"

    def test_ssm_initialized_from_loaded_status(self, tmp_path):
        """StateManager's SSM should be initialized from the loaded state."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="working")
        sm._write(state)

        sm2 = StateManager(session_dir)
        sm2.update_status("waiting:review")

        assert sm2.load_state().status == "waiting:review"
