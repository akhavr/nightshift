"""Session state machine with validated transitions."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Literal

HookType = Literal["enter", "exit"]
HookCallback = Callable[[dict], None]


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""


# Terminal states: session is fully done, no further transitions allowed
TERMINAL_STATES: frozenset[str] = frozenset({
    "accepted",
    "rejected",
    "closed",
})

# Completion states: coder finished work, but session can still resume
# (e.g., rebase conflict, revise verdict). These have completed_at set
# but can transition back to working.
COMPLETION_STATES: frozenset[str] = frozenset({
    "waiting:review",
    "waiting:human-review",
})


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
    "suspended:branch-missing",
    "suspended:too-complex",
    "suspended:review-failed",
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
    ("working", "reviewing"),  # revert on failed revise launch
    # starting -> working (normal startup)
    ("starting", "working"),
    # starting -> suspended states (early failures)
    ("starting", "suspended:auth-failure"),
    ("starting", "suspended:hook-failure"),
    ("starting", "suspended:too-complex"),
    # starting -> done (immediate completion in tests/edge cases)
    ("starting", "done:pending-review"),
    ("starting", "waiting:review"),
    # starting -> rejected (discard stuck session)
    ("starting", "rejected"),
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
    ("working", "suspended:review-failed"),  # review session hits orphan limit
    ("working", "cancelled:external"),
    ("working", "error:merge-conflict"),
    # waiting:question -> working (answer received)
    ("waiting:question", "working"),
    ("waiting:question", "suspended:answer-ready"),
    # waiting:review -> reviewing (review started)
    ("waiting:review", "reviewing"),
    ("waiting:review", "waiting:review"),  # self-transition for error recovery
    # waiting:review -> working (resumed for revision)
    ("waiting:review", "working"),
    # waiting:review -> accepted/rejected (human decision)
    ("waiting:review", "accepted"),
    ("waiting:review", "rejected"),
    ("waiting:review", "error:merge-conflict"),  # conflict markers during accept
    # waiting:review -> human-review (escalation)
    ("waiting:review", "waiting:human-review"),
    # waiting:human-review -> accepted/rejected
    ("waiting:human-review", "accepted"),
    ("waiting:human-review", "rejected"),
    ("waiting:human-review", "working"),
    ("waiting:human-review", "error:merge-conflict"),  # conflict markers during accept
    # reviewing -> waiting:review (review done)
    ("reviewing", "reviewing"),  # self-transition for re-launch
    ("reviewing", "waiting:review"),
    ("reviewing", "waiting:human-review"),
    ("reviewing", "working"),  # revise verdict -> resume coder
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
    # suspended:* -> rejected (discard everything)
    ("suspended:max-resumes", "rejected"),
    ("suspended:auth-failure", "rejected"),
    ("suspended:auth-failure-permanent", "rejected"),
    ("suspended:too-complex", "rejected"),
    ("suspended:review-no-verdict", "rejected"),
    # suspended:branch-missing -> working (manual resume after branch recreated)
    ("suspended:branch-missing", "working"),
    # suspended:too-complex -> working (manual resume after task split)
    ("suspended:too-complex", "working"),
    # suspended:review-failed -> working (manual resume)
    ("suspended:review-failed", "working"),
    # working -> new suspended states
    ("working", "suspended:branch-missing"),
    ("working", "suspended:too-complex"),
    # reviewing -> suspended:review-failed
    ("reviewing", "suspended:review-failed"),
    # fallback to suspended:unexpected (safety net for unhandled states)
    ("suspended:hook-failure", "suspended:unexpected"),
    ("suspended:max-resumes", "suspended:unexpected"),
    ("suspended:review-no-verdict", "suspended:unexpected"),
    ("suspended:branch-missing", "suspended:unexpected"),
    ("suspended:too-complex", "suspended:unexpected"),
    ("suspended:review-failed", "suspended:unexpected"),
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
        self._hooks: dict[str, dict[HookType, list[HookCallback]]] = defaultdict(
            lambda: {"enter": [], "exit": []}
        )

    @property
    def state(self) -> str:
        return self._state

    def register_hook(
        self, state: str, event: HookType, callback: HookCallback
    ) -> None:
        """Register a callback to be invoked on state enter or exit.

        Args:
            state: The state to attach the hook to
            event: 'enter' (called when entering state) or 'exit' (called when leaving)
            callback: Function receiving context dict with from_state, to_state, timestamp
        """
        self._hooks[state][event].append(callback)

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
        from_state = self._state
        ctx = {
            "from_state": from_state,
            "to_state": to_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for hook in self._hooks[from_state]["exit"]:
            hook(ctx)
        self._state = to_state
        for hook in self._hooks[to_state]["enter"]:
            hook(ctx)
