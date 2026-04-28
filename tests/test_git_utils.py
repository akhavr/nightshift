"""Tests for host/git_utils.py."""

import subprocess
import os
from unittest.mock import patch, MagicMock
from pathlib import Path

from host.git_utils import (
    detect_default_branch, current_branch, branch_exists,
    merge_no_ff, diff_stat, audit_worktree_symlinks,
)


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _clean_git_env():
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


class TestDetectDefaultBranch:
    def test_uses_origin_head(self):
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _completed("refs/remotes/origin/main\n")
            assert detect_default_branch(Path("/repo")) == "main"

    def test_falls_back_to_current_branch(self):
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(returncode=1),
                _completed("develop\n"),
            ]
            assert detect_default_branch(Path("/repo")) == "develop"

    def test_defaults_to_main(self):
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(returncode=1),
                _completed(returncode=1),
            ]
            assert detect_default_branch(Path("/repo")) == "main"


class TestCurrentBranch:
    def test_success(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed("feature\n")):
            assert current_branch(Path("/repo")) == "feature"

    def test_failure(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed(returncode=1)):
            assert current_branch(Path("/repo")) == ""


class TestBranchExists:
    def test_exists(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed()):
            assert branch_exists(Path("/repo"), "main") is True

    def test_not_exists(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed(returncode=1)):
            assert branch_exists(Path("/repo"), "nope") is False


class TestMergeNoFf:
    def test_calls_git_merge(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed()) as mock_run:
            merge_no_ff(Path("/repo"), "feature", "merge msg")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "merge" in args
            assert "--no-ff" in args


class TestDiffStat:
    def test_success(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed("1 file changed")):
            assert diff_stat(Path("/repo"), "main", "feature") == "1 file changed"

    def test_failure(self):
        with patch("host.git_utils.subprocess.run", return_value=_completed(returncode=1)):
            assert diff_stat(Path("/repo"), "main", "feature") == "N/A"


class TestAuditWorktreeSymlinks:
    def test_skips_gitignored_symlinks(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(), check=True)
        (repo / ".gitignore").write_text(".venv/\n")
        (repo / "tracked.txt").write_text("tracked\n")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(), check=True)

        venv = repo / ".venv"
        venv.mkdir()
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("outside\n")
        ignored_link = venv / "python"
        ignored_link.symlink_to(outside)

        assert audit_worktree_symlinks(repo, workspace_root=tmp_path) == []

    def test_catches_tracked_escaping_symlinks(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(worktree), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(worktree), capture_output=True, text=True, env=_clean_git_env(), check=True)
        tracked = worktree / "escape.txt"
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("outside\n")
        tracked.symlink_to(outside)
        subprocess.run(["git", "add", "escape.txt"], cwd=str(worktree), capture_output=True, text=True, env=_clean_git_env(), check=True)
        subprocess.run(["git", "commit", "-m", "add escape link"], cwd=str(worktree), capture_output=True, text=True, env=_clean_git_env(), check=True)

        result = audit_worktree_symlinks(worktree, workspace_root=tmp_path)

        assert result == [(tracked, outside.resolve())]

    def test_allows_internal_symlinks(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        target = worktree / "target.txt"
        target.write_text("ok\n")
        link = worktree / "link.txt"
        link.symlink_to(target)

        assert audit_worktree_symlinks(worktree, workspace_root=tmp_path) == []


from host.git_utils import validate_git_objects, auto_commit_dirty_worktree


class TestValidateGitObjects:
    def test_clean_repo_passes(self, tmp_path):
        """A repo with no fsck errors passes validation."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _completed(returncode=0)
            is_valid, errors = validate_git_objects(git_dir)
            assert is_valid is True
            assert errors == []

    def test_corruption_detected(self, tmp_path):
        """Real corruption errors are reported."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="error: corrupt loose object abc123\n"
            )
            is_valid, errors = validate_git_objects(git_dir)
            assert is_valid is False
            assert any("corrupt loose object" in e for e in errors)

    def test_git_bug_noise_filtered(self, tmp_path):
        """Git-bug related warnings are ignored."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="dangling blob abc123 (git-bug)\n"
            )
            is_valid, errors = validate_git_objects(git_dir)
            assert is_valid is True
            assert errors == []

    def test_unknown_object_type_filtered(self, tmp_path):
        """Unknown object type errors (git-bug) are ignored."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="error: Unknown object type 14\n"
            )
            is_valid, errors = validate_git_objects(git_dir)
            assert is_valid is True
            assert errors == []

    def test_nonexistent_dir_passes(self):
        """Non-existent directory returns True (no corruption)."""
        is_valid, errors = validate_git_objects(Path("/nonexistent/path"))
        assert is_valid is True
        assert errors == []


class TestAutoCommitDirtyWorktree:
    def test_commits_dirty_worktree(self, tmp_path):
        """Uncommitted changes get committed."""
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed("M file.py\n"),  # status --porcelain
                _completed(),  # git add -A
                _completed(),  # git commit
            ]
            result = auto_commit_dirty_worktree(tmp_path)
            assert result is True
            assert mock_run.call_count == 3

    def test_clean_worktree_no_commit(self, tmp_path):
        """Clean worktree does not create a commit."""
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _completed("")  # empty status
            result = auto_commit_dirty_worktree(tmp_path)
            assert result is False
            assert mock_run.call_count == 1

    def test_commit_failure_returns_false(self, tmp_path):
        """Failed commit returns False."""
        with patch("host.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed("M file.py\n"),  # status
                _completed(),  # add
                _completed(returncode=1),  # commit fails
            ]
            result = auto_commit_dirty_worktree(tmp_path)
            assert result is False

    def test_nonexistent_dir_returns_false(self):
        """Non-existent directory returns False."""
        result = auto_commit_dirty_worktree(Path("/nonexistent/path"))
        assert result is False
