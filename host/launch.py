#!/usr/bin/env python3
"""Host-side launcher — reads WORKFLOW.md, creates workspace, runs Docker."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# host/launch.py runs on the host, so it adds the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow, create_tracker
from host.env import load_all_dotenv
from host.session_utils import get_repo_root, force_remove_dir


def _create_worktree(repo: Path, wt_path: Path, branch: str,
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


def _dump_issue_data(config, repo: Path, session_dir: Path,
                     issue_id: str, is_review: bool, is_resume: bool):
    """Dump issue data to session dir for the static tracker inside the container."""
    issue_json = session_dir / "issue.json"
    issues_json = session_dir / "issues.json"

    if is_review and issue_json.exists():
        return  # Already copied from coder session

    tracker = create_tracker(config, repo_dir=str(repo))
    issue = tracker.get_issue(issue_id)

    if not issue and is_resume and issue_json.exists():
        print(f"Tracker unavailable, reusing cached issue data for resume")
    elif not issue:
        print(f"Issue {issue_id} not found", file=sys.stderr)
        sys.exit(1)
    else:
        issue_json.write_text(json.dumps(asdict(issue), indent=2))
        all_issues = tracker.list_issues()
        issues_json.write_text(
            json.dumps([asdict(i) for i in all_issues], indent=2)
        )
        print(f"Dumped issue + {len(all_issues)} issues to {session_dir}")


def _build_docker_cmd(repo: Path, workspace_mount: str, session_dir: Path,
                      container_name: str, worktree_name: str,
                      issue_id: str, short_id: str, max_turns: int,
                      step: str, is_resume: bool, workflow_path: str,
                      image: str) -> list[str]:
    """Build the docker run command with all mounts and env vars."""
    home = Path.home()
    auth_mounts = []
    if (home / ".claude").is_dir():
        auth_mounts += ["-v", f"{home / '.claude'}:/claude-auth:ro"]
    if (home / ".claude.json").exists():
        auth_mounts += ["-v", f"{home / '.claude.json'}:/home/agent/.claude.json:ro"]

    notify_env = []
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
                "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    tty_flags = ["-it"] if sys.stdin.isatty() else []
    workflow_mount_path = str(Path(workflow_path).resolve())

    cmd = [
        "docker", "run", "--rm", *tty_flags,
        "--name", container_name,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        "-v", f"{repo / '.git'}:/repo-git:rw",
        "-v", f"{workflow_mount_path}:/workspace/WORKFLOW.md:ro",
        *auth_mounts,
        "-e", f"ISSUE_ID={issue_id}",
        "-e", f"SHORT_ID={short_id}",
        "-e", f"WORKTREE_NAME={worktree_name}",
        "-e", f"RESUME={'--resume' if is_resume else ''}",
        "-e", f"MAX_TURNS={max_turns}",
        "-e", f"STEP={step}",
        "-e", f"PROJECT_NAME={repo.name}",
        *notify_env,
        image,
    ]

    ssh_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if ssh_sock:
        cmd.insert(-1, "-v")
        cmd.insert(-1, f"{ssh_sock}:/ssh-agent")
        cmd.insert(-1, "-e")
        cmd.insert(-1, "SSH_AUTH_SOCK=/ssh-agent")

    return cmd


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

    workflow_path = args.workflow or repo / "WORKFLOW.md"
    config = load_workflow(workflow_path)

    issue_id = args.issue_id
    short_id = issue_id[:12]
    max_turns = args.max_turns or config.agent.max_turns

    is_review = args.step == "review"
    prefix = "review" if is_review else "agent"
    session_name = f"review-{short_id}" if is_review else short_id
    branch = f"{prefix}/{short_id}"
    container_name = f"nightshift-{prefix}-{short_id}" if is_review else f"nightshift-{short_id}"
    worktree_name = f"{prefix}-{short_id}"
    base_branch = f"agent/{short_id}" if is_review else config.workspace.base_branch
    session_dir = repo / ".nightshift" / "sessions" / session_name

    if config.workspace.kind == "worktree":
        wt_path = repo / config.workspace.root / worktree_name

        if not args.resume:
            _create_worktree(repo, wt_path, branch, base_branch, session_dir, issue_id)
            if is_review:
                _prepare_review_session(repo, session_dir, short_id, config)
        else:
            if not (session_dir / "state.json").exists():
                print(f"No session state at {session_dir}", file=sys.stderr)
                sys.exit(1)
            print(f"Resuming session for {session_name}")

        workspace_mount = str(wt_path)

    _dump_issue_data(config, repo, session_dir, issue_id, is_review, args.resume)

    docker_cmd = _build_docker_cmd(
        repo, workspace_mount, session_dir, container_name,
        worktree_name, issue_id, short_id, max_turns,
        args.step, args.resume, str(workflow_path), args.image,
    )

    print(f"Launching container {container_name}...")
    result = subprocess.run(docker_cmd)

    if not is_review:
        _post_container(session_dir, config, repo, issue_id)

    sys.exit(result.returncode)


def _prepare_review_session(repo, review_session_dir, short_id, config):
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
        tracker = create_tracker(config, repo_dir=str(repo))
        tracker.add_comment(issue_id, proof)
        tracker.add_label(issue_id, "needs-review")
        try:
            tracker.remove_label(issue_id, "agent-in-progress")
        except Exception as e:
            print(f"Warning: remove_label failed: {e}", file=sys.stderr)
        tracker.sync()
        print(f"Posted review summary to tracker for {issue_id[:12]}")
    except Exception as e:
        print(f"Failed to post review summary: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
