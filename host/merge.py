"""Merge execution and conflict validation for nightshift accept.

Extracted from cli.py to keep merge logic separate from CLI arg parsing.
"""

import subprocess
import sys
from pathlib import Path

from host.constants import (
    SHORT_ID_LEN, CONFLICT_FILE_PREVIEW_LEN,
)
from core.workspace_transaction import check_worktree_integrity
from core.workspace_transaction import WorkspaceTransaction, RebaseConflictError
from host.git_utils import fetch_and_resolve_ref
from host.rebase import sanitize_git_config
from host.session_utils import update_status

BEHIND_BASE_COMMIT_PREVIEW = 10  # max commits to show in behind-base warning


def check_branch_not_behind_base(repo: Path, branch: str, base: str) -> str | None:
    """Check if the agent branch is behind the base branch.

    Returns None if the branch is up to date, or a message describing
    the divergence if the branch is behind.
    """
    # Sanitize core.worktree if container set it to /workspace
    sanitize_git_config(repo)

    base_ref = fetch_and_resolve_ref(repo, base)

    # Find commits in base that are not in the agent branch
    result = subprocess.run(
        ["git", "log", "--oneline", f"{branch}..{base_ref}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None  # can't determine, allow merge to proceed

    behind_commits = result.stdout.strip()
    if not behind_commits:
        return None  # up to date

    lines = behind_commits.splitlines()
    preview = "\n".join(lines[:BEHIND_BASE_COMMIT_PREVIEW])
    suffix = ""
    if len(lines) > BEHIND_BASE_COMMIT_PREVIEW:
        suffix = f"\n... and {len(lines) - BEHIND_BASE_COMMIT_PREVIEW} more"

    return (
        f"Agent branch `{branch}` is behind `{base}` by "
        f"{len(lines)} commit(s):\n{preview}{suffix}\n\n"
        f"Run `nightshift resume <issue-id>` to merge latest base "
        f"branch into the agent branch before accepting."
    )


def resolve_merge_ref(repo: Path, branch: str, worktree: Path) -> str:
    """Find the merge source: branch ref or worktree HEAD. Exits on failure."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0:
        return branch
    if worktree.exists():
        wt_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(worktree),
        )
        if wt_head.returncode != 0:
            print(f"Branch {branch} not found and worktree HEAD unreadable.", file=sys.stderr)
            sys.exit(1)
        ref = wt_head.stdout.strip()
        print(f"Branch {branch} gone, using worktree HEAD {ref[:SHORT_ID_LEN]}")
        return ref
    print(f"Branch {branch} not found and no worktree at {worktree}.", file=sys.stderr)
    sys.exit(1)


def merge_with_rebase_fallback(repo: Path, merge_ref: str, branch: str,
                                base: str, issue_id: str, config,
                                report_failure, worktree: Path | None = None):
    """Attempt merge; on conflict, rebase in worktree and retry. Exits on failure.

    Args:
        worktree: If provided, rebase is done in the worktree to avoid polluting main repo.
    """
    result = subprocess.run(
        ["git", "merge", "--no-ff", merge_ref,
         "-m", f"Merge {branch}: agent work on {issue_id}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode == 0:
        return

    merge_err = result.stderr.strip()
    if "local changes" in merge_err or "overwritten by merge" in merge_err:
        print(f"Merge failed — uncommitted changes on {base}:\n{merge_err}", file=sys.stderr)
        report_failure(config, repo, issue_id,
                       f"Cannot merge: uncommitted changes on `{base}`. "
                       f"Commit or stash them first.")
        sys.exit(1)

    # Conflict — abort, rebase in worktree, retry
    subprocess.run(["git", "merge", "--abort"], capture_output=True, cwd=str(repo))
    print(f"Merge conflict — rebasing {branch} onto {base}...")
    _rebase_and_retry_merge(repo, branch, base, issue_id, config, report_failure, worktree)


def _rebase_and_retry_merge(repo: Path, branch: str, base: str,
                             issue_id: str, config, report_failure,
                             worktree: Path | None = None):
    """Rebase branch onto base in worktree, then retry merge in main repo. Exits on failure.

    If worktree is provided and exists, the rebase is done there to avoid polluting
    the main repo working tree with conflict markers.
    """
    rebase_dir = worktree if worktree and worktree.exists() else None
    if not rebase_dir:
        report_failure(
            config, repo, issue_id,
            f"Cannot rebase `{branch}` against `{base}` without a workspace "
            f"transaction worktree; manual conflict resolution is required.")
        sys.exit(1)

    check_worktree_integrity(rebase_dir, auto_repair=True)
    try:
        with WorkspaceTransaction(rebase_dir) as txn:
            txn.rebase(base)
    except RebaseConflictError as exc:
        details = exc.stderr.strip() or str(exc)
        print(f"Rebase failed:\n{details}", file=sys.stderr)
        report_failure(
            config, rebase_dir, issue_id,
            f"Merge conflicts with `{base}` that need manual resolution:\n"
            f"```\n{details}\n```\n"
            f"@nightshift revise")
        sys.exit(1)

    print("Rebase successful, retrying merge...")
    _retry_merge_after_rebase(repo, branch, issue_id, config, report_failure)


def _retry_merge_after_rebase(repo: Path, branch: str, issue_id: str,
                               config, report_failure):
    """Retry the merge after a successful rebase."""
    result = subprocess.run(
        ["git", "merge", "--no-ff", branch,
         "-m", f"Merge {branch}: agent work on {issue_id}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode != 0:
        print(f"Merge still failed after rebase:\n{result.stderr}", file=sys.stderr)
        report_failure(config, repo, issue_id,
                       f"Merge failed even after rebase:\n"
                       f"```\n{result.stderr.strip()}\n```")
        sys.exit(1)


def check_conflict_markers(repo: Path) -> list[str]:
    """Check files changed by the merge commit for conflict markers.

    Returns list of files containing markers, or empty list if clean.
    """
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^1..HEAD"],
        capture_output=True, text=True, cwd=str(repo),
    )
    if diff_result.returncode != 0:
        print(f"Warning: git diff --name-only failed (rc={diff_result.returncode}), "
              f"skipping conflict marker check", file=sys.stderr)
        return []
    changed_files = [f for f in diff_result.stdout.strip().splitlines() if f]
    if not changed_files:
        return []

    conflict_files = []
    for fname in changed_files:
        fpath = repo / fname
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(errors="replace")
        except Exception as e:
            print(f"Warning: cannot read {fname}: {e}", file=sys.stderr)
            continue
        if "\n<<<<<<<" in content or content.startswith("<<<<<<<"):
            conflict_files.append(fname)
    return conflict_files


def verify_no_conflict_markers(repo: Path, config, issue_id: str,
                                sid: str, sessions_dir, report_failure):
    """Check for conflict markers post-merge. Resets and exits if found."""
    conflict_files = check_conflict_markers(repo)
    if not conflict_files:
        return
    file_list = "\n".join(conflict_files[:CONFLICT_FILE_PREVIEW_LEN])
    print(f"Conflict markers found after merge — aborting:\n{file_list}",
          file=sys.stderr)
    subprocess.run(["git", "reset", "--hard", "HEAD~1"],
                   capture_output=True, cwd=str(repo))
    msg = (f"Merge aborted: conflict markers (`<<<<<<<`) found in "
           f"{len(conflict_files)} file(s) after rebase+merge:\n"
           f"```\n{file_list}\n```\n"
           f"Manual conflict resolution required.")
    report_failure(config, repo, issue_id, msg)
    sd = sessions_dir / sid
    if (sd / "state.json").exists():
        try:
            update_status(sd, "error:merge-conflict")
        except Exception as e:
            print(f"Failed to update session state: {e}", file=sys.stderr)
    sys.exit(1)
