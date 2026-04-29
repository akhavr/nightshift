"""Nightshift client library."""

from pathlib import Path
from typing import Optional

from nightshift_client._gitbug import GitBug
from nightshift_client._state import labels_to_state, STATE_LABEL_MAP
from nightshift_client.exceptions import (
    AuthError,
    NetworkError,
    NightshiftError,
    PushError,
    TrackerError,
)


class NightshiftClient:
    """High-level client for interacting with nightshift via git-bug."""

    def __init__(self, repo_path: str | Path, identity: str):
        """Initialize NightshiftClient.

        Args:
            repo_path: Path to the git repository.
            identity: User identity (e.g., email). Required.

        Raises:
            AuthError: If identity is not provided.
        """
        if not identity:
            raise AuthError("identity is required")
        self.repo_path = str(repo_path)
        self.identity = identity
        self._gitbug = GitBug(repo_path=self.repo_path)

    def create_issue(
        self,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> str:
        """Create a new issue with the nightshift label.

        Args:
            title: Issue title.
            body: Issue body/description.
            labels: Optional additional labels.

        Returns:
            The new issue ID.
        """
        all_labels = list(labels) if labels else []
        if "nightshift" not in all_labels:
            all_labels.append("nightshift")
        return self._gitbug.add(title, body, labels=all_labels)

    def push(self) -> None:
        """Push git-bug data to remote."""
        self._gitbug.push()


__all__ = [
    "AuthError",
    "GitBug",
    "NetworkError",
    "NightshiftClient",
    "NightshiftError",
    "PushError",
    "STATE_LABEL_MAP",
    "TrackerError",
    "labels_to_state",
]
