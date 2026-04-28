"""Host-side pre-review rebase.

Rebases the agent branch onto the latest base branch before launching review.
Runs on the host (not in the container) to avoid bind-mount issues where git
cannot unlink mounted files like WORKFLOW.md.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("watcher")

TEST_COMMAND_TIMEOUT_S = 120  # timeout for test command execution


def attempt_pre_review_rebase(
    worktree_path: Path,
    base_branch: str,
    test_command: str | None = None,
    test_timeout_s: int = TEST_COMMAND_TIMEOUT_S,
) -> str | None:
    """Rebase onto latest base branch and re-run tests.

    Runs on the host side, outside the container, to avoid bind-mount issues.

    Returns None on success, or a resume prompt describing the failure
    so the agent can fix it.
    """
    if not worktree_path.exists():
        log.warning(f"Worktree does not exist: {worktree_path}")
        return None

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


def _rebase(worktree_path: Path, base_branch: str) -> RebaseResult:
    """Fetch latest base branch and rebase the worktree branch onto it."""
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
    return RebaseResult(success=False, conflict_details=details)


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
