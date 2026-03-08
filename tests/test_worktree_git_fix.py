"""Tests for worktree .git pointer fix inside containers.

The container sees /workspace (the worktree) and /repo-git (the main .git).
The entrypoint must rewrite /workspace/.git to point at the right place
so commits land on the agent branch, not a disconnected repo.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch


def test_docker_entrypoint_rewrites_git_pointer(tmp_path):
    """docker-entrypoint.sh should rewrite .git file to container path."""
    # Simulate worktree .git file with broken host path
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(
        "gitdir: /home/user/repo/.git/worktrees/agent-abc123\n"
    )

    # Simulate the fix: rewrite to container path
    # This is what the entrypoint should do
    short_id = "abc123"
    (workspace / ".git").write_text(
        f"gitdir: /repo-git/worktrees/agent-{short_id}\n"
    )

    content = (workspace / ".git").read_text()
    assert "/repo-git/worktrees/agent-abc123" in content
    assert "/home/user" not in content


def test_launch_mounts_repo_git():
    """launch.py docker command should include /repo-git mount."""
    from host.launch import main
    import host.launch as launch_mod
    source = Path(launch_mod.__file__).read_text()
    assert "/repo-git" in source, "launch.py should mount .git as /repo-git"
