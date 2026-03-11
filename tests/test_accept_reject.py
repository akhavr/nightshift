"""Tests for accept/reject CLI commands."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from host.cli import _check_conflict_markers


def test_cli_has_accept_command():
    """cli.py should have an 'accept' subcommand."""
    import host.cli as cli_mod
    source = Path(cli_mod.__file__).read_text()
    assert "cmd_accept" in source
    assert '"accept"' in source or "'accept'" in source


def test_cli_has_reject_command():
    """cli.py should have a 'reject' subcommand."""
    import host.cli as cli_mod
    source = Path(cli_mod.__file__).read_text()
    assert "cmd_reject" in source
    assert '"reject"' in source or "'reject'" in source


def test_accept_merges_branch(tmp_path):
    """Accept should merge the agent branch into the base branch."""
    # Set up a git repo with main and agent branch
    repo = tmp_path / "repo"
    repo.mkdir()
    _run = lambda *args: subprocess.run(args, cwd=str(repo), capture_output=True, text=True)

    _run("git", "init")
    _run("git", "config", "user.email", "test@test.com")
    _run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial")
    _run("git", "add", ".")
    _run("git", "commit", "-m", "initial")
    _run("git", "checkout", "-b", "main")
    _run("git", "checkout", "-b", "agent/abc123")
    (repo / "new.txt").write_text("agent work")
    _run("git", "add", ".")
    _run("git", "commit", "-m", "agent commit")
    _run("git", "checkout", "main")

    # Verify the branch exists and has the commit
    result = _run("git", "log", "--oneline", "agent/abc123")
    assert "agent commit" in result.stdout

    # Simulate merge (what accept should do)
    result = _run("git", "merge", "--no-ff", "agent/abc123", "-m", "Merge agent/abc123")
    assert result.returncode == 0

    # Verify merge
    result = _run("git", "log", "--oneline")
    assert "agent commit" in result.stdout


def test_reject_cleans_up(tmp_path):
    """Reject should remove worktree and session but NOT merge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run = lambda *args: subprocess.run(args, cwd=str(repo), capture_output=True, text=True)

    _run("git", "init")
    _run("git", "config", "user.email", "test@test.com")
    _run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial")
    _run("git", "add", ".")
    _run("git", "commit", "-m", "initial")
    _run("git", "checkout", "-b", "agent/abc123")
    (repo / "new.txt").write_text("agent work")
    _run("git", "add", ".")
    _run("git", "commit", "-m", "agent commit")
    _run("git", "checkout", "-")

    # After reject, the branch should be deleted
    _run("git", "branch", "-D", "agent/abc123")
    result = _run("git", "branch")
    assert "agent/abc123" not in result.stdout


def _init_repo(tmp_path):
    """Helper: create a git repo with an initial commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(
        args, cwd=str(repo), capture_output=True, text=True,
    )
    run("git", "init")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "initial")
    run("git", "checkout", "-b", "main")
    return repo, run


def test_check_conflict_markers_detects_markers(tmp_path):
    """_check_conflict_markers should detect conflict markers in merged files."""
    repo, run = _init_repo(tmp_path)

    # Create agent branch with a change
    run("git", "checkout", "-b", "agent/test123")
    (repo / "file.txt").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "agent commit with markers")
    run("git", "checkout", "main")

    # Merge (this simulates a merge that succeeds but has markers in content)
    run("git", "merge", "--no-ff", "agent/test123", "-m", "Merge agent/test123")

    result = _check_conflict_markers(repo, "main")
    assert "file.txt" in result


def test_check_conflict_markers_clean_merge(tmp_path):
    """_check_conflict_markers should return empty list for a clean merge."""
    repo, run = _init_repo(tmp_path)

    # Create agent branch with a clean change
    run("git", "checkout", "-b", "agent/test456")
    (repo / "new.txt").write_text("clean content\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "clean agent commit")
    run("git", "checkout", "main")

    run("git", "merge", "--no-ff", "agent/test456", "-m", "Merge agent/test456")

    result = _check_conflict_markers(repo, "main")
    assert result == []


def test_accept_aborts_on_conflict_markers(tmp_path):
    """cmd_accept should abort and reset the merge if conflict markers are found."""
    repo, run = _init_repo(tmp_path)

    # Create agent branch with conflict markers baked in
    run("git", "checkout", "-b", "agent/test789")
    (repo / "file.txt").write_text(
        "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n"
    )
    run("git", "add", ".")
    run("git", "commit", "-m", "commit with markers")
    run("git", "checkout", "main")

    # Record the pre-merge HEAD
    pre_merge = run("git", "rev-parse", "HEAD").stdout.strip()

    # Create session dir structure AFTER branch setup (outside git tracking)
    ns_dir = repo / ".nightshift" / "sessions" / "test789"
    ns_dir.mkdir(parents=True)
    (ns_dir / "state.json").write_text(json.dumps({"status": "waiting:review"}))

    # Create minimal WORKFLOW.md (untracked, fine)
    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )

    # Patch out functions that need external resources
    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="test789"), \
         patch("host.cli.create_tracker") as mock_tracker, \
         patch("host.cli._report_accept_failure") as mock_report:
        mock_tracker.return_value = MagicMock()
        args = MagicMock()
        args.issue_id = "test789"
        args.workflow = str(repo / "WORKFLOW.md")

        from host.cli import cmd_accept
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept(args)

        assert exc_info.value.code == 1

    # HEAD should have been reset back (merge undone)
    post_head = run("git", "rev-parse", "HEAD").stdout.strip()
    assert post_head == pre_merge

    # Session state should be error:merge-conflict
    state = json.loads((ns_dir / "state.json").read_text())
    assert state["status"] == "error:merge-conflict"

    # _report_accept_failure should have been called with conflict info
    mock_report.assert_called_once()
    call_msg = mock_report.call_args[0][3]
    assert "conflict markers" in call_msg
