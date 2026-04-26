"""Tests for host.git_overlay."""

from pathlib import Path
from unittest.mock import MagicMock, patch

def test_setup_overlay_creates_merged_mount(tmp_path):
    from host.git_overlay import setup_overlay

    repo_git = tmp_path / "repo" / ".git"
    repo_git.mkdir(parents=True)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    with patch("host.git_overlay.is_fuse_overlayfs_available", return_value=True), \
            patch("host.git_overlay.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        merged = setup_overlay(repo_git, session_dir)

    assert merged == session_dir / "git-merged"
    assert (session_dir / "git-upper").exists()
    assert (session_dir / "git-work").exists()
    assert (session_dir / "git-merged").exists()
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "fuse-overlayfs"
    assert str(repo_git) in cmd[2]
    assert str(session_dir / "git-merged") == cmd[-1]
    assert mock_run.call_args.kwargs["capture_output"] is True
    assert mock_run.call_args.kwargs["text"] is True


def test_setup_overlay_raises_on_failure(tmp_path):
    from host.git_overlay import setup_overlay
    import pytest

    repo_git = tmp_path / "repo" / ".git"
    repo_git.mkdir(parents=True)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    with patch("host.git_overlay.is_fuse_overlayfs_available", return_value=True), \
            patch("host.git_overlay.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="mount failed")
        with pytest.raises(RuntimeError, match="fuse-overlayfs failed: mount failed"):
            setup_overlay(repo_git, session_dir)


def test_teardown_overlay_unmounts(tmp_path):
    from host.git_overlay import teardown_overlay

    merged = tmp_path / "session" / "git-merged"
    merged.parent.mkdir(parents=True)
    merged.mkdir()

    with patch("host.git_overlay.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        teardown_overlay(merged)

    mock_run.assert_called_once_with(["fusermount", "-u", str(merged)],
                                     capture_output=True, text=True)


def test_overlay_commit_extraction(tmp_path):
    from host.git_overlay import extract_commits

    upper_dir = tmp_path / "session" / "git-upper"
    repo_git = tmp_path / "repo" / ".git"
    (upper_dir / "objects" / "ab").mkdir(parents=True)
    (repo_git / "objects").mkdir(parents=True)
    (upper_dir / "objects" / "ab" / "new-object").write_text("loose object")
    (upper_dir / "refs" / "heads").mkdir(parents=True)
    (upper_dir / "refs" / "heads" / "agent-test").write_text("deadbeef")
    (upper_dir / "HEAD").write_text("ref: refs/heads/agent-test")

    extract_commits(upper_dir, repo_git)

    assert (repo_git / "objects" / "ab" / "new-object").read_text() == "loose object"
    assert (repo_git / "refs" / "heads" / "agent-test").read_text() == "deadbeef"
    assert (repo_git / "HEAD").read_text() == "ref: refs/heads/agent-test"


def test_overlay_fallback_to_copy(tmp_path):
    from host.git_overlay import setup_git_copy

    repo_git = tmp_path / "repo" / ".git"
    repo_git.mkdir(parents=True)
    (repo_git / "config").write_text("[core]\n")
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    with patch("host.git_overlay.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        copied = setup_git_copy(repo_git, session_dir)

    assert copied == session_dir / "git-copy"
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[:2] == ["cp", "-a"]
    assert str(repo_git) in cmd
    assert str(session_dir / "git-copy") in cmd
    assert mock_run.call_args.kwargs["check"] is True
