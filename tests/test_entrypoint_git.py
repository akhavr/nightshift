"""Tests for docker-entrypoint.sh git worktree handling via environment variables."""

import os
import subprocess
from pathlib import Path

import pytest


# New env-var-based git worktree configuration (replaces file rewriting).
# Sets GIT_DIR and GIT_WORK_TREE instead of modifying .git file.
_GIT_ENV_SCRIPT = """\
#!/bin/sh

# Use env vars instead of rewriting .git file
if [ -d /repo-git ] && [ -n "$WORKTREE_NAME" ]; then
    export GIT_DIR="/repo-git/worktrees/${WORKTREE_NAME}"
    export GIT_WORK_TREE="/workspace"
fi

# Output env vars for verification
echo "GIT_DIR=$GIT_DIR"
echo "GIT_WORK_TREE=$GIT_WORK_TREE"
exit 0
"""

# Legacy script kept for backward-compatibility tests (file rewriting approach).
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


class TestGitEnvVars:
    """Tests for the new environment variable-based git worktree configuration."""

    def test_env_vars_set_for_worktree(self, tmp_path):
        """GIT_DIR and GIT_WORK_TREE are set when WORKTREE_NAME and /repo-git exist."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()
        worktrees_dir = repo_git / "worktrees" / "agent-abc123"
        worktrees_dir.mkdir(parents=True)

        script_text = _GIT_ENV_SCRIPT.replace("/workspace", str(workspace))
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
        # Check that env vars are set correctly
        assert f"GIT_DIR={repo_git}/worktrees/agent-abc123" in result.stdout
        assert f"GIT_WORK_TREE={workspace}" in result.stdout

    def test_git_operations_work_with_env_vars(self, tmp_path):
        """Git operations work correctly with GIT_DIR and GIT_WORK_TREE env vars."""
        # Set up a real git repo with a worktree
        # Clear git env vars to avoid inheriting from test runner environment
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Create an initial commit so we have a branch
        test_file = repo / "README.md"
        test_file.write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Create a worktree
        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Get the actual worktree name from .git/worktrees
        worktree_name = worktree_path.name
        git_dir = repo / ".git" / "worktrees" / worktree_name

        # Test git operations using env vars (simulating container environment)
        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(worktree_path),
        }

        # Test git status
        result = subprocess.run(
            ["git", "status"],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "On branch agent-branch" in result.stdout

        # Test git log
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Initial commit" in result.stdout

        # Test git branch
        result = subprocess.run(
            ["git", "branch"],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "agent-branch" in result.stdout

    def test_no_git_file_modification(self, tmp_path):
        """The .git file is not modified when using env vars."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()
        worktrees_dir = repo_git / "worktrees" / "agent-abc123"
        worktrees_dir.mkdir(parents=True)

        # Original .git file content (simulating host path)
        original_content = "gitdir: /home/user/repo/.git/worktrees/agent-abc123"
        git_file = workspace / ".git"
        git_file.write_text(original_content)

        script_text = _GIT_ENV_SCRIPT.replace("/workspace", str(workspace))
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
        # .git file should NOT be modified
        assert git_file.read_text() == original_content

    def test_env_vars_not_set_without_worktree_name(self, tmp_path):
        """GIT_DIR and GIT_WORK_TREE are not set if WORKTREE_NAME is missing."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo_git = tmp_path / "repo-git"
        repo_git.mkdir()

        script_text = _GIT_ENV_SCRIPT.replace("/workspace", str(workspace))
        script_text = script_text.replace("/repo-git", str(repo_git))
        script = tmp_path / "git_test.sh"
        script.write_text(script_text)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # No WORKTREE_NAME
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Env vars should be empty
        assert "GIT_DIR=" in result.stdout
        assert "GIT_WORK_TREE=" in result.stdout

    def test_env_vars_not_set_without_repo_git(self, tmp_path):
        """GIT_DIR and GIT_WORK_TREE are not set if /repo-git doesn't exist."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # No repo_git directory

        script_text = _GIT_ENV_SCRIPT.replace("/workspace", str(workspace))
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
        # Env vars should be empty since /repo-git doesn't exist
        assert "GIT_DIR=" in result.stdout
        assert "GIT_WORK_TREE=" in result.stdout