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
from core.config import load_workflow


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
    parser.add_argument("--image", default="agent-worker:latest", help="Docker image")
    args = parser.parse_args()

    repo = get_repo_root()
    config = load_workflow(args.workflow or repo / "WORKFLOW.md")

    issue_id = args.issue_id
    short_id = issue_id[:12]
    max_turns = args.max_turns or config.agent.max_turns

    # Session dir (always under repo)
    session_dir = repo / ".agent-worker" / "sessions" / short_id
    branch = f"agent/{short_id}"

    # Create workspace based on config
    if config.workspace.kind == "worktree":
        wt_root = repo / config.workspace.root
        wt_path = wt_root / f"agent-{short_id}"

        if not args.resume:
            session_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "branch", branch, config.workspace.base_branch],
                           capture_output=True, cwd=str(repo))
            subprocess.run(["git", "worktree", "add", str(wt_path), branch],
                           capture_output=True, cwd=str(repo))

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
    else:
        # directory mode — just use a subdirectory
        workspace_mount = str(repo / config.workspace.root / f"agent-{short_id}")
        if not args.resume:
            session_dir.mkdir(parents=True, exist_ok=True)
            Path(workspace_mount).mkdir(parents=True, exist_ok=True)
            (session_dir / "state.json").write_text(json.dumps({
                "issue_id": issue_id, "branch": "",
                "status": "starting", "step": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "checkpoints": [], "human_answers": [],
            }, indent=2))

    # Auth mounts (OQ-5: verified paths)
    home = Path.home()
    auth_mounts = []
    if (home / ".claude").is_dir():
        auth_mounts += ["-v", f"{home / '.claude'}:/root/.claude:ro"]
    if (home / ".claude.json").exists():
        auth_mounts += ["-v", f"{home / '.claude.json'}:/root/.claude.json:ro"]

    # Build env vars for notifications (pass through from host)
    notify_env = []
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
                "ANTHROPIC_API_KEY", "LINEAR_API_KEY", "GITHUB_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    docker_cmd = [
        "docker", "run", "--rm", "-it",
        "--name", f"agent-worker-{short_id}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        "-v", f"{repo / '.git'}:/repo-git:ro",
        # Mount WORKFLOW.md so container can read it
        "-v", f"{repo / 'WORKFLOW.md'}:/workspace/WORKFLOW.md:ro",
        *auth_mounts,
        "-v", f"{home / '.gitconfig'}:/root/.gitconfig:ro",
        "-e", f"ISSUE_ID={issue_id}",
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

    print(f"Launching container agent-worker-{short_id}...")
    os.execvp("docker", docker_cmd)


if __name__ == "__main__":
    main()
