#!/usr/bin/env python3
"""Host-side launcher — reads WORKFLOW.md, creates workspace, runs Docker.

Orchestrates workspace setup, issue data dumping, and container launch.
Heavy lifting is delegated to workspace_setup, issue_dump, and docker_cmd.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# host/launch.py runs on the host, so it adds the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow
from host.tracker_client import get_tracker_with_fallback
from host.config_discovery import discover_workflow
from host.constants import SHORT_ID_LEN, REVIEW_SESSION_PREFIX, OVERFLOW_FLAG_FILENAME
from host.docker_cmd import run_container
from host.env import load_all_dotenv
from host.issue_dump import dump_issue_data
from host.session_utils import get_repo_root
from host.workspace_setup import setup_workspace


def _resolve_names(issue_id: str, step: str, config):
    """Derive session/branch/container names from issue_id and step."""
    short_id = issue_id[:SHORT_ID_LEN]
    is_review = step == "review"
    prefix = "review" if is_review else "agent"
    return {
        "short_id": short_id,
        "is_review": is_review,
        "session_name": f"{REVIEW_SESSION_PREFIX}{short_id}" if is_review else short_id,
        "branch": f"{prefix}/{short_id}",
        "container_name": f"nightshift-{prefix}-{short_id}" if is_review else f"nightshift-{short_id}",
        "worktree_name": f"{prefix}-{short_id}",
        "base_branch": f"agent/{short_id}" if is_review else config.workspace.base_branch,
    }


def _post_container(session_dir, config, repo, issue_id):
    """After container exits, post proof-of-work summary to the real tracker."""
    state_file = session_dir / "state.json"
    if not state_file.exists():
        return

    state = json.loads(state_file.read_text())
    if state.get("status") != "waiting:review":
        return

    checkpoints = state.get("checkpoints", [])
    human_answers = state.get("human_answers", [])
    branch = state.get("branch", "")

    summary_lines = [f"- {cp['description']}" for cp in checkpoints]
    summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

    base = config.workspace.base_branch
    diff_result = subprocess.run(
        ["git", "diff", "--stat", f"{base}..{branch}"],
        capture_output=True, text=True, cwd=str(repo),
    )
    diff = diff_result.stdout.strip() if diff_result.returncode == 0 else "N/A"

    ticks = "```"
    proof = (
        f"🏁 **Work complete — awaiting review**\n\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Q&A exchanges:** {len(human_answers)}\n"
        f"**Changes:**\n{ticks}\n{diff}\n{ticks}\n\n"
        f"Review with: `nightshift accept/reject/revise {issue_id}`"
    )

    try:
        tracker = get_tracker_with_fallback(config, repo)
        tracker.add_comment(issue_id, proof)
        tracker.add_label(issue_id, "needs-review")
        try:
            tracker.remove_label(issue_id, "agent-in-progress")
        except Exception as e:
            print(f"Warning: remove_label failed: {e}", file=sys.stderr)
        tracker.sync()
        print(f"Posted review summary to tracker for {issue_id[:SHORT_ID_LEN]}")
    except Exception as e:
        print(f"Failed to post review summary: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Launch agent worker")
    parser.add_argument("issue_id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    parser.add_argument("--image", default="nightshift:latest", help="Docker image")
    parser.add_argument("--step", default="coder", choices=["coder", "review"],
                        help="Pipeline step (coder or review)")
    parser.add_argument("--coder-session", default=None,
                        help="Coder session ID (for review step, links back to coder)")
    args = parser.parse_args()

    repo = get_repo_root()
    load_all_dotenv(repo / ".env")

    workflow_path = discover_workflow(repo, args.workflow)
    config = load_workflow(workflow_path)
    max_turns = args.max_turns or config.agent.max_turns
    names = _resolve_names(args.issue_id, args.step, config)
    session_dir = repo / ".nightshift" / "sessions" / names["session_name"]

    workspace_mount = setup_workspace(config, repo, names, args.resume, args.issue_id)

    dump_issue_data(config, repo, session_dir, args.issue_id,
                    names["is_review"], args.resume)

    # Check overflow flag file — skip overflow for review sessions so the
    # review agent always uses its own provider (e.g. Claude Code).
    overflow = None
    if not names["is_review"]:
        overflow_flag = repo / ".nightshift" / OVERFLOW_FLAG_FILENAME
        has_overflow_config = (
            config.overflow.extra_args
            or config.overflow.env
            or config.overflow.agent_kind
        )
        if overflow_flag.exists() and has_overflow_config:
            overflow = config.overflow
            print(f"Overflow active: using alternate provider")

    returncode = run_container(
        repo, workspace_mount, session_dir, names, args.issue_id,
        max_turns, args.step, args.resume, str(workflow_path), args.image,
        overflow=overflow,
    )

    if not names["is_review"]:
        _post_container(session_dir, config, repo, args.issue_id)

    sys.exit(returncode)


if __name__ == "__main__":
    main()
