"""Tests for docker-entrypoint.sh git worktree handling via environment variables."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear git worktree env so subprocess calls use the temp repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)


def _init_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a real git repo and worktree for cleanup tests."""
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

    test_file = repo / "README.md"
    test_file.write_text("# Test\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    worktree = tmp_path / "workspace"
    subprocess.run(
        ["git", "worktree", "add", "-b", "agent-branch", str(worktree)],
        cwd=repo,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    git_dir = repo / ".git" / "worktrees" / worktree.name
    return repo, worktree, git_dir


def _write_cleanup_harness(tmp_path: Path, worktree: Path, git_dir: Path, exit_code: int) -> Path:
    """Write a shell script that mutates git state and runs the cleanup helper on exit."""
    original_git_file = tmp_path / "original.git"
    original_git_file.write_text((worktree / ".git").read_text())

    script = tmp_path / "entrypoint_harness.sh"
    script.write_text(
        f"""#!/bin/sh
set -eu

export GIT_DIR="{git_dir}"
export GIT_WORK_TREE="{worktree}"
export WORKTREE_PATH="{worktree}"
export ORIGINAL_GIT_CONTENT_FILE="{original_git_file}"

cleanup() {{
    cleanup_status=0
    if python3 /workspace/entrypoint.py --cleanup; then
        cleanup_status=0
    else
        cleanup_status=$?
    fi

    if [ "$cleanup_status" -ne 0 ]; then
        if [ -n "${{ORIGINAL_GIT_CONTENT_FILE:-}}" ] && [ -f "$ORIGINAL_GIT_CONTENT_FILE" ] && [ -f "$WORKTREE_PATH/.git" ]; then
            cp "$ORIGINAL_GIT_CONTENT_FILE" "$WORKTREE_PATH/.git" 2>/dev/null || true
        fi
    fi
}}
trap cleanup EXIT

printf '%s\n' "gitdir: /repo-git/worktrees/{worktree.name}" > "{worktree / '.git'}"
git config core.worktree /workspace

exit {exit_code}
"""
    )
    script.chmod(0o755)
    return script


# WT-1.6: Script to test core.worktree sanitization
_SANITIZE_WORKTREE_SCRIPT = """\
#!/bin/sh

# Simulate GIT_DIR being set
export GIT_DIR="$TEST_GIT_DIR"

# WT-1.6: Sanitize core.worktree if set to container path
if [ -n "$GIT_DIR" ]; then
    WORKTREE_VAL=$(git config --get core.worktree 2>/dev/null || true)
    if [ "$WORKTREE_VAL" = "/workspace" ]; then
        git config --unset core.worktree
        echo "Sanitized core.worktree=/workspace from config"
    fi
fi

# Output current core.worktree for verification
CURRENT_VAL=$(git config --get core.worktree 2>/dev/null || echo "UNSET")
echo "core.worktree=$CURRENT_VAL"
exit 0
"""


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

    def test_git_pointer_restored_on_exit(self, tmp_path, monkeypatch):
        """entrypoint.run() restores .git content after a successful run."""
        _, worktree, _ = _init_repo_with_worktree(tmp_path)
        git_file = worktree / ".git"
        original_content = git_file.read_text()

        import entrypoint

        def fake_main():
            git_file.write_text("gitdir: /tmp/container-pointer\n")

        monkeypatch.setattr(entrypoint, "main", fake_main)

        entrypoint.run(worktree)

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

    def test_git_pointer_restored_on_error(self, tmp_path):
        """EXIT trap restores .git content even when the script exits nonzero."""
        _, worktree, git_dir = _init_repo_with_worktree(tmp_path)
        git_file = worktree / ".git"
        original_content = git_file.read_text()
        script = _write_cleanup_harness(tmp_path, worktree, git_dir, exit_code=1)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

        assert result.returncode == 1
        assert git_file.read_text() == original_content

    def test_git_pointer_restored_when_cleanup_helper_fails(self, tmp_path):
        """Shell fallback restores .git when the Python cleanup helper cannot run."""
        _, worktree, git_dir = _init_repo_with_worktree(tmp_path)
        git_file = worktree / ".git"
        original_content = git_file.read_text()
        script = _write_cleanup_harness(tmp_path, worktree, git_dir, exit_code=1)

        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "WORKTREE_PATH": str(worktree),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

        assert result.returncode == 1
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


class TestCoreWorktreeSanitization:
    """Tests for WT-1.6: sanitize core.worktree in docker-entrypoint.sh."""

    def test_entrypoint_sanitizes_core_worktree(self, tmp_path):
        """core.worktree=/workspace is removed by docker-entrypoint.sh."""
        # Set up a real git repo with a worktree
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

        # Pollute the config with core.worktree=/workspace (simulating container pollution)
        subprocess.run(
            ["git", "config", "core.worktree", "/workspace"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True,
            check=True,
        )

        # Verify the pollution is present
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "/workspace"

        # Create and run the sanitization script
        script = tmp_path / "sanitize_test.sh"
        script.write_text(_SANITIZE_WORKTREE_SCRIPT)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_GIT_DIR": str(git_dir),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "Sanitized core.worktree=/workspace from config" in result.stdout
        assert "core.worktree=UNSET" in result.stdout

        # Double-check: git config should no longer have core.worktree
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True,
            text=True,
        )
        # git config --get returns exit code 1 when key is not found
        assert result.returncode == 1

    def test_sanitization_preserves_other_worktree_values(self, tmp_path):
        """core.worktree with non-/workspace values is preserved."""
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

        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        worktree_name = worktree_path.name
        git_dir = repo / ".git" / "worktrees" / worktree_name

        # Set core.worktree to a different value (not /workspace)
        subprocess.run(
            ["git", "config", "core.worktree", "/some/other/path"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True,
            check=True,
        )

        script = tmp_path / "sanitize_test.sh"
        script.write_text(_SANITIZE_WORKTREE_SCRIPT)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_GIT_DIR": str(git_dir),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        # Should NOT sanitize since value is not /workspace
        assert "Sanitized" not in result.stdout
        assert "core.worktree=/some/other/path" in result.stdout

    def test_sanitization_noop_when_not_set(self, tmp_path):
        """No error when core.worktree is not set."""
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

        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=repo,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        worktree_name = worktree_path.name
        git_dir = repo / ".git" / "worktrees" / worktree_name

        # core.worktree is NOT set — this is the normal state

        script = tmp_path / "sanitize_test.sh"
        script.write_text(_SANITIZE_WORKTREE_SCRIPT)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_GIT_DIR": str(git_dir),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "Sanitized" not in result.stdout
        assert "core.worktree=UNSET" in result.stdout


    def test_no_config_pollution_on_exit(self, tmp_path):
        """EXIT cleanup removes core.worktree=/workspace from the repo config."""
        _, worktree, git_dir = _init_repo_with_worktree(tmp_path)
        git_file = worktree / ".git"
        original_content = git_file.read_text()
        script = _write_cleanup_harness(tmp_path, worktree, git_dir, exit_code=0)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

        assert result.returncode == 0
        assert git_file.read_text() == original_content

        config_result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=worktree,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert config_result.returncode != 0
        assert config_result.stdout.strip() == ""


# WT-1.7: Exit trap sanitization script
_EXIT_TRAP_SCRIPT = """\
#!/bin/sh

# Simulate GIT_DIR being set
export GIT_DIR="$TEST_GIT_DIR"

# WT-1.7: Exit trap for defense-in-depth core.worktree sanitization
cleanup_worktree() {
    if [ -n "$GIT_DIR" ]; then
        WORKTREE_VAL=$(git config --get core.worktree 2>/dev/null || true)
        if [ "$WORKTREE_VAL" = "/workspace" ]; then
            git config --unset core.worktree 2>/dev/null || true
            echo "Exit: sanitized core.worktree"
        fi
    fi
}
trap cleanup_worktree EXIT

# Simulate some work, then exit (trap fires automatically)
echo "Working..."
exit 0
"""


class TestExitTrapSanitization:
    """Tests for WT-1.7: exit trap sanitizes core.worktree on container exit."""

    def test_exit_trap_sanitizes_core_worktree(self, tmp_path):
        """Exit trap sanitizes core.worktree=/workspace on container exit."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        test_file = repo / "README.md"
        test_file.write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        worktree_name = worktree_path.name
        git_dir = repo / ".git" / "worktrees" / worktree_name

        # Simulate pollution that occurs DURING a run (not at startup)
        subprocess.run(
            ["git", "config", "core.worktree", "/workspace"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True, check=True,
        )

        # Verify pollution is present
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "/workspace"

        # Run the exit trap script
        script = tmp_path / "exit_trap_test.sh"
        script.write_text(_EXIT_TRAP_SCRIPT)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_GIT_DIR": str(git_dir),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "Exit: sanitized core.worktree" in result.stdout

        # Verify core.worktree is now unset
        result = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            env={**clean_env, "GIT_DIR": str(git_dir)},
            capture_output=True, text=True,
        )
        assert result.returncode == 1  # Not found

    def test_exit_trap_noop_when_not_polluted(self, tmp_path):
        """Exit trap does nothing when core.worktree is not set to /workspace."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        test_file = repo / "README.md"
        test_file.write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True, env=clean_env)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=repo, capture_output=True, check=True, env=clean_env,
        )

        worktree_name = worktree_path.name
        git_dir = repo / ".git" / "worktrees" / worktree_name

        # core.worktree is NOT set (normal state)

        script = tmp_path / "exit_trap_test.sh"
        script.write_text(_EXIT_TRAP_SCRIPT)
        script.chmod(0o755)

        env = {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_GIT_DIR": str(git_dir),
        }
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "Exit: sanitized core.worktree" not in result.stdout
