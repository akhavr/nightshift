"""Shared session state I/O and path helpers for host-side modules.

Eliminates duplicated state.json read/write, worktree cleanup, and
force-remove patterns across cli.py, launch.py, and watcher.py.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

from host.constants import ARCHIVE_DIR

log = logging.getLogger(__name__)

# Files to preserve when archiving a session
ARCHIVE_FILES = ("conversation.jsonl", "state.json", "raw-output.log")


# ── Path helpers ─────────────────────────────────────────

def get_repo_root() -> Path:
    """Return the git repository root (main repo, not worktree).

    Uses --git-common-dir which returns the same path for both the main
    repo and all its worktrees, ensuring socket paths resolve correctly
    when CLI commands are run from worktrees.

    Falls back to --show-toplevel when git dir is external (e.g., Docker
    bind-mounted git dirs that aren't named .git).
    """
    git_common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    common_path = Path(git_common).resolve()
    # Standard layout: .git dir is inside repo, so parent is repo root
    if common_path.name == ".git":
        return common_path.parent
    # External git dir (e.g., /repo-git in Docker): fall back to show-toplevel
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


# ── Session archival ────────────────────────────────────

def archive_session(session_dir: Path, repo: Path | None = None) -> Path | None:
    """Copy key session files to .nightshift/archive/<session-id>/ before cleanup.

    Returns the archive directory path, or None if session_dir doesn't exist.
    """
    if not session_dir.exists():
        return None

    if repo is None:
        repo = get_repo_root()

    session_id = session_dir.name
    archive_dir = repo / ".nightshift" / ARCHIVE_DIR / session_id
    archive_dir.mkdir(parents=True, exist_ok=True)

    for filename in ARCHIVE_FILES:
        src = session_dir / filename
        if src.exists():
            shutil.copy2(src, archive_dir / filename)

    log.info("Archived session %s to %s", session_id, archive_dir)
    return archive_dir


# ── Duplicate detection ─────────────────────────────────

# Import review session prefix from constants
from host.constants import REVIEW_SESSION_PREFIX


def find_existing_session_by_prefix(sessions_root: Path, issue_id: str,
                                    step: str = "coder") -> str | None:
    """Check if any existing session has an issue_id that is a prefix match.

    Returns the existing issue_id if found, None otherwise.
    Two IDs match if one starts with the other (handles both
    short-prefix and full-ID lookups).

    Args:
        sessions_root: Path to the sessions directory.
        issue_id: The issue ID to check for duplicates.
        step: Either "coder" or "review". When "review", only checks for
              existing review sessions (ignores coder sessions). When
              "coder", only checks for existing coder sessions (ignores
              review sessions).
    """
    if not sessions_root.exists():
        return None

    is_review_launch = (step == "review")

    for session_dir in sessions_root.iterdir():
        # Skip sessions of the wrong type
        session_is_review = session_dir.name.startswith(REVIEW_SESSION_PREFIX)
        if is_review_launch and not session_is_review:
            # Launching review but found coder session — skip it
            continue
        if not is_review_launch and session_is_review:
            # Launching coder but found review session — skip it
            continue

        state_file = session_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read %s: %s", state_file, e)
            continue
        existing_id = state.get("issue_id", "")
        if not existing_id:
            continue
        if existing_id.startswith(issue_id) or issue_id.startswith(existing_id):
            return existing_id
    return None


def _issue_id_prefix_match(issue_id: str, existing_ids: set[str]) -> bool:
    """Return True if issue_id matches any existing ID by prefix."""
    return any(
        eid.startswith(issue_id) or issue_id.startswith(eid)
        for eid in existing_ids
    )


# ── Worktree cleanup ────────────────────────────────────

def force_remove_dir(path: Path) -> None:
    """Remove a directory, handling root-owned files from Docker.

    Uses ignore_errors=True to handle race conditions where subdirs disappear
    during iteration (e.g., concurrent git/fuse-overlayfs modifications).
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
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
