"""Pre-review rebase: fetch latest base, rebase, and re-run tests.

Before transitioning to waiting:review, this module attempts to rebase
the agent branch onto the latest base branch. If the rebase has conflicts
or tests fail after rebase, a resume prompt is returned so the agent can
fix the issues itself.
"""

import logging
import subprocess
from pathlib import Path

from core.protocols import RebaseResult, Workspace, WorkspaceManager

log = logging.getLogger(__name__)

TEST_COMMAND_TIMEOUT_S = 120  # timeout for test command execution


def attempt_pre_review_rebase(
    workspace_mgr: WorkspaceManager,
    workspace: Workspace | None,
    base_branch: str,
    test_command: str | None = None,
    test_timeout_s: int = TEST_COMMAND_TIMEOUT_S,
) -> str | None:
    """Rebase onto latest base branch and re-run tests.

    Returns None on success, or a resume prompt describing the failure
    so the agent can fix it.
    """
    if workspace is None:
        log.warning("No workspace — skipping pre-review rebase")
        return None

    log.info(f"Pre-review rebase onto {base_branch}...")
    result = workspace_mgr.rebase(workspace.path, base_branch)

    if not result.success:
        log.warning(f"Rebase failed: {result.conflict_details}")
        return _build_rebase_conflict_prompt(base_branch, result)

    if test_command:
        test_failure = _run_test_command(workspace.path, test_command, test_timeout_s)
        if test_failure:
            log.warning(f"Post-rebase tests failed: {test_failure}")
            return _build_test_failure_prompt(base_branch, test_failure)

    log.info("Pre-review rebase succeeded")
    return None


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
