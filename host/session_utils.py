"""Shared session state I/O and path helpers for host-side modules.

Eliminates duplicated state.json read/write, worktree cleanup, and
force-remove patterns across cli.py, launch.py, and watcher.py.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

from core.state import _validate_state, state_lock
from core.state_machine import SessionStateMachine
from host.constants import ARCHIVE_DIR, MAX_ORPHAN_RESUMES
from host.rebase import CONTAINER_GIT_PATH, _fix_container_gitdir

log = logging.getLogger(__name__)

# Files to preserve when archiving a session
ARCHIVE_FILES = ("conversation.jsonl", "state.json", "raw-output.log")


# ── Path helpers ─────────────────────────────────────────

def _git_repo_root() -> Path:
    """Return the git repository root based on git commands (internal helper).

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
    ).stdout.strip()).resolve()


def get_repo_root() -> Path:
    """Return the nightshift repo root with symlink resolution and validation.

    1. Gets git repo root and resolves symlinks
    2. Validates .nightshift/ exists at that location
    3. If not found, walks up from cwd to find .nightshift/
    4. Raises RuntimeError if no .nightshift/ found anywhere
    """
    root = _git_repo_root()
    if (root / ".nightshift").exists():
        return root

    # Walk up from cwd to find .nightshift/
    for parent in Path.cwd().resolve().parents:
        if (parent / ".nightshift").exists():
            return parent

    raise RuntimeError(
        "No .nightshift/ found. Run from a nightshift repo or use --repo flag."
    )


def sessions_dir(repo: Path | None = None) -> Path:
    """Return the sessions directory path."""
    if repo is None:
        repo = get_repo_root()
    return repo / ".nightshift" / "sessions"


# ── State I/O ────────────────────────────────────────────

def read_state(session_dir: Path, *, max_orphan_resumes: int | None = MAX_ORPHAN_RESUMES) -> dict:
    """Read and parse state.json from a session directory.

    By default, orphan resume counts above the hard auto-resume cap are
    normalized to the default state value. Callers that need the raw count
    for monitoring can pass ``max_orphan_resumes=None`` to preserve larger
    values while still validating type/shape.
    """
    raw_state = json.loads((session_dir / "state.json").read_text())
    state, warnings = _validate_state(raw_state, max_orphan_resumes=max_orphan_resumes)
    if warnings:
        log.warning(
            "Validated state.json in %s with issues: %s",
            session_dir,
            "; ".join(warnings),
        )
    return state


def write_state(session_dir: Path, state: dict) -> None:
    """Atomically write state.json to a session directory."""
    state_file = session_dir / "state.json"
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(state_file)


def update_status(session_dir: Path, status: str) -> None:
    """Read state.json, validate and update the status field via SSM, and write back (locked).

    All status changes go through the SessionStateMachine for validation.
    Raises InvalidTransition if the transition is not allowed.
    """
    with state_lock(session_dir):
        state = read_state(session_dir)
        ssm = SessionStateMachine(initial_state=state.get("status", "starting"))
        ssm.transition(status)  # validates transition
        state["status"] = ssm.state
        write_state(session_dir, state)


def increment_orphan_resumes(session_dir: Path) -> int:
    """Atomically increment orphan_resumes and return the new value (locked)."""
    with state_lock(session_dir):
        state = read_state(session_dir)
        state["orphan_resumes"] = state.get("orphan_resumes", 0) + 1
        write_state(session_dir, state)
        return state["orphan_resumes"]


def increment_auth_retries(session_dir: Path) -> int:
    """Atomically increment auth_retries and return the new value (locked)."""
    with state_lock(session_dir):
        state = read_state(session_dir)
        state["auth_retries"] = state.get("auth_retries", 0) + 1
        write_state(session_dir, state)
        return state["auth_retries"]


def increment_provider_outage_retries(session_dir: Path) -> int:
    """Atomically increment provider_outage_retries and return the new value (locked)."""
    with state_lock(session_dir):
        state = read_state(session_dir)
        state["provider_outage_retries"] = state.get("provider_outage_retries", 0) + 1
        write_state(session_dir, state)
        return state["provider_outage_retries"]


