"""Git command utilities shared across host modules."""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def fetch_and_resolve_ref(repo: Path, branch: str) -> str:
    """Fetch a branch from origin and return the best available ref.

    Fetches `origin/<branch>`, then checks if the remote ref exists.
    Returns `origin/<branch>` if available, otherwise falls back to the
    local `<branch>` ref.
    """
    subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=str(repo), capture_output=True, text=True,
    )
    fetch_ok = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{branch}"],
        cwd=str(repo), capture_output=True,
    ).returncode == 0
    return f"origin/{branch}" if fetch_ok else branch


def detect_default_branch(repo: Path) -> str:
    """Detect the default branch (main, master, etc.)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def current_branch(repo: Path) -> str:
    """Get the current branch name."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=str(repo),
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def branch_exists(repo: Path, branch: str) -> bool:
    """Check if a git branch exists."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True, text=True, cwd=str(repo),
    )
    return result.returncode == 0


def merge_no_ff(repo: Path, ref: str, message: str) -> subprocess.CompletedProcess:
    """Perform a --no-ff merge."""
    return subprocess.run(
        ["git", "merge", "--no-ff", ref, "-m", message],
        capture_output=True, text=True, cwd=str(repo),
    )


def diff_stat(repo: Path, base: str, head: str) -> str:
    """Get diff --stat between two refs."""
    result = subprocess.run(
        ["git", "diff", "--stat", f"{base}..{head}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    return result.stdout.strip() if result.returncode == 0 else "N/A"


def audit_worktree_symlinks(
    worktree_path: Path,
    workspace_root: Path | None = None,
) -> list[tuple[Path, Path]]:
    """Return symlinks in a worktree whose targets resolve outside /workspace."""
    workspace_root = Path("/workspace") if workspace_root is None else workspace_root
    workspace_root = workspace_root.resolve()
    escaping_symlinks: list[tuple[Path, Path]] = []
    git_env = os.environ.copy()
    git_env.pop("GIT_DIR", None)
    git_env.pop("GIT_WORK_TREE", None)

    result = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=str(worktree_path), capture_output=True, text=True, env=git_env,
    )
    tracked_files = {line.split()[-1] for line in result.stdout.splitlines()}

    for dirpath, dirnames, filenames in os.walk(worktree_path):
        if ".git" in dirnames:
            dirnames.remove(".git")

        for name in (*dirnames, *filenames):
            rel_path = Path(dirpath).relative_to(worktree_path) / name
            if str(rel_path) not in tracked_files:
                continue

            symlink_path = Path(dirpath) / name
            if not symlink_path.is_symlink():
                continue

            target_path = symlink_path.resolve(strict=False)
            if not target_path.is_relative_to(workspace_root):
                escaping_symlinks.append((symlink_path, target_path))

    return escaping_symlinks


_FSCK_NOISE_SUBSTRINGS = (
    "Unknown object type",
    "Could not read",
    "fatal: not a git repository",
    "git-bug",
    "dangling ",
)


def validate_git_objects(git_dir: Path) -> tuple[bool, list[str]]:
    """Run git fsck to validate git objects.

    Args:
        git_dir: Path to .git directory or worktree git dir

    Returns:
        (is_valid, real_errors) tuple. is_valid is True if no corruption found.
        real_errors is a list of error lines that are not known noise.
    """
    if not git_dir.is_dir():
        return True, []

    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "fsck", "--connectivity-only"],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        return True, []

    details = result.stderr or result.stdout or ""
    real_errors = [
        line for line in details.splitlines()
        if line.strip() and not any(skip in line for skip in _FSCK_NOISE_SUBSTRINGS)
    ]

    if real_errors:
        return False, real_errors
    return True, []


def auto_commit_dirty_worktree(worktree: Path, message: str = "WIP: uncommitted agent changes") -> bool:
    """Auto-commit any uncommitted changes in the worktree.

    Args:
        worktree: Path to the git worktree
        message: Commit message to use

    Returns:
        True if a commit was made, False if nothing to commit or worktree doesn't exist
    """
    if not worktree.is_dir():
        return False

    # Check if there are uncommitted changes
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    if status_result.returncode != 0:
        return False

    if not status_result.stdout.strip():
        return False

    # Stage all changes
    add_result = subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    if add_result.returncode != 0:
        logger.warning("git add failed: %s", add_result.stderr)
        return False

    # Commit
    commit_result = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, text=True, cwd=str(worktree),
    )
    if commit_result.returncode != 0:
        logger.warning("git commit failed: %s", commit_result.stderr)
        return False

    return True
