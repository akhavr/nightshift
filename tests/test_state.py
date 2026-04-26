"""Tests for core/state.py — atomic session state operations."""

import json
import threading
import time
from pathlib import Path

import pytest

from core.state import StateManager, SessionState, state_lock
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


class TestStateLocking:
    """Tests for file locking to prevent read-modify-write races."""

    def test_state_lock_creates_lock_file(self, tmp_path):
        """state_lock should create state.json.lock file."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        with state_lock(session_dir):
            assert (session_dir / "state.json.lock").exists()

    def test_state_lock_is_exclusive(self, tmp_path):
        """Two concurrent state_lock calls should serialize."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        order = []

        def worker(name: str, delay: float):
            with state_lock(session_dir):
                order.append(f"{name}-start")
                time.sleep(delay)
                order.append(f"{name}-end")

        t1 = threading.Thread(target=worker, args=("A", 0.05))
        t2 = threading.Thread(target=worker, args=("B", 0.05))

        t1.start()
        time.sleep(0.01)
        t2.start()

        t1.join()
        t2.join()

        assert order == ["A-start", "A-end", "B-start", "B-end"], \
            f"Lock did not serialize access: {order}"

    def test_concurrent_increments_with_locking(self, tmp_path):
        """Concurrent increment_step calls should not lose updates."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc", status="working")
        sm._write(state)

        num_threads = 10
        increments_per_thread = 5

        def incrementer():
            for _ in range(increments_per_thread):
                sm.increment_step()

        threads = [threading.Thread(target=incrementer) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = sm.load_state()
        expected = num_threads * increments_per_thread
        assert final.step == expected, f"Lost increments: {final.step} != {expected}"

    def test_concurrent_status_updates_with_locking(self, tmp_path):
        """Concurrent update_status calls should not corrupt state."""
        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc", status="starting")
        sm._write(state)
        sm.update_status("working")

        errors = []

        def add_checkpoints(n: int):
            for i in range(n):
                try:
                    sm.add_checkpoint(f"cp-{i}", i)
                except Exception as e:
                    errors.append(str(e))

        def update_usage(n: int):
            for i in range(n):
                try:
                    sm.update_usage(100, 50, 0.01)
                except Exception as e:
                    errors.append(str(e))

        t1 = threading.Thread(target=add_checkpoints, args=(5,))
        t2 = threading.Thread(target=update_usage, args=(5,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent operations failed: {errors}"

        final = sm.load_state()
        assert len(final.checkpoints) == 5
        assert final.usage.input_tokens == 500
        assert final.usage.output_tokens == 250


class TestTransitionLogging:
    """Tests for SSM-4: State transitions auto-log."""

    def test_transition_logs_state_change(self, tmp_path, caplog):
        """StateManager should log state transitions."""
        import logging

        session_dir = tmp_path / "session"
        sm = StateManager(session_dir)
        state = SessionState(issue_id="abc123", branch="agent/abc123", status="starting")
        sm._write(state)

        with caplog.at_level(logging.INFO):
            sm.update_status("working")

        assert any(
            "starting" in record.message and "working" in record.message
            for record in caplog.records
        ), f"Expected log with starting->working, got: {[r.message for r in caplog.records]}"
