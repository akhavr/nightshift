"""Workspace transaction helpers for host-side worktree integrity checks."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)
_thread_state = threading.local()


class WorktreeCorruptError(RuntimeError):
    """Raised when a worktree's git metadata is missing or invalid."""


class TransactionError(RuntimeError):
    """Raised when workspace transactions are nested in the same thread."""


class WorkspaceTransaction:
    """Context manager that temporarily rewrites a worktree's .git pointer."""

    def __init__(self, worktree_path: Path):
        self.worktree_path = Path(worktree_path)
        self.active = False
        self._git_file = self.worktree_path / ".git"
        self._original_git_content: str | None = None
        self.rollback_stack: list[Callable[[], None]] = []

    def __enter__(self) -> "WorkspaceTransaction":
        if getattr(_thread_state, "active_transaction", None) is not None:
            raise TransactionError(
                "WorkspaceTransaction cannot be nested in the same thread"
            )

        self._original_git_content = self._git_file.read_text()
        _thread_state.active_transaction = self
        self.active = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if exc_type is not None:
                self._run_rollback_stack()
            if self._original_git_content is not None:
                self._git_file.write_text(self._original_git_content)
        finally:
            self.active = False
            if getattr(_thread_state, "active_transaction", None) is self:
                _thread_state.active_transaction = None
        return False

    def rewrite_git_pointer(self, new_content: str) -> None:
        self._git_file.write_text(new_content)

    def create_branch(self, name: str, start_point: str | None = None) -> None:
        """Create a branch and register a rollback delete operation."""
        args = ["git", "branch", name]
        if start_point is not None:
            args.append(start_point)
        self._run_git(args)
        self.rollback_stack.append(
            lambda: self._run_git(["git", "branch", "-D", name])
        )

    def checkout(self, branch: str) -> None:
        """Checkout a branch and register a rollback restore operation."""
        previous_branch = self._current_branch()
        self._run_git(["git", "checkout", branch])
        if previous_branch:
            self.rollback_stack.append(
                lambda: self._run_git(["git", "checkout", previous_branch])
            )

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(
            args,
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"{' '.join(args)} failed: {stderr}")
        return result

    def _current_branch(self) -> str:
        """Return the current branch for this worktree."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _run_rollback_stack(self) -> None:
        while self.rollback_stack:
            rollback = self.rollback_stack.pop()
            try:
                rollback()
            except Exception:
                log.exception("WorkspaceTransaction rollback step failed")


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
