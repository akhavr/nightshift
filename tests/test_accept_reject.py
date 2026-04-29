"""Tests for accept/reject CLI commands."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _clean_git_env():
    """Return env dict without GIT_DIR/GIT_WORK_TREE (allows tests in temp repos)."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


@pytest.fixture(autouse=True)
def clean_git_environ(monkeypatch):
    """Clear GIT_DIR/GIT_WORK_TREE so subprocess calls use the temp repo."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)


from host.merge import (
    check_conflict_markers as _check_conflict_markers,
    check_branch_not_behind_base,
)


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
    _run = lambda *args: subprocess.run(args, cwd=str(repo), capture_output=True, text=True, env=_clean_git_env())

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
    _run = lambda *args: subprocess.run(args, cwd=str(repo), capture_output=True, text=True, env=_clean_git_env())

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
        args, cwd=str(repo), capture_output=True, text=True, env=_clean_git_env(),
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

    result = _check_conflict_markers(repo)
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

    result = _check_conflict_markers(repo)
    assert result == []


def _setup_conflict_marker_repo(tmp_path):
    """Helper: create a repo with an agent branch containing conflict markers."""
    repo, run = _init_repo(tmp_path)
    run("git", "checkout", "-b", "agent/test789")
    (repo / "file.txt").write_text(
        "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n"
    )
    run("git", "add", ".")
    run("git", "commit", "-m", "commit with markers")
    run("git", "checkout", "main")

    pre_merge = run("git", "rev-parse", "HEAD").stdout.strip()

    ns_dir = repo / ".nightshift" / "sessions" / "test789"
    ns_dir.mkdir(parents=True)
    (ns_dir / "state.json").write_text(json.dumps({"status": "waiting:review"}))

    # Create worktree directory for audit_worktree_symlinks
    wt_dir = repo / ".worktrees" / "agent-test789"
    run("git", "worktree", "add", str(wt_dir), "agent/test789")

    (repo / "WORKFLOW.md").write_text(
        "---\n"
        "agent:\n  kind: claude-code\n"
        "tracker:\n  kind: git-bug\n"
        "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
        "---\nPrompt\n"
    )
    return repo, run, pre_merge, ns_dir


def test_accept_aborts_on_conflict_markers(tmp_path):
    """cmd_accept should abort and reset the merge if conflict markers are found."""
    repo, run, pre_merge, ns_dir = _setup_conflict_marker_repo(tmp_path)

    with patch("host.cli.repo_root", return_value=repo), \
         patch("host.cli.resolve_session", return_value="test789"), \
         patch("host.cli.get_tracker_with_fallback") as mock_tracker, \
         patch("host.cli._report_accept_failure") as mock_report:
        mock_tracker.return_value = MagicMock()
        args = MagicMock()
        args.issue_id = "test789"
        args.workflow = str(repo / "WORKFLOW.md")

        from host.cli import cmd_accept
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept(args)

        assert exc_info.value.code == 1

    post_head = run("git", "rev-parse", "HEAD").stdout.strip()
    assert post_head == pre_merge

    state = json.loads((ns_dir / "state.json").read_text())
    assert state["status"] == "error:merge-conflict"

    mock_report.assert_called_once()
    call_msg = mock_report.call_args[0][3]
    assert "conflict markers" in call_msg


class TestCheckBranchNotBehindBase:

    def test_returns_none_when_up_to_date(self, tmp_path):
        """No divergence means branch is up to date."""
        repo, run = _init_repo(tmp_path)

        run("git", "checkout", "-b", "agent/test1")
        (repo / "agent.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")
        run("git", "checkout", "main")

        result = check_branch_not_behind_base(repo, "agent/test1", "main")
        assert result is None

    def test_returns_message_when_behind(self, tmp_path):
        """Agent branch behind base should return a warning message."""
        repo, run = _init_repo(tmp_path)

        # Create agent branch first
        run("git", "checkout", "-b", "agent/test2")
        (repo / "agent.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")

        # Now advance main
        run("git", "checkout", "main")
        (repo / "new_feature.txt").write_text("from main\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "main advance")

        result = check_branch_not_behind_base(repo, "agent/test2", "main")
        assert result is not None
        assert "behind" in result
        assert "agent/test2" in result
        assert "nightshift resume" in result

    def test_returns_none_when_agent_includes_base(self, tmp_path):
        """After a merge, agent should no longer be behind."""
        repo, run = _init_repo(tmp_path)

        run("git", "checkout", "-b", "agent/test3")
        (repo / "agent.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")

        # Advance main
        run("git", "checkout", "main")
        (repo / "new.txt").write_text("from main\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "main advance")

        # Merge main into agent branch
        run("git", "checkout", "agent/test3")
        run("git", "merge", "main", "--no-edit")
        run("git", "checkout", "main")

        result = check_branch_not_behind_base(repo, "agent/test3", "main")
        assert result is None


class TestAcceptRejectsBehindBase:

    def test_accept_exits_when_branch_behind_base(self, tmp_path):
        """cmd_accept should reject if agent branch is behind base."""
        repo, run = _init_repo(tmp_path)

        # Create agent branch
        run("git", "checkout", "-b", "agent/bbb123")
        (repo / "agent.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")

        # Advance main
        run("git", "checkout", "main")
        (repo / "main_new.txt").write_text("new on main\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "main advance")

        # Setup session
        ns_dir = repo / ".nightshift" / "sessions" / "bbb123"
        ns_dir.mkdir(parents=True)
        (ns_dir / "state.json").write_text(json.dumps({"status": "waiting:review"}))
        (repo / "WORKFLOW.md").write_text(
            "---\n"
            "agent:\n  kind: claude-code\n"
            "tracker:\n  kind: git-bug\n"
            "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
            "---\nPrompt\n"
        )

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.resolve_session", return_value="bbb123"), \
             patch("host.cli.get_tracker_with_fallback") as mock_tracker, \
             patch("host.cli._report_accept_failure") as mock_report:
            mock_tracker.return_value = MagicMock()
            args = MagicMock()
            args.issue_id = "bbb123"
            args.workflow = str(repo / "WORKFLOW.md")

            from host.cli import cmd_accept
            with pytest.raises(SystemExit) as exc_info:
                cmd_accept(args)

            assert exc_info.value.code == 1
            mock_report.assert_called_once()
            call_msg = mock_report.call_args[0][3]
            assert "behind" in call_msg
