"""Merge execution and conflict validation for nightshift accept.

Extracted from cli.py to keep merge logic separate from CLI arg parsing.
"""

import subprocess
import sys
from pathlib import Path

from host.constants import SHORT_ID_LEN


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


def check_working_tree_clean(repo: Path, base: str, config,
                             issue_id: str, report_failure):
    """Exit if repo has uncommitted changes."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(repo),
    )
    dirty_files = [line for line in status.stdout.strip().splitlines()
                   if line and not line.startswith("??")]
    if dirty_files:
        file_list = "\n".join(dirty_files[:10])
        msg = (f"Cannot merge: working tree on `{base}` is not clean.\n"
               f"```\n{file_list}\n```\n"
               f"Commit or stash changes first.")
        print(f"Working tree not clean:\n{file_list}", file=sys.stderr)
        report_failure(config, repo, issue_id, msg)
        sys.exit(1)


def merge_with_rebase_fallback(repo: Path, merge_ref: str, branch: str,
                                base: str, issue_id: str, config,
                                report_failure):
    """Attempt merge; on conflict, rebase and retry. Exits on failure."""
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

    # Conflict — abort, rebase, retry
    subprocess.run(["git", "merge", "--abort"], capture_output=True, cwd=str(repo))
    print(f"Merge conflict — rebasing {branch} onto {base}...")
    _rebase_and_retry_merge(repo, branch, base, issue_id, config, report_failure)


def _rebase_and_retry_merge(repo: Path, branch: str, base: str,
                             issue_id: str, config, report_failure):
    """Rebase branch onto base, then retry the merge. Exits on failure."""
    old_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=str(repo),
    ).stdout.strip()
    subprocess.run(["git", "checkout", branch], capture_output=True, cwd=str(repo))
    rebase = subprocess.run(
        ["git", "rebase", base],
        capture_output=True, text=True, cwd=str(repo),
    )
    if rebase.returncode != 0:
        _abort_rebase(repo, old_branch, branch, base, issue_id, config,
                      rebase.stderr.strip(), report_failure)

    subprocess.run(["git", "checkout", old_branch], capture_output=True, cwd=str(repo))
    print("Rebase successful, retrying merge...")
    _retry_merge_after_rebase(repo, branch, issue_id, config, report_failure)


def _abort_rebase(repo: Path, old_branch: str, branch: str, base: str,
                  issue_id: str, config, details: str, report_failure):
    """Abort a failed rebase and report the error."""
    subprocess.run(["git", "rebase", "--abort"], capture_output=True, cwd=str(repo))
    subprocess.run(["git", "checkout", old_branch], capture_output=True, cwd=str(repo))
    print(f"Rebase failed:\n{details}", file=sys.stderr)
    report_failure(
        config, repo, issue_id,
        f"Merge conflicts with `{base}` that need manual resolution:\n"
        f"```\n{details}\n```\n"
        f"@nightshift revise")
    sys.exit(1)


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
    file_list = "\n".join(conflict_files[:20])
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
        from host.session_utils import update_status
        try:
            update_status(sd, "error:merge-conflict")
        except Exception as e:
            print(f"Failed to update session state: {e}", file=sys.stderr)
    sys.exit(1)
