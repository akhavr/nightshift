"""Session state machine with validated transitions."""


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""


STATES: frozenset[str] = frozenset({
    "starting",
    "working",
    "waiting:question",
    "waiting:review",
    "waiting:human-review",
    "suspended:auth-failure",
    "suspended:auth-failure-permanent",
    "suspended:provider-overload",
    "suspended:provider-overload-permanent",
    "suspended:context-limit",
    "suspended:stall",
    "suspended:hook-failure",
    "suspended:max-resumes",
    "suspended:unexpected",
    "suspended:answer-ready",
    "suspended:review-no-verdict",
    "done:pending-review",
    "cancelled:external",
    "reviewing",
    "accepted",
    "rejected",
    "closed",
    "error:merge-conflict",
})

TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    # self-transitions (no-op resets/confirmations)
    ("working", "working"),
    # starting -> working (normal startup)
    ("starting", "working"),
    # starting -> suspended states (early failures)
    ("starting", "suspended:auth-failure"),
    ("starting", "suspended:hook-failure"),
    # starting -> done (immediate completion in tests/edge cases)
    ("starting", "done:pending-review"),
    ("starting", "waiting:review"),
    # working -> waiting states
    ("working", "waiting:question"),
    ("working", "waiting:review"),
    ("working", "done:pending-review"),
    # working -> suspended states
    ("working", "suspended:auth-failure"),
    ("working", "suspended:auth-failure-permanent"),
    ("working", "suspended:provider-overload"),
    ("working", "suspended:context-limit"),
    ("working", "suspended:stall"),
    ("working", "suspended:hook-failure"),
    ("working", "suspended:max-resumes"),
    ("working", "suspended:unexpected"),
    ("working", "suspended:answer-ready"),
    ("working", "suspended:review-no-verdict"),
    ("working", "cancelled:external"),
    # waiting:question -> working (answer received)
    ("waiting:question", "working"),
    ("waiting:question", "suspended:answer-ready"),
    # waiting:review -> reviewing (review started)
    ("waiting:review", "reviewing"),
    # waiting:review -> working (resumed for revision)
    ("waiting:review", "working"),
    # waiting:review -> accepted/rejected (human decision)
    ("waiting:review", "accepted"),
    ("waiting:review", "rejected"),
    # waiting:review -> human-review (escalation)
    ("waiting:review", "waiting:human-review"),
    # waiting:human-review -> accepted/rejected
    ("waiting:human-review", "accepted"),
    ("waiting:human-review", "rejected"),
    ("waiting:human-review", "working"),
    # reviewing -> waiting:review (review done)
    ("reviewing", "waiting:review"),
    ("reviewing", "waiting:human-review"),
    # reviewing -> suspended (review failures)
    ("reviewing", "suspended:auth-failure"),
    ("reviewing", "suspended:context-limit"),
    ("reviewing", "suspended:review-no-verdict"),
    # suspended:* -> working (resume)
    ("suspended:auth-failure", "working"),
    ("suspended:auth-failure", "suspended:auth-failure-permanent"),
    ("suspended:auth-failure-permanent", "working"),
    ("suspended:provider-overload", "working"),
    ("suspended:provider-overload", "suspended:provider-overload-permanent"),
    ("suspended:provider-overload-permanent", "working"),
    ("suspended:context-limit", "working"),
    ("suspended:stall", "working"),
    ("suspended:hook-failure", "working"),
    ("suspended:max-resumes", "working"),
    ("suspended:unexpected", "working"),
    ("suspended:answer-ready", "working"),
    ("suspended:review-no-verdict", "waiting:human-review"),
    ("suspended:review-no-verdict", "working"),
    # fallback to suspended:unexpected (safety net for unhandled states)
    ("suspended:hook-failure", "suspended:unexpected"),
    ("suspended:max-resumes", "suspended:unexpected"),
    ("suspended:review-no-verdict", "suspended:unexpected"),
    ("waiting:question", "suspended:unexpected"),
    ("waiting:review", "suspended:unexpected"),
    ("waiting:human-review", "suspended:unexpected"),
    ("reviewing", "suspended:unexpected"),
    ("error:merge-conflict", "suspended:unexpected"),
    ("starting", "suspended:unexpected"),
    # done:pending-review -> waiting:review (normal) or working (rebase conflict)
    ("done:pending-review", "waiting:review"),
    ("done:pending-review", "working"),
    # cancelled:external -> working (resume after cancellation)
    ("cancelled:external", "working"),
    # error:merge-conflict -> working (resume after conflict resolution)
    ("error:merge-conflict", "working"),
    # terminal states (accepted, rejected, closed) have no outgoing transitions
})


class SessionStateMachine:
    """Finite state machine for session lifecycle validation.

    Validates that state transitions follow the allowed graph.
    Raises InvalidTransition for disallowed transitions.
    Raises ValueError for unknown states.
    """

    def __init__(self, initial_state: str = "starting"):
        if initial_state not in STATES:
            raise ValueError(f"unknown state: {initial_state}")
        self._state = initial_state

    @property
    def state(self) -> str:
        return self._state

    def can_transition(self, to_state: str) -> bool:
        """Check if transition to to_state is valid without changing state."""
        if to_state not in STATES:
            return False
        return (self._state, to_state) in TRANSITIONS

    def transition(self, to_state: str) -> None:
        """Transition to a new state.

        Raises:
            ValueError: if to_state is not a known state
            InvalidTransition: if the transition is not allowed
        """
        if to_state not in STATES:
            raise ValueError(f"unknown state: {to_state}")
        if not self.can_transition(to_state):
            raise InvalidTransition(
                f"invalid transition: {self._state} -> {to_state}"
            )
        self._state = to_state
