"""Git worktree workspace manager."""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from core.protocols import WorkspaceManager, Workspace, TrackerIssue, RebaseResult

HOOK_TIMEOUT_S = 60

log = logging.getLogger(__name__)


class GitWorktreeManager:
    def __init__(self, repo_root: Path, worktree_root: Path | None = None,
                 base_branch: str = "master"):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root) if worktree_root else self.repo_root / ".worktrees"
        self.base_branch = base_branch

    def create(self, issue: TrackerIssue) -> Workspace:
        sid = self._sanitize(issue.identifier)
        branch = f"agent/{sid}"
        wt = self.worktree_root / f"agent-{sid}"
        is_new = not wt.exists()
        if is_new:
            self._git("branch", branch, self.base_branch)
            self._git("worktree", "add", str(wt), branch)
        return Workspace(path=wt, branch=branch, is_new=is_new)

    def cleanup(self, issue: TrackerIssue) -> None:
        sid = self._sanitize(issue.identifier)
        wt = self.worktree_root / f"agent-{sid}"
        if wt.exists():
            self._git("worktree", "remove", str(wt), "--force")
        self._git("branch", "-D", f"agent/{sid}")

    def finalize(self, issue: TrackerIssue, target_branch: str = "master") -> None:
        """Merge the agent branch into target.

        IMPORTANT: The merge runs from repo_root (the main working tree),
        NOT from the worktree. You cannot checkout a branch in a worktree
        if it's already checked out elsewhere. The main working tree should
        have the target branch checked out.
        """
        sid = self._sanitize(issue.identifier)
        branch = f"agent/{sid}"
        wt = self.worktree_root / f"agent-{sid}"

        # Commit any remaining changes in the worktree
        self.commit(wt, f"fix: resolve {sid} — {issue.title}")

        # Merge from repo_root (main working tree)
        # Ensure we're on the target branch in the main tree
        current = self._git("branch", "--show-current")
        if current != target_branch:
            self._git("checkout", target_branch)

        self._git(
            "merge", "--no-ff", branch, "-m",
            f"Merge {branch}: {issue.title}\n\nResolved {issue.id}",
        )

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

    def rebase(self, workspace: Path, base_branch: str = "master") -> RebaseResult:
        """Fetch latest base branch and rebase the worktree branch onto it."""
        # Fetch latest from remote (ignore failure — remote may not exist)
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=str(workspace), capture_output=True, text=True,
        )
        # Use fetched remote ref if available, otherwise local base branch
        rebase_target = f"origin/{base_branch}" if fetch_result.returncode == 0 else base_branch

        result = subprocess.run(
            ["git", "rebase", rebase_target],
            cwd=str(workspace), capture_output=True, text=True,
        )
        if result.returncode == 0:
            return RebaseResult(success=True)

        # Collect conflict details before aborting
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(workspace), capture_output=True, text=True,
        )
        conflict_files = diff_result.stdout.strip()
        details = f"Rebase failed.\nstderr: {result.stderr.strip()}"
        if conflict_files:
            details += f"\nConflicting files:\n{conflict_files}"

        # Abort the failed rebase to restore a clean state
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(workspace), capture_output=True, text=True,
        )
        return RebaseResult(success=False, conflict_details=details)

    def run_hook(self, workspace: Path, script: str | None,
                 timeout_s: int = HOOK_TIMEOUT_S) -> bool:
        """Run a hook script in the workspace. Returns True on success."""
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

    def _sanitize(self, s: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", s)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(self.repo_root),
            capture_output=True, text=True,
        ).stdout.strip()

    def _git_in(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        ).stdout.strip()
