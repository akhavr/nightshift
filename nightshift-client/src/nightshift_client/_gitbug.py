"""Git-bug CLI wrapper with lock retry."""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

from nightshift_client.exceptions import TrackerError

_CMD_TIMEOUT_S = 30
_LOCK_RETRY_ATTEMPTS = 6
_LOCK_RETRY_BASE_DELAY_S = 1


class GitBug:
    """Wrapper around git-bug CLI with lock retry and exponential backoff."""

    def __init__(self, repo_path: str | Path = "."):
        """Initialize GitBug wrapper.

        Args:
            repo_path: Path to the git repository containing .git/git-bug data.
        """
        self.repo_path = str(repo_path)

    def _run(
        self,
        *args: str,
        timeout: int = _CMD_TIMEOUT_S,
        ignore_rc: Optional[set[int]] = None,
    ) -> str:
        """Run a git-bug command with lock retry.

        Args:
            *args: Arguments to pass to git-bug.
            timeout: Command timeout in seconds.
            ignore_rc: Set of return codes to treat as success.

        Returns:
            Command stdout, stripped.

        Raises:
            TrackerError: On command failure or timeout.
        """
        cmd = ["git-bug", *args]
        ignore_rc = ignore_rc or set()

        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                result = subprocess.run(
                    cmd,
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

                if result.returncode == 0 or result.returncode in ignore_rc:
                    return result.stdout.strip()

                if "already locked by the process pid" in result.stderr:
                    if attempt < _LOCK_RETRY_ATTEMPTS - 1:
                        delay = _LOCK_RETRY_BASE_DELAY_S * (2**attempt)
                        time.sleep(delay)
                        continue
                    raise TrackerError(
                        f"git-bug {args[0]} failed after {_LOCK_RETRY_ATTEMPTS} "
                        f"retries due to lock contention"
                    )

                raise TrackerError(
                    f"git-bug {args[0]} failed (rc={result.returncode}): "
                    f"{result.stderr.strip()}"
                )

            except subprocess.TimeoutExpired:
                raise TrackerError(
                    f"git-bug {args[0]} timed out after {timeout}s"
                )

        raise TrackerError(
            f"git-bug {args[0]} failed after {_LOCK_RETRY_ATTEMPTS} retries "
            f"due to lock contention"
        )

    def add(
        self,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> str:
        """Create a new issue.

        Args:
            title: Issue title.
            body: Issue body/description.
            labels: Optional list of labels to apply.

        Returns:
            The new issue ID.

        Raises:
            TrackerError: If issue creation fails.
        """
        cmd = ["bug", "add", "-t", title, "-m", body]

        for label in labels or []:
            cmd.extend(["-l", label])

        result = self._run(*cmd)

        if not result:
            raise TrackerError("Failed to create issue: no issue ID returned")

        return result

    def comment(self, issue_id: str, text: str) -> None:
        """Add a comment to an issue.

        Args:
            issue_id: Issue ID (full or prefix).
            text: Comment text.

        Raises:
            TrackerError: If adding comment fails.
        """
        try:
            self._run("bug", "comment", "new", issue_id, "-m", text)
        except TrackerError as e:
            raise TrackerError(f"Failed to add comment to {issue_id}: {e}") from e

    def label(self, issue_id: str, label_name: str) -> None:
        """Add a label to an issue.

        Args:
            issue_id: Issue ID (full or prefix).
            label_name: Label to add.

        Raises:
            TrackerError: If adding label fails (except 'already set').
        """
        self._run("bug", "label", "new", issue_id, label_name, ignore_rc={1})

    def push(self) -> None:
        """Push git-bug data to remote.

        Raises:
            TrackerError: If push fails.
        """
        self._run("push")

    def pull(self) -> None:
        """Pull git-bug data from remote.

        Raises:
            TrackerError: If pull fails.
        """
        self._run("pull")

    def list(self, labels: Optional[list[str]] = None) -> list[dict]:
        """List issues, optionally filtered by labels.

        Args:
            labels: Optional list of labels to filter by.

        Returns:
            List of issue dicts with id, title, etc.

        Raises:
            TrackerError: If listing fails or output cannot be parsed.
        """
        args = ["bug", "-f", "json"]
        for label in labels or []:
            args.append(f"label:{label}")

        output = self._run(*args)
        if not output:
            return []

        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise TrackerError(f"Failed to parse git-bug output: {e}") from e
