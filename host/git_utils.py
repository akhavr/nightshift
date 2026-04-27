"""Git command utilities shared across host modules."""

import os
import subprocess
from pathlib import Path


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
