"""Tests for core/state_machine.py — session state machine validation."""

import pytest

from core.state_machine import (
    SessionStateMachine,
    InvalidTransition,
    STATES,
    TRANSITIONS,
)


class TestSessionStateMachine:
    """Tests for SessionStateMachine class."""

    def test_initial_state_is_starting(self):
        """Default initial state should be 'starting'."""
        ssm = SessionStateMachine()
        assert ssm.state == "starting"

    def test_valid_transition_succeeds(self):
        """Valid transitions should change state without raising."""
        ssm = SessionStateMachine()
        assert ssm.state == "starting"
        ssm.transition("working")
        assert ssm.state == "working"

    def test_invalid_transition_raises_error(self):
        """Invalid transitions should raise InvalidTransition."""
        ssm = SessionStateMachine()
        assert ssm.state == "starting"
        with pytest.raises(InvalidTransition) as exc_info:
            ssm.transition("accepted")
        assert "starting" in str(exc_info.value)
        assert "accepted" in str(exc_info.value)
        assert ssm.state == "starting"  # state unchanged

    def test_can_transition_returns_bool(self):
        """can_transition() should return True for valid, False for invalid."""
        ssm = SessionStateMachine()
        assert ssm.can_transition("working") is True
        assert ssm.can_transition("accepted") is False

    def test_unknown_state_raises_value_error(self):
        """Transitioning to an unknown state should raise ValueError."""
        ssm = SessionStateMachine()
        with pytest.raises(ValueError) as exc_info:
            ssm.transition("nonexistent-state")
        assert "nonexistent-state" in str(exc_info.value)

    def test_custom_initial_state(self):
        """SSM can be initialized with a custom state."""
        ssm = SessionStateMachine(initial_state="working")
        assert ssm.state == "working"

    def test_invalid_initial_state_raises(self):
        """Initializing with unknown state should raise ValueError."""
        with pytest.raises(ValueError):
            SessionStateMachine(initial_state="bogus")

    def test_states_set_contains_known_states(self):
        """STATES should contain all expected session states."""
        expected = {
            "starting",
            "working",
            "waiting:question",
            "waiting:review",
            "waiting:human-review",
            "suspended:auth-failure",
            "suspended:context-limit",
            "suspended:stall",
            "reviewing",
            "accepted",
            "rejected",
            "closed",
        }
        assert expected.issubset(STATES)

    def test_transitions_set_has_tuples(self):
        """TRANSITIONS should be a set of (from_state, to_state) tuples."""
        assert isinstance(TRANSITIONS, (set, frozenset))
        for t in TRANSITIONS:
            assert isinstance(t, tuple)
            assert len(t) == 2
            from_state, to_state = t
            assert from_state in STATES
            assert to_state in STATES

    def test_working_to_waiting_question_allowed(self):
        """working -> waiting:question should be allowed."""
        ssm = SessionStateMachine(initial_state="working")
        ssm.transition("waiting:question")
        assert ssm.state == "waiting:question"

    def test_waiting_question_to_working_allowed(self):
        """waiting:question -> working should be allowed (answer received)."""
        ssm = SessionStateMachine(initial_state="waiting:question")
        ssm.transition("working")
        assert ssm.state == "working"

    def test_working_to_waiting_review_allowed(self):
        """working -> waiting:review should be allowed (agent done)."""
        ssm = SessionStateMachine(initial_state="working")
        ssm.transition("waiting:review")
        assert ssm.state == "waiting:review"
