"""Host-side pre-review rebase.

Rebases the agent branch onto the latest base branch before launching review.
Runs on the host (not in the container) to avoid bind-mount issues where git
cannot unlink mounted files like WORKFLOW.md.
"""

import logging
import os
import subprocess
from pathlib import Path

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

    # Sanitize core.worktree if container set it to /workspace
    if repo_root:
        sanitize_git_config(repo_root)

    log.info(f"Pre-review rebase onto {base_branch} in {worktree_path}...")
    result = _rebase(worktree_path, base_branch)

    if not result.success:
        log.warning(f"Rebase failed, falling back to merge: {result.conflict_details}")
        merge_result = _merge(worktree_path, base_branch)
        if not merge_result.success:
            log.warning(f"Merge also failed: {merge_result.conflict_details}")
            return _build_merge_conflict_prompt(base_branch, merge_result)

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


def _rebase(worktree_path: Path, base_branch: str) -> RebaseResult:
    """Fetch latest base branch and rebase the worktree branch onto it."""
    # Stash uncommitted changes before rebase (e.g., WORKFLOW.md local config)
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked", "-m", "pre-rebase-stash"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    had_stash = "No local changes" not in stash_result.stdout

    # Fetch latest from remote (ignore failure — remote may not exist)
    fetch_result = subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    # Use fetched remote ref if available, otherwise local base branch
    rebase_target = f"origin/{base_branch}" if fetch_result.returncode == 0 else base_branch

    result = subprocess.run(
        ["git", "rebase", rebase_target],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if result.returncode == 0:
        if had_stash:
            pop_result = subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(worktree_path), capture_output=True, text=True,
            )
            if pop_result.returncode != 0:
                log.warning(f"Stash pop failed (conflicts?): {pop_result.stderr}")
        return RebaseResult(success=True)

    # Collect conflict details before aborting
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    conflict_files = diff_result.stdout.strip()
    details = f"Rebase failed.\nstderr: {result.stderr.strip()}"
    if conflict_files:
        details += f"\nConflicting files:\n{conflict_files}"

    # Abort the failed rebase to restore a clean state
    subprocess.run(
        ["git", "rebase", "--abort"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )

    # Restore stashed changes after abort
    if had_stash:
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        if pop_result.returncode != 0:
            log.warning(f"Stash pop failed after rebase abort: {pop_result.stderr}")

    return RebaseResult(success=False, conflict_details=details)


class MergeResult:
    """Result of a merge operation."""

    def __init__(self, success: bool, conflict_details: str = ""):
        self.success = success
        self.conflict_details = conflict_details


def _merge(worktree_path: Path, base_branch: str) -> MergeResult:
    """Fetch latest base branch and merge it into the worktree branch."""
    # Stash uncommitted changes before merge (e.g., WORKFLOW.md local config)
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked", "-m", "pre-merge-stash"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    had_stash = "No local changes" not in stash_result.stdout

    # Fetch latest from remote (ignore failure — remote may not exist)
    fetch_result = subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    # Use fetched remote ref if available, otherwise local base branch
    merge_target = f"origin/{base_branch}" if fetch_result.returncode == 0 else base_branch

    result = subprocess.run(
        ["git", "merge", merge_target, "-m", f"Merge {merge_target} into agent branch"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if result.returncode == 0:
        if had_stash:
            pop_result = subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(worktree_path), capture_output=True, text=True,
            )
            if pop_result.returncode != 0:
                log.warning(f"Stash pop failed (conflicts?): {pop_result.stderr}")
        return MergeResult(success=True)

    # Collect conflict details before aborting
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    conflict_files = diff_result.stdout.strip()
    details = f"Merge failed.\nstderr: {result.stderr.strip()}"
    if conflict_files:
        details += f"\nConflicting files:\n{conflict_files}"

    # Abort the failed merge to restore a clean state
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )

    # Restore stashed changes after abort
    if had_stash:
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        if pop_result.returncode != 0:
            log.warning(f"Stash pop failed after merge abort: {pop_result.stderr}")

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


def _build_test_failure_prompt(base_branch: str, test_output: str) -> str:
    """Build a resume prompt telling the agent to fix post-rebase test failures."""
    return (
        f"POST-REBASE TEST FAILURE: Your branch was successfully rebased onto "
        f"the latest {base_branch}, but the test suite now fails.\n\n"
        f"Test output:\n{test_output}\n\n"
        f"Please:\n"
        f"1. Investigate and fix the test failures\n"
        f"2. Commit your fixes\n"
        f"3. Then output @@DONE@@ again"
    )
