"""Host-side pre-review rebase.

Rebases the agent branch onto the latest base branch before launching review.
Runs on the host (not in the container) to avoid bind-mount issues where git
cannot unlink mounted files like WORKFLOW.md.
"""

import logging
import os
import subprocess
from pathlib import Path

from core.workspace_transaction import (
    WorkspaceTransaction,
    RebaseConflictError,
    check_worktree_integrity,
)

log = logging.getLogger("watcher")

TEST_COMMAND_TIMEOUT_S = 120  # timeout for test command execution
CONTAINER_GIT_PATH = "/repo-git/"  # container mount point for .git directory
CONTAINER_WORKTREE_PATH = "/workspace"  # container workspace mount point


def _clean_git_env() -> dict:
    """Return env dict without GIT_DIR/GIT_WORK_TREE to operate on the repo directly."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def sanitize_git_config(repo: Path) -> bool:
    """Remove core.worktree=/workspace if set by container.

    Container may set core.worktree to /workspace in the repo's .git/config.
    This breaks host-side git commands because /workspace doesn't exist on host.

    Returns True if sanitization occurred, False otherwise.
    """
    env = _clean_git_env()
    result = subprocess.run(
        ["git", "config", "--get", "core.worktree"],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        return False  # not set

    worktree_value = result.stdout.strip()
    if worktree_value != CONTAINER_WORKTREE_PATH:
        return False  # set to something else, leave it alone

    # Unset the container worktree path
    unset_result = subprocess.run(
        ["git", "config", "--unset", "core.worktree"],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )
    if unset_result.returncode == 0:
        log.info(f"Sanitized core.worktree (was {CONTAINER_WORKTREE_PATH}) in {repo}")
        return True

    log.warning(f"Failed to unset core.worktree in {repo}: {unset_result.stderr}")
    return False


def _fix_container_gitdir(worktree_path: Path, repo_root: Path | None = None) -> None:
    """Fix gitdir path if corrupted by container (points to /repo-git/).

    After container exit, the trap may not have restored the .git file,
    leaving it pointing to /repo-git/worktrees/... instead of the host path.
    This fixes it before running git commands.
    """
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return  # Not a worktree or doesn't exist

    try:
        content = git_file.read_text()
    except OSError as e:
        log.warning(f"Cannot read {git_file}: {e}")
        return

    if CONTAINER_GIT_PATH not in content:
        return  # Already has a valid host path

    # Extract worktree name from the container path
    # Expected format: "gitdir: /repo-git/worktrees/agent-abc123\n"
    worktree_name = worktree_path.name

    # Derive repo root if not provided
    # Worktree path is typically: <repo_root>/<workspace.root>/<worktree_name>
    # e.g., /home/user/repo/worktrees/agent-abc123
    if repo_root is None:
        repo_root = worktree_path.parent.parent

    host_gitdir = repo_root / ".git" / "worktrees" / worktree_name
    if not host_gitdir.exists():
        log.warning(f"Cannot fix gitdir: {host_gitdir} does not exist")
        return

    new_content = f"gitdir: {host_gitdir}\n"
    try:
        git_file.write_text(new_content)
        log.info(f"Fixed container gitdir in {git_file} -> {host_gitdir}")
    except OSError as e:
        log.warning(f"Cannot write {git_file}: {e}")


def attempt_pre_review_rebase(
    worktree_path: Path,
    base_branch: str,
    test_command: str | None = None,
    test_timeout_s: int = TEST_COMMAND_TIMEOUT_S,
    repo_root: Path | None = None,
) -> str | None:
    """Rebase onto latest base branch and re-run tests.

    Runs on the host side, outside the container, to avoid bind-mount issues.

    Returns None on success, or a resume prompt describing the failure
    so the agent can fix it.
    """
    if not worktree_path.exists():
        log.warning(f"Worktree does not exist: {worktree_path}")
        return None

    # Fix gitdir if container corrupted it (points to /repo-git/)
    _fix_container_gitdir(worktree_path, repo_root)

    # Repair or fail fast before any git command touches a real worktree.
    if (worktree_path / ".git").exists():
        check_worktree_integrity(worktree_path, auto_repair=True)

    # Sanitize core.worktree if container set it to /workspace
    if repo_root:
        sanitize_git_config(repo_root)

    log.info(f"Pre-review rebase onto {base_branch} in {worktree_path}...")
    result = _rebase(worktree_path, base_branch)
    if not result.success:
        log.warning(f"Rebase failed: {result.conflict_details}")
        return _build_rebase_conflict_prompt(base_branch, result)

    if test_command:
        test_failure = _run_test_command(worktree_path, test_command, test_timeout_s)
        if test_failure:
            log.warning(f"Post-rebase tests failed: {test_failure}")
            return _build_test_failure_prompt(base_branch, test_failure)

    log.info("Pre-review rebase succeeded")
    return None


class RebaseResult:
    """Result of a rebase operation."""

    def __init__(self, success: bool, conflict_details: str = ""):
        self.success = success
        self.conflict_details = conflict_details


class MergeResult:
    """Result of a merge operation."""

    def __init__(self, success: bool, conflict_details: str = ""):
        self.success = success
        self.conflict_details = conflict_details


def _stash_changes(worktree_path: Path, label: str) -> bool:
    """Stash uncommitted changes before rebase/merge. Returns True if stash was created."""
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked", "-m", f"pre-{label}-stash"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    return "No local changes" not in stash_result.stdout


def _fetch_and_get_target(worktree_path: Path, base_branch: str) -> str:
    """Fetch latest from remote and return target ref. Falls back to local branch."""
    fetch_result = subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    return f"origin/{base_branch}" if fetch_result.returncode == 0 else base_branch


def _pop_stash_if_needed(worktree_path: Path, had_stash: bool, context: str) -> None:
    """Pop stash if one was created. Logs warning on failure."""
    if not had_stash:
        return
    pop_result = subprocess.run(
        ["git", "stash", "pop"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if pop_result.returncode != 0:
        log.warning(f"Stash pop failed {context}: {pop_result.stderr}")


def _collect_conflict_details(worktree_path: Path, operation: str, stderr: str) -> str:
    """Collect conflicting files and build details string."""
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    conflict_files = diff_result.stdout.strip()
    details = f"{operation} failed.\nstderr: {stderr.strip()}"
    if conflict_files:
        details += f"\nConflicting files:\n{conflict_files}"
    return details


def _format_conflict_details(operation: str, stderr: str, conflicting_files: list[str]) -> str:
    """Build conflict details from a transaction result or exception."""
    details = f"{operation} failed.\nstderr: {stderr.strip()}"
    if conflicting_files:
        details += f"\nConflicting files:\n" + "\n".join(conflicting_files)
    return details


def _rebase(worktree_path: Path, base_branch: str) -> RebaseResult:
    """Fetch latest base branch and rebase the worktree branch onto it."""
    had_stash = _stash_changes(worktree_path, "rebase")
    target = _fetch_and_get_target(worktree_path, base_branch)

    if not (worktree_path / ".git").exists():
        result = subprocess.run(
            ["git", "rebase", target],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        if result.returncode == 0:
            _pop_stash_if_needed(worktree_path, had_stash, "(conflicts?)")
            return RebaseResult(success=True)

        details = _collect_conflict_details(worktree_path, "Rebase", result.stderr)

        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )

        _pop_stash_if_needed(worktree_path, had_stash, "after rebase abort")
        return RebaseResult(success=False, conflict_details=details)

    try:
        with WorkspaceTransaction(worktree_path) as txn:
            txn.rebase(target)
    except RebaseConflictError as exc:
        details = _format_conflict_details("Rebase", exc.stderr, exc.conflicting_files)
        _pop_stash_if_needed(worktree_path, had_stash, "after rebase abort")
        return RebaseResult(success=False, conflict_details=details)

    _pop_stash_if_needed(worktree_path, had_stash, "(conflicts?)")
    return RebaseResult(success=True)


def _merge(worktree_path: Path, base_branch: str) -> MergeResult:
    """Fetch latest base branch and merge it into the worktree branch."""
    had_stash = _stash_changes(worktree_path, "merge")
    target = _fetch_and_get_target(worktree_path, base_branch)

    if not (worktree_path / ".git").exists():
        result = subprocess.run(
            ["git", "merge", target, "-m", f"Merge {target} into agent branch"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        if result.returncode == 0:
            _pop_stash_if_needed(worktree_path, had_stash, "(conflicts?)")
            return MergeResult(success=True)

        details = _collect_conflict_details(worktree_path, "Merge", result.stderr)

        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )

        _pop_stash_if_needed(worktree_path, had_stash, "after merge abort")
        return MergeResult(success=False, conflict_details=details)

    with WorkspaceTransaction(worktree_path) as txn:
        result = txn.merge(target)

    if result.success:
        _pop_stash_if_needed(worktree_path, had_stash, "(conflicts?)")
        return MergeResult(success=True)

    details = _format_conflict_details("Merge", result.stderr, result.conflicting_files)
    _pop_stash_if_needed(worktree_path, had_stash, "after merge abort")
    return MergeResult(success=False, conflict_details=details)


def _run_test_command(
    workspace_path: Path,
    test_command: str,
    timeout_s: int = TEST_COMMAND_TIMEOUT_S,
) -> str | None:
    """Run the test command in the workspace. Returns failure output or None."""
    try:
        result = subprocess.run(
            ["sh", "-c", test_command],
            cwd=str(workspace_path),
            capture_output=True, text=True,
            timeout=timeout_s,
        )
        if result.returncode == 0:
            return None
        output = result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout
        stderr = result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr
        return f"Exit code {result.returncode}\nstdout:\n{output}\nstderr:\n{stderr}"
    except subprocess.TimeoutExpired:
        return f"Test command timed out after {timeout_s}s"
    except Exception as e:
        return f"Test command error: {e}"


def _build_rebase_conflict_prompt(base_branch: str, result: RebaseResult) -> str:
    """Build a resume prompt telling the agent to resolve rebase conflicts."""
    return (
        f"REBASE CONFLICT: Before submitting for review, I rebased your branch "
        f"onto the latest {base_branch}, but there are merge conflicts.\n\n"
        f"{result.conflict_details}\n\n"
        f"Please:\n"
        f"1. Run `git rebase {base_branch}` (or `git rebase origin/{base_branch}`)\n"
        f"2. Resolve all conflicts\n"
        f"3. Run `git rebase --continue`\n"
        f"4. Re-run the test suite to confirm no regressions\n"
        f"5. Then output @@DONE@@ again"
    )


def _build_merge_conflict_prompt(base_branch: str, result: MergeResult) -> str:
    """Build a resume prompt telling the agent to resolve merge conflicts."""
    return (
        f"MERGE CONFLICT: Before submitting for review, I tried to merge the latest "
        f"{base_branch} into your branch, but there are conflicts.\n\n"
        f"{result.conflict_details}\n\n"
        f"Please:\n"
        f"1. Run `git merge origin/{base_branch}` (or `git merge {base_branch}`)\n"
        f"2. Resolve all conflicts in the listed files\n"
        f"3. Stage the resolved files with `git add`\n"
        f"4. Complete the merge with `git commit`\n"
        f"5. Re-run the test suite to confirm no regressions\n"
        f"6. Then output @@DONE@@ again"
    )


def _build_test_failure_prompt(
    base_branch: str, test_output: str, was_merged: bool = False
) -> str:
    """Build a resume prompt telling the agent to fix post-rebase/merge test failures."""
    if was_merged:
        header = (
            f"POST-MERGE TEST FAILURE: The latest {base_branch} was successfully "
            f"merged into your branch, but the test suite now fails."
        )
    else:
        header = (
            f"POST-REBASE TEST FAILURE: Your branch was successfully rebased onto "
            f"the latest {base_branch}, but the test suite now fails."
        )
    return (
        f"{header}\n\n"
        f"Test output:\n{test_output}\n\n"
        f"Please:\n"
        f"1. Investigate and fix the test failures\n"
        f"2. Commit your fixes\n"
        f"3. Then output @@DONE@@ again"
    )
