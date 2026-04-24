"""Tests for docker-entrypoint.sh .git pointer restoration on exit."""

import os
import subprocess
from pathlib import Path

import pytest


# Extract the .git pointer rewrite portion from docker-entrypoint.sh,
# including the EXIT trap that restores the original content.
_GIT_POINTER_SCRIPT = """\
#!/bin/sh

# Save original .git content and set up cleanup trap
if [ -f /workspace/.git ] && [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    ORIGINAL_GIT_CONTENT=$(cat /workspace/.git)
    cleanup() {
        printf '%s' "$ORIGINAL_GIT_CONTENT" > /workspace/.git
    }
    trap cleanup EXIT

    # Rewrite to container path
    echo "gitdir: /repo-git/worktrees/${WORKTREE_NAME}" > /workspace/.git
fi

# Simulate the rest of the entrypoint (exec entrypoint.py) by just exiting
exit 0
"""


class TestGitPointerRestoration:

    def test_git_pointer_restored_on_exit(self, tmp_path):
        """The .git pointer is restored to host path when the container exits."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()
        worktrees_dir = repo_git / "worktrees" / "agent-abc123"
        worktrees_dir.mkdir(parents=True)

        # Original host-style .git pointer
        original_content = "gitdir: /home/user/repo/.git/worktrees/agent-abc123"
        git_file = workspace / ".git"
        git_file.write_text(original_content)

        # Rewrite script to use tmp_path paths
        script_text = _GIT_POINTER_SCRIPT.replace("/workspace", str(workspace))
        script_text = script_text.replace("/repo-git", str(repo_git))
        script = tmp_path / "git_test.sh"
        script.write_text(script_text)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WORKTREE_NAME": "agent-abc123",
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # After the script exits, .git should be restored to original content
        assert git_file.read_text() == original_content

    def test_git_pointer_unchanged_if_no_git_file(self, tmp_path):
        """If there's no .git file, nothing happens and no error."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()

        script_text = _GIT_POINTER_SCRIPT.replace("/workspace", str(workspace))
        script_text = script_text.replace("/repo-git", str(repo_git))
        script = tmp_path / "git_test.sh"
        script.write_text(script_text)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WORKTREE_NAME": "agent-abc123",
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert not (workspace / ".git").exists()

    def test_git_pointer_unchanged_if_no_worktree_name(self, tmp_path):
        """If WORKTREE_NAME is not set, .git is not modified."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()

        original_content = "gitdir: /home/user/repo/.git/worktrees/agent-abc123"
        git_file = workspace / ".git"
        git_file.write_text(original_content)

        script_text = _GIT_POINTER_SCRIPT.replace("/workspace", str(workspace))
        script_text = script_text.replace("/repo-git", str(repo_git))
        script = tmp_path / "git_test.sh"
        script.write_text(script_text)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # No WORKTREE_NAME set
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # .git should remain unchanged
        assert git_file.read_text() == original_content

    def test_git_pointer_unchanged_if_no_repo_git(self, tmp_path):
        """If /repo-git doesn't exist, .git is not modified."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # No repo_git directory

        original_content = "gitdir: /home/user/repo/.git/worktrees/agent-abc123"
        git_file = workspace / ".git"
        git_file.write_text(original_content)

        script_text = _GIT_POINTER_SCRIPT.replace("/workspace", str(workspace))
        script_text = script_text.replace("/repo-git", str(tmp_path / "repo-git"))
        script = tmp_path / "git_test.sh"
        script.write_text(script_text)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WORKTREE_NAME": "agent-abc123",
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # .git should remain unchanged
        assert git_file.read_text() == original_content
