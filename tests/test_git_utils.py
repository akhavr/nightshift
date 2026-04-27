"""Tests for host/git_utils.py."""

import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path

from host.git_utils import (
    detect_default_branch, current_branch, branch_exists,
    merge_no_ff, diff_stat, audit_worktree_symlinks,
)


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


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
    def test_detects_escape(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        internal_dir = worktree / "internal"
        internal_dir.mkdir()
        internal_file = internal_dir / "file.txt"
        internal_file.write_text("inside\n")

        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("outside\n")
        escaping_link = worktree / "escape.txt"
        escaping_link.symlink_to(outside)
        internal_link = worktree / "internal-link.txt"
        internal_link.symlink_to(internal_file)

        result = audit_worktree_symlinks(worktree, workspace_root=tmp_path)

        assert result == [(escaping_link, outside.resolve())]

    def test_allows_internal_symlinks(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        target = worktree / "target.txt"
        target.write_text("ok\n")
        link = worktree / "link.txt"
        link.symlink_to(target)

        assert audit_worktree_symlinks(worktree, workspace_root=tmp_path) == []
