"""Shared session state I/O and path helpers for host-side modules.

Eliminates duplicated state.json read/write, worktree cleanup, and
force-remove patterns across cli.py, launch.py, and watcher.py.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# ── Path helpers ─────────────────────────────────────────

def get_repo_root() -> Path:
    """Return the git repository root."""
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def sessions_dir(repo: Path | None = None) -> Path:
    """Return the sessions directory path."""
    if repo is None:
        repo = get_repo_root()
    return repo / ".nightshift" / "sessions"


# ── State I/O ────────────────────────────────────────────

def read_state(session_dir: Path) -> dict:
    """Read and parse state.json from a session directory."""
    return json.loads((session_dir / "state.json").read_text())


def write_state(session_dir: Path, state: dict) -> None:
    """Atomically write state.json to a session directory."""
    state_file = session_dir / "state.json"
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(state_file)


def update_status(session_dir: Path, status: str) -> None:
    """Read state.json, update the status field, and write back."""
    state = read_state(session_dir)
    state["status"] = status
    write_state(session_dir, state)


# ── Worktree cleanup ────────────────────────────────────

def force_remove_dir(path: Path) -> None:
    """Remove a directory, handling root-owned files from Docker."""
    try:
        shutil.rmtree(path)
    except (PermissionError, OSError):
        subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{path}:/cleanup:rw",
             "ubuntu:24.04", "rm", "-rf", "/cleanup"],
            capture_output=True,
        )
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass


def remove_worktree(repo: Path, wt: Path, branch: str) -> None:
    """Remove a git worktree and its branch, handling broken .git and root-owned files."""
    if wt.exists():
        result = subprocess.run(
            ["git", "worktree", "remove", str(wt), "--force"],
            capture_output=True, cwd=str(repo),
        )
        if result.returncode != 0:
            force_remove_dir(wt)
    subprocess.run(["git", "worktree", "prune"],
                   capture_output=True, cwd=str(repo))
    subprocess.run(["git", "branch", "-D", branch],
                   capture_output=True, cwd=str(repo))
