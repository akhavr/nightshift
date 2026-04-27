"""Workspace transaction helpers for host-side worktree integrity checks."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class WorktreeCorruptError(RuntimeError):
    """Raised when a worktree's git metadata is missing or invalid."""


def repair_worktree(worktree_path: Path) -> None:
    """Run `git worktree repair` for the given worktree."""
    result = subprocess.run(
        ["git", "worktree", "repair"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorktreeCorruptError(
            f"git worktree repair failed for {worktree_path}: {result.stderr.strip()}"
        )


def _read_gitdir(worktree_path: Path) -> Path:
    """Read and normalize the gitdir path from a worktree's .git file."""
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        raise WorktreeCorruptError(f"Missing .git file in worktree: {worktree_path}")

    try:
        content = git_file.read_text().strip()
    except OSError as exc:
        raise WorktreeCorruptError(f"Cannot read {git_file}: {exc}") from exc

    if not content.startswith("gitdir:"):
        raise WorktreeCorruptError(f"Invalid .git file in {worktree_path}: {content!r}")

    gitdir_value = content.split(":", 1)[1].strip()
    if not gitdir_value:
        raise WorktreeCorruptError(f"Empty gitdir pointer in {git_file}")

    gitdir = Path(gitdir_value)
    if not gitdir.is_absolute():
        gitdir = (worktree_path / gitdir).resolve()
    return gitdir


def _is_worktree_metadata_dir(worktree_path: Path, gitdir: Path) -> bool:
    """Return True when gitdir matches `.git/worktrees/<worktree-name>/`."""
    return (
        gitdir.name == worktree_path.name
        and gitdir.parent.name == "worktrees"
        and gitdir.parent.parent.name == ".git"
    )


def check_worktree_integrity(worktree_path: Path, auto_repair: bool = False) -> bool:
    """Verify that a worktree points at existing metadata.

    A valid worktree must have a .git file that points at an existing metadata
    directory, typically `.git/worktrees/<name>/`.
    """
    gitdir = _read_gitdir(worktree_path)

    if gitdir.exists():
        if _is_worktree_metadata_dir(worktree_path, gitdir):
            return True
        raise WorktreeCorruptError(
            f"Invalid worktree metadata location for {worktree_path}: {gitdir}"
        )

    if auto_repair:
        log.warning("Repairing missing worktree metadata for %s", worktree_path)
        repair_worktree(worktree_path)
        return True

    raise WorktreeCorruptError(
        f"Missing worktree metadata for {worktree_path}: {gitdir}"
    )
