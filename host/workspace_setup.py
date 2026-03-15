"""Workspace setup — worktree creation, branch management, review prep.

Extracted from launch.py to keep each module focused on one concern.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from host.session_utils import force_remove_dir


def create_worktree(repo: Path, wt_path: Path, branch: str,
                    base_branch: str, session_dir: Path, issue_id: str):
    """Create a fresh git worktree and initialize session state."""
    session_dir.mkdir(parents=True, exist_ok=True)

    if wt_path.exists():
        force_remove_dir(wt_path)
    subprocess.run(["git", "worktree", "prune"],
                   capture_output=True, cwd=str(repo))

    subprocess.run(["git", "branch", branch, base_branch],
                   capture_output=True, cwd=str(repo))

    result = subprocess.run(
        ["git", "worktree", "add", str(wt_path), branch],
        capture_output=True, text=True, cwd=str(repo),
    )
    if result.returncode != 0:
        print(f"Failed to create worktree:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    gitignore_src = repo / ".gitignore"
    gitignore_dst = wt_path / ".gitignore"
    if gitignore_src.exists() and not gitignore_dst.exists():
        shutil.copy2(str(gitignore_src), str(gitignore_dst))

    files = [f for f in wt_path.iterdir() if f.name != ".git"]
    if not files:
        print(f"Worktree at {wt_path} is empty — check base_branch", file=sys.stderr)
        sys.exit(1)

    (session_dir / "state.json").write_text(json.dumps({
        "issue_id": issue_id, "branch": branch,
        "status": "starting", "step": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checkpoints": [], "human_answers": [],
    }, indent=2))
    print(f"Created worktree at {wt_path}")


def merge_base_into_worktree(repo: Path, wt_path: Path,
                             base_branch: str) -> None:
    """Merge latest base branch into the agent worktree branch.

    Keeps the agent branch up to date with upstream changes. If the merge
    has conflicts, they are left for the agent to resolve.
    """
    # Fetch latest from remote (ignore failure — remote may not exist)
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(wt_path), capture_output=True, text=True,
    )

    # Try merging the remote ref first, fall back to local
    fetch_ok = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{base_branch}"],
        cwd=str(wt_path), capture_output=True,
    ).returncode == 0
    merge_target = f"origin/{base_branch}" if fetch_ok else base_branch

    result = subprocess.run(
        ["git", "merge", merge_target, "--no-edit",
         "-m", f"Merge {merge_target} into agent branch"],
        cwd=str(wt_path), capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Merged {merge_target} into agent branch")
    else:
        # Abort the failed merge — agent will get a clean state
        # and the pre-review rebase will catch divergence later
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(wt_path), capture_output=True,
        )
        print(f"Warning: merge from {merge_target} had conflicts (aborted). "
              f"Agent will work from current state.", file=sys.stderr)


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
        if not names.get("is_review"):
            merge_base_into_worktree(repo, wt_path, names["base_branch"])
        print(f"Resuming session for {names['session_name']}")

    return str(wt_path)


def prepare_review_session(repo, review_session_dir, short_id, config):
    """Prepare review session: copy issue data and generate diff."""
    coder_session = repo / ".nightshift" / "sessions" / short_id

    for fname in ("issue.json", "issues.json"):
        src = coder_session / fname
        if src.exists():
            shutil.copy2(str(src), str(review_session_dir / fname))

    base = config.workspace.base_branch
    agent_branch = f"agent/{short_id}"
    diff_result = subprocess.run(
        ["git", "diff", f"{base}..{agent_branch}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    diff = diff_result.stdout if diff_result.returncode == 0 else "N/A"
    (review_session_dir / "diff.patch").write_text(diff)
    print(f"Generated diff ({len(diff)} bytes) for review")
