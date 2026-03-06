"""Protocol mocks for testing core modules without real adapters."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Iterator

from core.protocols import (
    CodingAgent, IssueTracker, Notifier, WorkspaceManager,
    TrackerIssue, TrackerComment, AgentEvent, AgentEventType,
    Workspace,
)


class MockAgent:
    """Mock CodingAgent that yields scripted events."""

    def __init__(self, events: list[AgentEvent] | None = None):
        self.events = events or []
        self.started = False
        self.terminated = False
        self.inputs_sent: list[str] = []
        self._pid = 12345

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        self.started = True
        self.terminated = False
        self.last_prompt = prompt
        self.last_workspace = workspace
        self.last_max_turns = max_turns

    def stream_events(self) -> Iterator[AgentEvent]:
        yield from self.events

    def send_input(self, text: str) -> None:
        self.inputs_sent.append(text)

    def is_alive(self) -> bool:
        return self.started and not self.terminated

    def terminate(self) -> None:
        self.terminated = True

    @property
    def pid(self) -> int | None:
        return self._pid if self.started else None


class MockTracker:
    """Mock IssueTracker that stores state in memory."""

    def __init__(self, issues: dict[str, TrackerIssue] | None = None):
        self.issues = issues or {}
        self.comments: dict[str, list[TrackerComment]] = {}
        self.synced = 0

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        return self.issues.get(issue_id)

    def list_issues(self, status=None) -> list[TrackerIssue]:
        issues = list(self.issues.values())
        if isinstance(status, str):
            issues = [i for i in issues if i.status == status]
        elif isinstance(status, list):
            issues = [i for i in issues if i.status in status]
        return issues

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        return self.comments.get(issue_id, [])

    def add_comment(self, issue_id: str, body: str) -> None:
        self.comments.setdefault(issue_id, []).append(
            TrackerComment(author="agent", body=body)
        )

    def set_status(self, issue_id: str, status: str) -> None:
        if issue_id in self.issues:
            # TrackerIssue is a dataclass, we can replace it
            old = self.issues[issue_id]
            self.issues[issue_id] = TrackerIssue(
                id=old.id, identifier=old.identifier, title=old.title,
                body=old.body, status=status, labels=old.labels,
            )

    def add_label(self, issue_id: str, label: str) -> None:
        if issue_id in self.issues:
            self.issues[issue_id].labels.append(label)

    def remove_label(self, issue_id: str, label: str) -> None:
        if issue_id in self.issues:
            labels = self.issues[issue_id].labels
            if label in labels:
                labels.remove(label)

    def sync(self) -> None:
        self.synced += 1


class MockNotifier:
    """Mock Notifier that records all calls."""

    def __init__(self):
        self.notifications: list[str] = []
        self.questions: list[dict] = []
        self.pending_answers: dict[str, str] = {}

    def notify(self, message: str) -> None:
        self.notifications.append(message)

    def send_question(self, issue_id: str, question: str, short_id: str = "") -> bool:
        self.questions.append({
            "issue_id": issue_id, "question": question, "short_id": short_id,
        })
        return True

    def check_answer(self, issue_id: str) -> Optional[str]:
        return self.pending_answers.pop(issue_id, None)

    def clear_pending(self, issue_id: str) -> None:
        self.pending_answers.pop(issue_id, None)


class MockWorkspaceManager:
    """Mock WorkspaceManager using temp directories."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.created: list[str] = []
        self.cleaned: list[str] = []
        self.finalized: list[str] = []
        self.commits: list[str] = []

    def create(self, issue: TrackerIssue) -> Workspace:
        ws_path = self.tmp_path / f"ws-{issue.identifier}"
        ws_path.mkdir(exist_ok=True)
        is_new = issue.identifier not in self.created
        self.created.append(issue.identifier)
        return Workspace(path=ws_path, branch=f"agent/{issue.identifier}", is_new=is_new)

    def cleanup(self, issue: TrackerIssue) -> None:
        self.cleaned.append(issue.identifier)

    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None:
        self.finalized.append(issue.identifier)

    def commit(self, workspace: Path, message: str) -> None:
        self.commits.append(message)

    def has_changes(self, workspace: Path) -> bool:
        return True

    def diff_stat(self, workspace: Path, base: str = "master") -> str:
        return "1 file changed, 10 insertions(+)"

    def get_current_commit(self, workspace: Path) -> str:
        return "abc1234"

    def run_hook(self, workspace: Path, script: str | None, timeout_s: int = 60) -> bool:
        return True


def make_test_issue(
    issue_id: str = "test-001",
    title: str = "Fix the widget",
    body: str = "The widget is broken. Please fix it.",
    status: str = "open",
    labels: list[str] | None = None,
) -> TrackerIssue:
    return TrackerIssue(
        id=issue_id,
        identifier=issue_id[:12],
        title=title,
        body=body,
        status=status,
        labels=labels or [],
    )
