"""Docker orchestrator — mounts, env vars, and command construction.

Builds the `docker run` command for launching the agent container.
"""

import os
import subprocess
from pathlib import Path


_PASSTHROUGH_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
    "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
)


def _auth_mounts() -> list[str]:
    """Build -v flags for Claude auth credentials."""
    home = Path.home()
    mounts: list[str] = []
    if (home / ".claude").is_dir():
        mounts += ["-v", f"{home / '.claude'}:/claude-auth:ro"]
    if (home / ".claude.json").exists():
        mounts += ["-v", f"{home / '.claude.json'}:/home/agent/.claude.json:ro"]
    return mounts


def build_docker_cmd(repo: Path, workspace_mount: str, session_dir: Path,
                     container_name: str, worktree_name: str,
                     issue_id: str, short_id: str, max_turns: int,
                     step: str, is_resume: bool, workflow_path: str,
                     image: str) -> list[str]:
    """Build the docker run command with all mounts and env vars."""
    notify_env = []
    for var in _PASSTHROUGH_ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    workflow_mount_path = str(Path(workflow_path).resolve())

    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        "-v", f"{repo / '.git'}:/repo-git:rw",
        "-v", f"{workflow_mount_path}:/workspace/WORKFLOW.md:ro",
        *_auth_mounts(),
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


def run_container(repo: Path, workspace_mount: str, session_dir: Path,
                  names: dict, issue_id: str, max_turns: int,
                  step: str, is_resume: bool, workflow_path: str,
                  image: str) -> int:
    """Build docker command, run the container, return its exit code."""
    docker_cmd = build_docker_cmd(
        repo, workspace_mount, session_dir, names["container_name"],
        names["worktree_name"], issue_id, names["short_id"], max_turns,
        step, is_resume, workflow_path, image,
    )

    # Save the worktree .git file — the container rewrites it to /repo-git/...
    # which is invalid on the host. Restore after container exits.
    worktree_git = Path(workspace_mount) / ".git"
    original_git_content = None
    if worktree_git.is_file():
        original_git_content = worktree_git.read_text()

    print(f"Launching container {names['container_name']}...")
    result = subprocess.run(docker_cmd)

    if original_git_content is not None and worktree_git.is_file():
        worktree_git.write_text(original_git_content)

    return result.returncode
