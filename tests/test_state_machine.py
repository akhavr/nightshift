"""Tests for core/state_machine.py — session state machine validation."""

import pytest

from core.state_machine import (
    SessionStateMachine,
    InvalidTransition,
    STATES,
    TRANSITIONS,
    TERMINAL_STATES,
    COMPLETION_STATES,
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


class TestStateCategories:
    """Tests for TERMINAL_STATES and COMPLETION_STATES (SSM-11)."""

    def test_terminal_states_are_subset_of_states(self):
        """TERMINAL_STATES should be a subset of STATES."""
        assert TERMINAL_STATES.issubset(STATES)

    def test_completion_states_are_subset_of_states(self):
        """COMPLETION_STATES should be a subset of STATES."""
        assert COMPLETION_STATES.issubset(STATES)

    def test_terminal_and_completion_are_disjoint(self):
        """TERMINAL_STATES and COMPLETION_STATES should not overlap."""
        assert TERMINAL_STATES.isdisjoint(COMPLETION_STATES)

    def test_terminal_states_content(self):
        """TERMINAL_STATES should contain exactly accepted, rejected, closed."""
        assert TERMINAL_STATES == {"accepted", "rejected", "closed"}

    def test_completion_states_content(self):
        """COMPLETION_STATES should contain waiting:review and waiting:human-review."""
        assert COMPLETION_STATES == {"waiting:review", "waiting:human-review"}

    def test_terminal_states_have_no_outgoing_transitions(self):
        """Terminal states should not have any outgoing transitions."""
        for state in TERMINAL_STATES:
            outgoing = [t for t in TRANSITIONS if t[0] == state]
            assert not outgoing, f"{state} has outgoing transitions: {outgoing}"

    def test_completion_states_can_transition_to_working(self):
        """Completion states should be able to transition back to working."""
        for state in COMPLETION_STATES:
            assert (state, "working") in TRANSITIONS, \
                f"{state} should be able to transition to working"


class TestHooks:
    """Tests for SSM-4: hooks for logging and notifications."""

    def test_on_enter_hook_called(self):
        """Enter hooks should be called when transitioning to the target state."""
        ssm = SessionStateMachine()
        calls = []

        def on_enter(ctx):
            calls.append(("enter", ctx))

        ssm.register_hook("working", "enter", on_enter)
        ssm.transition("working")

        assert len(calls) == 1
        assert calls[0][0] == "enter"

    def test_on_exit_hook_called(self):
        """Exit hooks should be called when transitioning from the source state."""
        ssm = SessionStateMachine(initial_state="working")
        calls = []

        def on_exit(ctx):
            calls.append(("exit", ctx))

        ssm.register_hook("working", "exit", on_exit)
        ssm.transition("waiting:review")

        assert len(calls) == 1
        assert calls[0][0] == "exit"

    def test_hook_receives_context(self):
        """Hooks should receive a context dict with from_state, to_state, and timestamp."""
        ssm = SessionStateMachine()
        received_ctx = []

        def capture_ctx(ctx):
            received_ctx.append(ctx)

        ssm.register_hook("working", "enter", capture_ctx)
        ssm.transition("working")

        assert len(received_ctx) == 1
        ctx = received_ctx[0]
        assert ctx["from_state"] == "starting"
        assert ctx["to_state"] == "working"
        assert "timestamp" in ctx

    def test_multiple_hooks_for_same_state(self):
        """Multiple hooks can be registered for the same state/event."""
        ssm = SessionStateMachine()
        calls = []

        ssm.register_hook("working", "enter", lambda ctx: calls.append("a"))
        ssm.register_hook("working", "enter", lambda ctx: calls.append("b"))
        ssm.transition("working")

        assert calls == ["a", "b"]

    def test_enter_and_exit_hooks_both_fire(self):
        """Both exit hook from source and enter hook to target should fire."""
        ssm = SessionStateMachine(initial_state="working")
        calls = []

        ssm.register_hook("working", "exit", lambda ctx: calls.append("exit-working"))
        ssm.register_hook("waiting:review", "enter", lambda ctx: calls.append("enter-waiting"))
        ssm.transition("waiting:review")

        assert "exit-working" in calls
        assert "enter-waiting" in calls
        # Exit should fire before enter
        assert calls.index("exit-working") < calls.index("enter-waiting")
