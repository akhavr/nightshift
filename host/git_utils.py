"""Git command utilities shared across host modules."""

import subprocess
from pathlib import Path


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
