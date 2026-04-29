#!/usr/bin/env python3
"""Host-side launcher — reads WORKFLOW.md, creates workspace, runs Docker.

Orchestrates workspace setup, issue data dumping, and container launch.
Heavy lifting is delegated to workspace_setup, issue_dump, and docker_cmd.
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

# host/launch.py runs on the host, so it adds the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow, resolve_overflow_config
from core.post_run import format_cost_line
from core.protocols import UsageData
from host.tracker_client import get_tracker_with_fallback
from host.config_discovery import discover_workflow
from host.constants import SHORT_ID_LEN, REVIEW_SESSION_PREFIX, OVERFLOW_FLAG_FILENAME, USAGE_LOG_FILENAME
from host.git_overlay import extract_commits, is_fuse_overlayfs_available, setup_git_copy, setup_overlay, teardown_overlay
from host.docker_cmd import run_container
from host.env import load_all_dotenv
from host.issue_dump import dump_issue_data
from host.session_utils import get_repo_root, find_existing_session_by_prefix
from host.workspace_setup import setup_workspace
from host.git_utils import validate_git_objects, auto_commit_dirty_worktree

logger = logging.getLogger(__name__)


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


def _read_overflow_profile_name(flag: Path) -> str | None:
    """Read the selected overflow profile name from the flag file."""
    if not flag.exists():
        return None
    profile_name = flag.read_text().strip()
    return profile_name or None


def _setup_git_overlay(repo: Path, session_dir: Path) -> Path:
    """Create the session-local git mount path."""
    repo_git = repo / ".git"
    if is_fuse_overlayfs_available():
        return setup_overlay(repo_git, session_dir)
    return setup_git_copy(repo_git, session_dir)


def _teardown_git_overlay(git_mount_path: Path, session_dir: Path) -> None:
    """Clean up the session-local git mount path and its temp directories."""
    if git_mount_path.name == "git-merged":
        teardown_overlay(git_mount_path)
        for temp_dir in ("git-merged", "git-upper", "git-work"):
            path = session_dir / temp_dir
            if path.exists():
                shutil.rmtree(path)
    elif git_mount_path.exists():
        shutil.rmtree(git_mount_path)


def _copy_git_changes(session_dir: Path, repo: Path) -> int:
    """Validate and copy back git objects and whitelisted refs."""
    source_git = None
    for candidate in (session_dir / "git-merged", session_dir / "git-copy"):
        if candidate.exists():
            source_git = candidate
            break
    if source_git is None:
        return 0

    is_valid, real_errors = validate_git_objects(source_git)
    if not is_valid:
        logger.error(
            "git fsck found issues for %s:\n%s",
            source_git,
            "\n".join(real_errors),
        )
        return 1

    repo_git = repo / ".git"
    skipped_refs = extract_commits(source_git, repo_git)

    if skipped_refs:
        logger.warning(
            "Skipped non-whitelisted refs during copy-back: %s",
            ", ".join(sorted(set(skipped_refs))),
        )

    return 0


def _auto_commit_uncommitted_changes(repo: Path, session_id: str) -> bool:
    """Commit dirty worktree changes left behind after @@DONE@@."""
    committed = auto_commit_dirty_worktree(
        repo,
        message="WIP: auto-commit uncommitted changes on @@DONE@@",
    )
    if committed:
        logger.warning("[%s] Container exited with uncommitted changes - auto-committed", session_id)
    return committed


def _append_usage_log(repo, state, issue_id, title="", agent_kind="claude-code",
                      step="coder"):
    """Append a usage entry to .nightshift/usage.jsonl (survives session cleanup).

    Deduplicates by session_id — skips if already logged.
    """
    usage = state.get("usage", {})
    if not usage.get("input_tokens") and not usage.get("output_tokens"):
        return
    usage_file = repo / ".nightshift" / USAGE_LOG_FILENAME
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    session_id = state.get("branch", "").split("/")[-1] if state.get("branch") else ""

    # Deduplicate: skip if session_id already logged
    if session_id and usage_file.exists():
        try:
            for line in usage_file.read_text().splitlines():
                if line.strip():
                    existing = json.loads(line)
                    if existing.get("session_id") == session_id:
                        return
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read usage log for dedup: {e}",
                  file=sys.stderr)

    entry = {
        "session_id": session_id,
        "issue_id": issue_id,
        "title": title,
        "agent_kind": agent_kind,
        "model": usage.get("model", ""),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0),
        "started_at": state.get("started_at", ""),
        "completed_at": state.get("completed_at", ""),
        "resumes": state.get("step", 0),
        "step": step,
    }
    try:
        with open(usage_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Warning: failed to append usage log: {e}", file=sys.stderr)


def _post_container(session_dir, config, repo, issue_id, step="coder"):
    """After container exits, log usage and post proof-of-work summary.

    Usage is logged for ALL sessions with usage data (any status/step).
    Proof-of-work comment is only posted for coder sessions in waiting:review.
    """
    copy_status = _copy_git_changes(session_dir, repo)

    state_file = session_dir / "state.json"
    if not state_file.exists():
        return copy_status

    state = json.loads(state_file.read_text())

    # Read issue title from dumped issue.json for usage log
    issue_title = ""
    issue_file = session_dir / "issue.json"
    if issue_file.exists():
        try:
            issue_title = json.loads(issue_file.read_text()).get("title", "")
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read issue title: {e}", file=sys.stderr)

    # Always log usage (survives cleanup), for any status/step
    _append_usage_log(repo, state, issue_id, title=issue_title,
                      agent_kind=config.agent.kind, step=step)

    # Proof-of-work comment only for coder sessions that reached waiting:review
    if state.get("status") != "waiting:review" or step == "review":
        return copy_status

    _auto_commit_uncommitted_changes(repo, state.get("branch", issue_id))

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

    usage = state.get("usage", {})
    usage_data = UsageData(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cost_usd=usage.get("cost_usd", 0.0),
        model=usage.get("model", ""),
    )
    resumes = state.get("step", 0)
    cost_line = format_cost_line(usage_data, resumes=resumes)
    cost_section = f"\n{cost_line}\n" if cost_line else "\n"

    ticks = "```"
    proof = (
        f"🏁 **Work complete — awaiting review**\n\n"
        f"**Summary:**\n{summary}\n\n"
        f"**Q&A exchanges:** {len(human_answers)}\n"
        f"**Changes:**\n{ticks}\n{diff}\n{ticks}"
        f"{cost_section}\n"
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
    return copy_status


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
    config = load_workflow(workflow_path, repo_root=repo)
    max_turns = args.max_turns or config.agent.max_turns
    names = _resolve_names(args.issue_id, args.step, config)
    session_dir = repo / ".nightshift" / "sessions" / names["session_name"]

    # Check for duplicate sessions (prefix match) — skip for resume
    # For reviews, only block if a review session exists (coder is expected to exist).
    # For coders, only block if a coder session exists.
    if not args.resume:
        sessions_root = repo / ".nightshift" / "sessions"
        existing = find_existing_session_by_prefix(sessions_root, args.issue_id,
                                                   step=args.step)
        if existing:
            print(f"Error: session already exists for issue {existing}", file=sys.stderr)
            sys.exit(1)

    workspace_mount = setup_workspace(config, repo, names, args.resume, args.issue_id)

    dump_issue_data(config, repo, session_dir, args.issue_id,
                    names["is_review"], args.resume)

    # Check overflow flag file. Review sessions can also use overflow when the
    # active REVIEW.md defines an overflow section.
    overflow = None
    overflow_flag = repo / ".nightshift" / OVERFLOW_FLAG_FILENAME
    has_overflow_config = (
        config.overflow.extra_args
        or config.overflow.env
        or config.overflow.pricing
        or config.overflow.agent_kind
    )
    selected_overflow_profile = _read_overflow_profile_name(overflow_flag)
    if overflow_flag.exists() and (has_overflow_config or selected_overflow_profile):
        overflow = resolve_overflow_config(config, selected_overflow_profile, repo_root=repo)
        print(f"Overflow active: using alternate provider")

    # Use overflow.agent_kind when overflow is active, otherwise config.agent.kind
    actual_agent_kind = (
        overflow.agent_kind if overflow and overflow.agent_kind else config.agent.kind
    )
    git_mount_path = _setup_git_overlay(repo, session_dir)
    if not (git_mount_path / "HEAD").exists():
        raise RuntimeError(
            f"Git overlay appears empty or invalid: {git_mount_path} "
            "(missing HEAD file). This may indicate a race condition or failed copy."
        )
    try:
        returncode = run_container(
            repo, workspace_mount, session_dir, names, args.issue_id,
            max_turns, args.step, args.resume, str(workflow_path), args.image,
            git_mount_path=git_mount_path,
            overflow=overflow, agent_kind=actual_agent_kind,
        )
        post_status = _post_container(session_dir, config, repo, args.issue_id, step=args.step)
        if isinstance(post_status, int) and post_status != 0:
            returncode = post_status
    finally:
        _teardown_git_overlay(git_mount_path, session_dir)

    sys.exit(returncode)


if __name__ == "__main__":
    main()
