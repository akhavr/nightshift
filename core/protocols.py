"""Protocol definitions for all external boundaries.

Core code imports ONLY from this module for external interactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Protocol, Optional, Iterator, Any, runtime_checkable

from core.agent_events import AgentEvent, AgentEventType


SHORT_ID_LEN = 12   # default truncation length for issue IDs


# ── Usage Tracking ───────────────────────────────────────

@dataclass
class UsageData:
    """Accumulated token usage and cost for a session."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


# ── Issue Tracker ─────────────────────────────────────────

@dataclass
class TrackerIssue:
    id: str
    identifier: str          # human-readable key
    title: str
    body: str
    status: str              # normalized: "open", "closed", etc.
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    priority: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class TrackerComment:
    author: str
    body: str
    created_at: str | None = None


@runtime_checkable
class IssueTracker(Protocol):
    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]: ...
    def list_issues(self, status: str | list[str] | None = None) -> list[TrackerIssue]: ...
    def get_comments(self, issue_id: str) -> list[TrackerComment]: ...
    def add_comment(self, issue_id: str, body: str) -> None: ...
    def set_status(self, issue_id: str, status: str) -> None: ...
    def add_label(self, issue_id: str, label: str) -> None: ...
    def remove_label(self, issue_id: str, label: str) -> None: ...
    def sync(self) -> None:
        """Bidirectional sync — pull remote state AND push local changes."""
        ...

    def run_raw(self, *args: str) -> str:
        """Pass arguments directly to the underlying tracker CLI.

        Used by `nightshift issue` to provide lock-safe CLI passthrough.
        """
        ...


# ── Coding Agent ──────────────────────────────────────────
# AgentEventType and AgentEvent are imported from core.agent_events
# (single source of truth for the unified event stream)


@runtime_checkable
class CodingAgent(Protocol):
    """Interface for a coding agent.

    Contract: implementations must be re-startable after terminate().
    start() must reinitialize all internal state so the same instance
    can be used for multiple sequential sessions (e.g. main run +
    summarization call).
    """
    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None: ...
    def stream_events(self) -> Iterator[AgentEvent]: ...
    def send_input(self, text: str) -> None: ...
    def is_alive(self) -> bool: ...
    def terminate(self) -> None: ...
    signal_method: str
    @property
    def pid(self) -> int | None: ...


# ── Workspace ─────────────────────────────────────────────

@dataclass
class Workspace:
    path: Path
    branch: str | None = None
    is_new: bool = False


@dataclass
class RebaseResult:
    """Outcome of a rebase attempt."""
    success: bool
    conflict_details: str = ""


@runtime_checkable
class WorkspaceManager(Protocol):
    def create(self, issue: TrackerIssue) -> Workspace: ...
    def cleanup(self, issue: TrackerIssue) -> None: ...
    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None: ...
    def commit(self, workspace: Path, message: str) -> None: ...
    def has_changes(self, workspace: Path) -> bool: ...
    def diff_stat(self, workspace: Path, base: str = "master") -> str: ...
    def get_current_commit(self, workspace: Path) -> str: ...
    def rebase(self, workspace: Path, base_branch: str = "master") -> RebaseResult: ...


# ── Notifications ─────────────────────────────────────────

class NotificationLevel(Enum):
    """Severity levels for notifications, ordered by increasing verbosity."""
    QUESTIONS = auto()   # only Q&A needing human input
    ACTIONS = auto()     # questions + done/accept/reject/escalation
    ALL = auto()         # everything (default, backward compat)


def should_notify(configured_level: "NotificationLevel", message_level: "NotificationLevel") -> bool:
    """Return True if a message at message_level should be sent given configured_level."""
    return message_level.value <= configured_level.value


@runtime_checkable
class Notifier(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def notify(self, message: str, *, level: NotificationLevel = NotificationLevel.ALL) -> None: ...
    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool: ...
    def check_answer(self, issue_id: str) -> Optional[str]: ...
    def clear_pending(self, issue_id: str) -> None: ...


# ── Markers ───────────────────────────────────────────────

class MarkerType(Enum):
    LOG = auto()
    CHECKPOINT = auto()
    QUESTION = auto()
    WAITING = auto()
    DONE = auto()


@dataclass
class Marker:
    type: MarkerType
    content: str = ""


DEFAULT_MARKERS: dict[str, MarkerType] = {
    "@@LOG@@": MarkerType.LOG,
    "@@CHECKPOINT@@": MarkerType.CHECKPOINT,
    "@@QUESTION@@": MarkerType.QUESTION,
    "@@WAITING@@": MarkerType.WAITING,
    "@@DONE@@": MarkerType.DONE,
}


def parse_marker(
    text: str, markers: dict[str, MarkerType] | None = None,
) -> Marker | None:
    for token, mt in (markers or DEFAULT_MARKERS).items():
        if token in text:
            return Marker(type=mt, content=text.split(token, 1)[1].strip())
    return None
