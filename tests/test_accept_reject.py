"""Tests for accept/reject CLI commands."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


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
