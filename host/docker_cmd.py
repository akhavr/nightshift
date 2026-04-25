"""Docker orchestrator — mounts, env vars, and command construction.

Builds the `docker run` command for launching the agent container.
"""

import json
import os
import subprocess
from pathlib import Path

from core.config.models import OverflowConfig
from core.constants import *
from host.docker_utils import docker_remove


_PASSTHROUGH_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "NOTIFY_WEBHOOK_URL", "SLACK_WEBHOOK",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "GITHUB_TOKEN",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "OVERFLOW_API_KEY", "OVERFLOW_BASE_URL", "OVERFLOW_MODEL",
    # OpenHands uses LLM_* env vars (litellm under the hood)
    "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
    # Codex uses CODEX_* env vars for independent provider configuration
    # OPENAI_API_KEY needed for Codex fallback chain (CODEX_API_KEY → OPENAI_API_KEY)
    "CODEX_API_KEY", "CODEX_BASE_URL", "CODEX_MODEL", "OPENAI_API_KEY",
    # OpenCode with OpenRouter models
    "OPENROUTER_API_KEY",
)


def _auth_mounts() -> list[str]:
    """Build -v flags for auth credentials (Claude + Codex)."""
    home = Path.home()
    mounts: list[str] = []
    if (home / ".claude").is_dir():
        mounts += ["-v", f"{home / '.claude'}:/claude-auth:ro"]
    if (home / ".claude.json").exists():
        mounts += ["-v", f"{home / '.claude.json'}:/home/agent/.claude.json:ro"]
    if (home / ".codex").is_dir():
        mounts += ["-v", f"{home / '.codex'}:/codex-auth:ro"]
    return mounts


def build_docker_cmd(repo: Path, workspace_mount: str, session_dir: Path,
                     container_name: str, worktree_name: str,
                     issue_id: str, short_id: str, max_turns: int,
                     step: str, is_resume: bool, workflow_path: str,
                     image: str,
                     overflow: OverflowConfig | None = None,
                     agent_kind: str = "claude-code") -> list[str]:
    """Build the docker run command with all mounts and env vars.

    Args:
        overflow: If provided, inject overflow env vars and extra_args
            into the container (alternate LLM provider).
        agent_kind: The agent kind from config (e.g. "codex", "claude-code").
            Passed as AGENT_KIND env var so docker-entrypoint.sh can
            configure agent-specific settings.
    """
    notify_env = []
    for var in _PASSTHROUGH_ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            notify_env += ["-e", f"{var}={val}"]

    # Overflow env vars override passthrough values (appended later wins)
    overflow_env: list[str] = []
    overflow_args_env: list[str] = []
    overflow_mounts: list[str] = []
    if overflow:
        overflow_env += ["-e", "OVERFLOW_ACTIVE=1"]
        if overflow.profile_name:
            overflow_env += ["-e", f"OVERFLOW_PROFILE={overflow.profile_name}"]
        for key, val in overflow.env.items():
            overflow_env += ["-e", f"{key}={val}"]
        if overflow.extra_args:
            overflow_args_env += ["-e", f"OVERFLOW_EXTRA_ARGS={json.dumps(overflow.extra_args)}"]
        if overflow.litellm_config:
            config_path = str(Path(overflow.litellm_config).resolve())
            overflow_mounts += ["-v", f"{config_path}:{LITELLM_CONFIG_CONTAINER_PATH}:ro"]
            proxy_url = f"http://localhost:{LITELLM_PROXY_PORT}"
            overflow_env += ["-e", f"ANTHROPIC_BASE_URL={proxy_url}"]

    workflow_mount_path = str(Path(workflow_path).resolve())

    # WT-1.7: Build git mounts with read-only config overlay to prevent core.worktree pollution.
    # The config mount must come AFTER the .git mount to properly overlay.
    git_mounts = ["-v", f"{repo / '.git'}:/repo-git:rw"]
    git_config = repo / ".git" / "config"
    if git_config.exists():
        git_mounts += ["-v", f"{git_config}:/repo-git/config:ro"]

    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--memory=8g", "--memory-swap=24g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workspace_mount}:/workspace:rw",
        "-v", f"{session_dir}:/session:rw",
        *git_mounts,
        "-v", f"{workflow_mount_path}:/workspace/WORKFLOW.md:ro",
        *_auth_mounts(),
        "-e", f"ISSUE_ID={issue_id}",
        "-e", f"SHORT_ID={short_id}",
        "-e", f"WORKTREE_NAME={worktree_name}",
        "-e", f"RESUME={'--resume' if is_resume else ''}",
        "-e", f"MAX_TURNS={max_turns}",
        "-e", f"STEP={step}",
        "-e", f"PROJECT_NAME={repo.name}",
        "-e", f"AGENT_KIND={agent_kind}",
        # Disable Rich terminal UI codes for JSON parsing
        # Set to 1 to force interactive mode (plain JSON output)
        "-e", "TTY_INTERACTIVE=1",
        "-e", "TTY_COMPATIBLE=1",
        *notify_env,
        *overflow_env,
        *overflow_args_env,
        *overflow_mounts,
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
                  image: str,
                  overflow: OverflowConfig | None = None,
                  agent_kind: str = "claude-code") -> int:
    """Build docker command, run the container, return its exit code."""
    docker_cmd = build_docker_cmd(
        repo, workspace_mount, session_dir, names["container_name"],
        names["worktree_name"], issue_id, names["short_id"], max_turns,
        step, is_resume, workflow_path, image, overflow=overflow,
        agent_kind=agent_kind,
    )

    # Remove any stale container with the same name (e.g. leftover from a
    # previous run that exited without cleanup). docker rm -f is a no-op if
    # the container doesn't exist.
    container_name = names["container_name"]
    if not docker_remove(container_name):
        raise RuntimeError(f"Failed to remove stale container {container_name}")

    # Defense-in-depth: save/restore the worktree .git file.
    # The container uses GIT_DIR/GIT_WORK_TREE env vars now, so it shouldn't
    # modify .git. But if anything corrupts it, we restore the original.
    worktree_git = Path(workspace_mount) / ".git"
    original_git_content = None
    if worktree_git.is_file():
        original_git_content = worktree_git.read_text()

    print(f"Launching container {names['container_name']}...")
    result = subprocess.run(docker_cmd)

    if original_git_content is not None and worktree_git.is_file():
        worktree_git.write_text(original_git_content)

    return result.returncode
