"""Directory workspace manager — uses an existing directory as-is.

Used inside containers where the host has already set up the workspace
(e.g. created a git worktree and mounted it at /workspace).
No worktree creation, no branch management — just commit/diff operations.
"""

import logging
import subprocess
from pathlib import Path

from core.protocols import WorkspaceManager, Workspace, TrackerIssue

log = logging.getLogger(__name__)


class DirectoryManager:
    def __init__(self, repo_root: Path, base_branch: str = "master", **kwargs):
        self.repo_root = Path(repo_root)
        self.base_branch = base_branch

    def create(self, issue: TrackerIssue) -> Workspace:
        branch = self._current_branch()
        return Workspace(path=self.repo_root, branch=branch, is_new=False)

    def cleanup(self, issue: TrackerIssue) -> None:
        pass  # host manages cleanup

    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None:
        self.commit(self.repo_root, f"fix: resolve {issue.identifier} — {issue.title}")

    def commit(self, workspace: Path, message: str) -> None:
        self._git_in(workspace, "add", "-A")
        if self.has_changes(workspace):
            self._git_in(workspace, "commit", "-m", message)

    def has_changes(self, workspace: Path) -> bool:
        return subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=str(workspace),
        ).returncode != 0

    def diff_stat(self, workspace: Path, base: str = "master") -> str:
        try:
            return subprocess.check_output(
                ["git", "diff", base, "--stat"],
                cwd=str(workspace), stderr=subprocess.DEVNULL,
            ).decode().strip() or "No changes"
        except subprocess.CalledProcessError:
            return "No changes"

    def get_current_commit(self, workspace: Path) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(workspace), stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "none"

    def run_hook(self, workspace: Path, script: str | None,
                 timeout_s: int = 60) -> bool:
        if not script:
            return True
        try:
            subprocess.run(
                ["sh", "-c", script], cwd=str(workspace),
                timeout=timeout_s, check=True,
                capture_output=True, text=True,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning(f"Hook failed: {e}")
            return False

    def _current_branch(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "branch", "--show-current"],
                cwd=str(self.repo_root), stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "unknown"

    def _git_in(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        ).stdout.strip()
