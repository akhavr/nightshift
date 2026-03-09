#!/usr/bin/env python3
"""Host-side launcher — reads WORKFLOW.md, creates workspace, runs Docker."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# host/launch.py runs on the host, so it adds the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow, create_tracker
from host.env import load_dotenv


def get_repo_root() -> Path:
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="Launch agent worker")
    parser.add_argument("issue_id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    parser.add_argument("--image", default="nightshift:latest", help="Docker image")
    args = parser.parse_args()

    repo = get_repo_root()

    # Load .env BEFORE config so $VAR references in WORKFLOW.md resolve correctly
    load_dotenv(repo / ".env")

    config = load_workflow(args.workflow or repo / "WORKFLOW.md")

    issue_id = args.issue_id
    short_id = issue_id[:12]
    max_turns = args.max_turns or config.agent.max_turns

    # Session dir (always under repo)
    session_dir = repo / ".nightshift" / "sessions" / short_id
    branch = f"agent/{short_id}"

    # Create workspace based on config
    if config.workspace.kind == "worktree":
        wt_root = repo / config.workspace.root
        wt_path = wt_root / f"agent-{short_id}"

        if not args.resume:
            session_dir.mkdir(parents=True, exist_ok=True)

            # Clean up stale worktree path if it exists
            if wt_path.exists():
                import shutil
                try:
                    shutil.rmtree(wt_path)
                except PermissionError:
                    subprocess.run(["docker", "run", "--rm",
                                    "-v", f"{wt_path}:/cleanup:rw",
                                    "ubuntu:24.04", "rm", "-rf", "/cleanup"],
                                   capture_output=True)
                    try:
                        shutil.rmtree(wt_path)
                    except FileNotFoundError:
                        pass
            subprocess.run(["git", "worktree", "prune"],
                           capture_output=True, cwd=str(repo))

            # Create branch (may already exist — ignore error)
            subprocess.run(["git", "branch", branch, config.workspace.base_branch],
                           capture_output=True, cwd=str(repo))

            # Create worktree — must succeed
            result = subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                capture_output=True, text=True, cwd=str(repo),
            )
            if result.returncode != 0:
                print(f"Failed to create worktree:\n{result.stderr}", file=sys.stderr)
                sys.exit(1)

            # Copy .gitignore from repo root if not already in worktree
            gitignore_src = repo / ".gitignore"
            gitignore_dst = wt_path / ".gitignore"
            if gitignore_src.exists() and not gitignore_dst.exists():
                import shutil
                shutil.copy2(str(gitignore_src), str(gitignore_dst))

            # Verify worktree has files (not just .git)
            files = [f for f in wt_path.iterdir() if f.name != ".git"]
            if not files:
                print(f"Worktree at {wt_path} is empty — check base_branch in WORKFLOW.md", file=sys.stderr)
                sys.exit(1)

            (session_dir / "state.json").write_text(json.dumps({
                "issue_id": issue_id, "branch": branch,
                "status": "starting", "step": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "checkpoints": [], "human_answers": [],
            }, indent=2))
            print(f"Created worktree at {wt_path}")
        else:
            if not (session_dir / "state.json").exists():
                print(f"No session state at {session_dir}", file=sys.stderr)
                sys.exit(1)
            print(f"Resuming session for {short_id}")

        workspace_mount = str(wt_path)

    # Dump issue data to session dir for the static tracker inside the container
    tracker = create_tracker(config, repo_dir=str(repo))
    issue = tracker.get_issue(issue_id)
    if not issue:
        print(f"Issue {issue_id} not found", file=sys.stderr)
        sys.exit(1)

    from dataclasses import asdict
    (session_dir / "issue.json").write_text(json.dumps(asdict(issue), indent=2))

    all_issues = tracker.list_issues()
    (session_dir / "issues.json").write_text(
        json.dumps([asdict(i) for i in all_issues], indent=2)
    )
    print(f"Dumped issue + {len(all_issues)} issues to {session_dir}")

    # Auth mounts — read-only, copied to writable HOME by docker-entrypoint.sh
    home = Path.home()
    auth_mounts = []
    if (home / ".claude").is_dir():
        auth_mounts += ["-v", f"{home / '.claude'}:/claude-auth:ro"]
    if (home / ".claude.json").exists():
        auth_mounts += ["-v", f"{home / '.claude.json'}:/home/agent/.claude.json:ro"]

    # Build env vars for notifications (pass through from host)
    notify_env = []
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
                "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    docker_cmd = [
        "docker", "run", "--rm", "-it",
        "--name", f"nightshift-{short_id}",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        "-v", f"{repo / '.git'}:/repo-git:rw",
        # Mount WORKFLOW.md so container can read it
        "-v", f"{repo / 'WORKFLOW.md'}:/workspace/WORKFLOW.md:ro",
        *auth_mounts,
        "-e", f"ISSUE_ID={issue_id}",
        "-e", f"SHORT_ID={short_id}",
        "-e", f"RESUME={'--resume' if args.resume else ''}",
        "-e", f"MAX_TURNS={max_turns}",
        *notify_env,
        args.image,
    ]

    # SSH agent forwarding (if available)
    ssh_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if ssh_sock:
        docker_cmd.insert(-1, "-v")
        docker_cmd.insert(-1, f"{ssh_sock}:/ssh-agent")
        docker_cmd.insert(-1, "-e")
        docker_cmd.insert(-1, "SSH_AUTH_SOCK=/ssh-agent")

    print(f"Launching container nightshift-{short_id}...")
    result = subprocess.run(docker_cmd)

    # Post-container: if agent finished, post proof-of-work to real tracker
    _post_container(session_dir, config, repo, issue_id)

    sys.exit(result.returncode)


def _post_container(session_dir, config, repo, issue_id):
    """After container exits, post proof-of-work summary to the real tracker."""
    state_file = session_dir / "state.json"
    if not state_file.exists():
        return

    state = json.loads(state_file.read_text())
    if state.get("status") != "waiting:review":
        return

    # Read checkpoints for summary
    checkpoints = state.get("checkpoints", [])
    human_answers = state.get("human_answers", [])
    branch = state.get("branch", "")

    summary_lines = [f"- {cp['description']}" for cp in checkpoints]
    summary = "\n".join(summary_lines) if summary_lines else "No checkpoints recorded."

    # Get diff stat from the worktree
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
        except Exception:
            pass
        tracker.sync()
        print(f"Posted review summary to tracker for {issue_id[:12]}")
    except Exception as e:
        print(f"Failed to post review summary: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
