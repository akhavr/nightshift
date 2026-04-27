"""Workspace setup — worktree creation, branch management, review prep.

Extracted from launch.py to keep each module focused on one concern.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.constants import MERGE_NEEDED_FILENAME
from core.workspace_transaction import WorkspaceTransaction
from host.git_utils import fetch_and_resolve_ref
from host.session_utils import force_remove_dir, safe_prune


def _cleanup_partial_worktree(wt_path: Path) -> None:
    """Remove a partially created worktree directory if it exists."""
    if wt_path.exists():
        force_remove_dir(wt_path)


def create_worktree(repo: Path, wt_path: Path, branch: str,
                    base_branch: str, session_dir: Path, issue_id: str):
    """Create a fresh git worktree and initialize session state.

    WT-6: Uses safe_prune instead of global prune to prevent collateral damage
    to other worktrees with corrupted .git files.
    """
    session_dir.mkdir(parents=True, exist_ok=True)

    if wt_path.exists():
        force_remove_dir(wt_path)
    # WT-6: Use safe_prune to fix corrupted gitdirs first and check active sessions
    safe_prune(repo)

    # Verify base commit exists before creating branch
    verify_result = subprocess.run(
        ["git", "cat-file", "-t", base_branch],
        capture_output=True, text=True, cwd=str(repo),
    )
    if verify_result.returncode != 0:
        raise ValueError(f"Base branch {base_branch} points to missing commit")

    subprocess.run(["git", "branch", branch, base_branch],
                   capture_output=True, cwd=str(repo))

    result = subprocess.run(
        ["git", "worktree", "add", str(wt_path), branch],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode != 0:
        print(f"Failed to create worktree:\n{result.stderr}", file=sys.stderr)
        _cleanup_partial_worktree(wt_path)
        sys.exit(1)

    try:
        with WorkspaceTransaction(wt_path):
            gitignore_src = repo / ".gitignore"
            gitignore_dst = wt_path / ".gitignore"
            if gitignore_src.exists() and not gitignore_dst.exists():
                shutil.copy2(str(gitignore_src), str(gitignore_dst))

            files = [f for f in wt_path.iterdir() if f.name != ".git"]
            if not files:
                print(f"Worktree at {wt_path} is empty — check base_branch",
                      file=sys.stderr)
                raise RuntimeError("empty worktree")

            (session_dir / "state.json").write_text(json.dumps({
                "issue_id": issue_id, "branch": branch,
                "status": "starting", "step": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "checkpoints": [], "human_answers": [],
            }, indent=2))
    except Exception as exc:
        print(f"Failed to initialize worktree at {wt_path}: {exc}",
              file=sys.stderr)
        state_file = session_dir / "state.json"
        if state_file.exists():
            state_file.unlink()
        _cleanup_partial_worktree(wt_path)
        sys.exit(1)

    print(f"Created worktree at {wt_path}")


def merge_base_into_worktree(repo: Path, wt_path: Path,
                             base_branch: str,
                             session_dir: Path | None = None) -> str:
    """Merge latest base branch into the agent worktree branch.

    Keeps the agent branch up to date with upstream changes.

    Returns:
        "clean"    — merge succeeded (or base had not advanced)
        "conflict" — merge had conflicts; aborted and merge-needed.txt written
        "noop"     — base has not diverged, nothing to merge
    """
    merge_target = fetch_and_resolve_ref(wt_path, base_branch)

    # Check if there is anything to merge (is base ahead of us?)
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", merge_target],
        cwd=str(wt_path), capture_output=True, text=True,
    )
    target_rev = subprocess.run(
        ["git", "rev-parse", merge_target],
        cwd=str(wt_path), capture_output=True, text=True,
    )
    if (merge_base.returncode == 0 and target_rev.returncode == 0
            and merge_base.stdout.strip() == target_rev.stdout.strip()):
        print(f"Base branch {merge_target} has not advanced — nothing to merge")
        return "noop"

    result = subprocess.run(
        ["git", "merge", merge_target, "--no-edit",
         "-m", f"Merge {merge_target} into agent branch"],
        cwd=str(wt_path), capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Merged {merge_target} into agent branch")
        return "clean"

    # Collect conflict details before aborting
    conflict_output = result.stdout + "\n" + result.stderr

    # Abort the failed merge — agent will get a clean state
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(wt_path), capture_output=True,
    )

    # Write merge-needed.txt so the container can instruct the agent
    if session_dir is not None:
        merge_needed = session_dir / MERGE_NEEDED_FILENAME
        merge_needed.write_text(
            f"merge_target: {merge_target}\n"
            f"base_branch: {base_branch}\n"
            f"---\n"
            f"{conflict_output.strip()}\n"
        )
        print(f"Wrote {MERGE_NEEDED_FILENAME} with conflict details")

    print(f"Warning: merge from {merge_target} had conflicts (aborted). "
          f"Agent will be instructed to merge.", file=sys.stderr)
    return "conflict"


def setup_workspace(config, repo: Path, names: dict, is_resume: bool,
                    issue_id: str) -> str:
    """Create worktree or validate resume, returning the workspace mount path."""
    session_dir = repo / ".nightshift" / "sessions" / names["session_name"]
    wt_path = repo / config.workspace.root / names["worktree_name"]

    if not is_resume:
        create_worktree(repo, wt_path, names["branch"],
                        names["base_branch"], session_dir, issue_id)
        if names["is_review"]:
            prepare_review_session(repo, session_dir, names["short_id"], config)
    else:
        if not (session_dir / "state.json").exists():
            print(f"No session state at {session_dir}", file=sys.stderr)
            sys.exit(1)
        # Merge latest base branch into agent branch before resuming
        if names.get("is_review"):
            # Sync review worktree to current agent branch (may have been rebased)
            coder_session = repo / ".nightshift" / "sessions" / names["short_id"]
            sync_review_worktree(
                repo,
                wt_path,
                session_dir,
                coder_session,
                names["short_id"],
                config.workspace.base_branch,
            )
        else:
            merge_base_into_worktree(repo, wt_path, names["base_branch"],
                                     session_dir=session_dir)
        print(f"Resuming session for {names['session_name']}")

    return str(wt_path)


def prepare_review_session(repo, review_session_dir, short_id, config):
    """Prepare review session: copy issue data and generate diff."""
    coder_session = repo / ".nightshift" / "sessions" / short_id

    for fname in ("issue.json", "issues.json"):
        src = coder_session / fname
        if src.exists():
            shutil.copy2(str(src), str(review_session_dir / fname))

    _regenerate_diff(repo, review_session_dir, short_id, config.workspace.base_branch)


def _regenerate_diff(repo: Path, review_session_dir: Path, short_id: str,
                     base_branch: str):
    """Generate diff.patch comparing base branch to agent branch."""
    agent_branch = f"agent/{short_id}"
    diff_result = subprocess.run(
        ["git", "diff", f"{base_branch}..{agent_branch}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    diff = diff_result.stdout if diff_result.returncode == 0 else "N/A"
    (review_session_dir / "diff.patch").write_text(diff)
    print(f"Generated diff ({len(diff)} bytes) for review")


def sync_review_worktree(repo: Path, review_wt: Path, review_session_dir: Path,
                         coder_session_dir: Path, short_id: str,
                         base_branch: str = "master"):
    """Sync review worktree to current agent branch state on resume.

    After coder rebases, the review worktree and diff.patch become stale.
    This function:
    1. Resets review worktree to current agent/{short_id} HEAD
    2. Regenerates diff.patch
    3. Re-copies issue.json from coder session
    """
    agent_branch = f"agent/{short_id}"

    # 1. Reset review worktree to agent branch HEAD
    result = subprocess.run(
        ["git", "fetch", ".", f"{agent_branch}:{agent_branch}"],
        cwd=str(review_wt), capture_output=True, text=True,
    )
    # Reset to the agent branch (this handles diverged history from rebase)
    result = subprocess.run(
        ["git", "reset", "--hard", agent_branch],
        cwd=str(review_wt), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: git reset failed: {result.stderr}", file=sys.stderr)
    else:
        print(f"Synced review worktree to {agent_branch}")

    # 2. Regenerate diff.patch
    _regenerate_diff(repo, review_session_dir, short_id, base_branch)

    # 3. Re-copy issue data from coder session
    for fname in ("issue.json", "issues.json"):
        src = coder_session_dir / fname
        if src.exists():
            shutil.copy2(str(src), str(review_session_dir / fname))
            print(f"Refreshed {fname} from coder session")