def update_state_fields(session_dir: Path, **fields) -> None:
    """Atomically update arbitrary fields in state.json (locked).

    If 'status' is in fields, the transition is validated via SSM.
    Raises InvalidTransition if the status transition is not allowed.
    """
    with state_lock(session_dir):
        state = read_state(session_dir)
        if "status" in fields:
            ssm = SessionStateMachine(initial_state=state.get("status", "starting"))
            ssm.transition(fields["status"])  # validates transition
            fields["status"] = ssm.state
        state.update(fields)
        write_state(session_dir, state)


def clear_completed_at(session_dir: Path) -> None:
    """Clear completed_at when resuming from a completion state.

    When a session in waiting:review or waiting:human-review is resumed
    (e.g., revise verdict), completed_at must be cleared so the orphan
    detector doesn't treat it as a completed session that crashed.
    """
    with state_lock(session_dir):
        state = read_state(session_dir)
        if "completed_at" in state:
            del state["completed_at"]
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


# ── Safe worktree prune (WT-6) ──────────────────────────

# Session statuses that indicate active work (should not prune while these exist)
ACTIVE_STATUSES = {"working", "starting", "running", "reviewing"}


def get_active_session_ids(repo: Path) -> list[str]:
    """Return list of session IDs that are actively running (working, starting, reviewing)."""
    sessions = repo / ".nightshift" / "sessions"
    if not sessions.exists():
        return []

    active_ids = []
    for session_dir in sessions.iterdir():
        state_file = session_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text())
            status = state.get("status", "")
            if status in ACTIVE_STATUSES:
                active_ids.append(state.get("issue_id", session_dir.name))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read %s: %s", state_file, e)
            continue
    return active_ids


def has_active_sessions(repo: Path) -> bool:
    """Check if any session is actively running (working, starting, reviewing)."""
    return len(get_active_session_ids(repo)) > 0


def fix_all_corrupted_gitdirs(repo: Path) -> None:
    """Fix all worktrees with corrupted .git files pointing to container paths.

    After container exit, the .git file may point to /repo-git/... instead of
    the host path. This scans all worktrees and fixes them before any prune.
    """
    # Get list of worktrees
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode != 0:
        log.warning("Failed to list worktrees: %s", result.stderr)
        return

    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt_path = Path(line.split(" ", 1)[1])
        _fix_container_gitdir(wt_path, repo)


def safe_prune(repo: Path) -> None:
    """Safely prune worktrees with defense-in-depth.

    WT-6: Before calling global prune:
    1. Fix all corrupted .git files (pointing to /repo-git/)
    2. Skip prune if any session is active (working, starting, reviewing)

    This prevents collateral damage where prune deletes metadata for
    worktrees that appear orphaned due to corrupted gitdir paths.
    """
    # Defense 1: Fix corrupted .git files before prune can see them as orphaned
    fix_all_corrupted_gitdirs(repo)

    # Defense 2: Don't prune if sessions are actively running
    if has_active_sessions(repo):
        log.debug("Skipping worktree prune: active sessions exist")
        return

    # Log worktrees before prune
    before = subprocess.run(["git", "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, cwd=str(repo))
    log.info(f"Worktree prune starting. Current worktrees:\n{before.stdout.strip()}")

    # Now safe to prune
    result = subprocess.run(["git", "worktree", "prune", "-v"],
                            capture_output=True, text=True, cwd=str(repo))
    if result.stdout.strip():
        log.warning(f"Worktree prune removed:\n{result.stdout.strip()}")
    else:
        log.debug("Worktree prune: nothing to remove")


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
    """Remove a git worktree and its branch, handling broken .git and root-owned files.

    WT-6: Does NOT call global `git worktree prune` because it can cause
    collateral damage to other worktrees with corrupted .git files. The
    `git worktree remove --force` command is sufficient for specific removal.
    """
    if wt.exists():
        # Fix corrupted gitdir before attempting remove (WT-6)
        _fix_container_gitdir(wt, repo)
        result = subprocess.run(
            ["git", "worktree", "remove", str(wt), "--force"],
            capture_output=True, cwd=str(repo),
        )
        if result.returncode != 0:
            force_remove_dir(wt)
    # WT-6: No global prune here - it can delete metadata for other worktrees
    subprocess.run(["git", "branch", "-D", branch],
                   capture_output=True, cwd=str(repo))
