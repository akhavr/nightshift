"""Tests for CLI helper functions: merge helpers, resolve_merge_ref, etc.

Focuses on functions not covered by test_cli_commands.py or test_accept_reject.py.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.cli import (
    _resolve_merge_ref,
    _check_working_tree_clean,
    _merge_with_rebase_fallback,
    _rebase_and_retry_merge,
    _verify_no_conflict_markers,
    _build_parser,
    _scaffold_file,
    _update_gitignore,
    cmd_logs,
    cmd_status,
    cmd_init,
    cmd_cleanup,
    resolve_session,
    _detect_default_branch,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_args(**kwargs):
    args = MagicMock()
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


def _init_repo(tmp_path, branch_name="main"):
    """Create a git repo with an initial commit on the given branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(
        args, cwd=str(repo), capture_output=True, text=True
    )
    run("git", "init")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "initial")
    run("git", "checkout", "-b", branch_name)
    return repo, run


def _make_config():
    """Return a mock config object with workspace settings."""
    config = MagicMock()
    config.workspace.base_branch = "main"
    config.workspace.root = ".worktrees"
    return config


# ── _resolve_merge_ref ───────────────────────────────────────────────────────


class TestResolveMergeRef:
    def test_branch_exists(self, tmp_path):
        """When branch exists, returns the branch name."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/abc123")
        (repo / "new.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")
        run("git", "checkout", "main")

        wt = tmp_path / "worktree"  # doesn't need to exist
        result = _resolve_merge_ref(repo, "agent/abc123", wt)
        assert result == "agent/abc123"

    def test_branch_gone_worktree_exists(self, tmp_path, capsys):
        """When branch is deleted but worktree exists, returns worktree HEAD."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/abc123")
        (repo / "new.txt").write_text("work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent work")
        agent_head = run("git", "rev-parse", "HEAD").stdout.strip()
        run("git", "checkout", "main")

        # Create a worktree-like dir with a git repo pointing at the commit
        wt = tmp_path / "worktree"
        wt.mkdir()
        # Initialize a temp repo in the worktree dir so git rev-parse HEAD works
        subprocess.run(["git", "init"], cwd=str(wt), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(wt), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(wt), capture_output=True)
        (wt / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(wt), capture_output=True)
        subprocess.run(["git", "commit", "-m", "wt"], cwd=str(wt), capture_output=True)

        # Delete the branch so it can't be found
        run("git", "branch", "-D", "agent/abc123")

        result = _resolve_merge_ref(repo, "agent/abc123", wt)
        # Should return a commit hash (the worktree HEAD)
        assert len(result) == 40  # full SHA
        out = capsys.readouterr().out
        assert "worktree HEAD" in out

    def test_branch_gone_no_worktree_exits(self, tmp_path):
        """When branch doesn't exist and no worktree, exits with code 1."""
        repo, run = _init_repo(tmp_path)
        wt = tmp_path / "nonexistent_worktree"

        with pytest.raises(SystemExit) as exc_info:
            _resolve_merge_ref(repo, "agent/nonexistent", wt)
        assert exc_info.value.code == 1

    def test_branch_gone_worktree_unreadable_exits(self, tmp_path):
        """When branch is gone and worktree HEAD is unreadable, exits."""
        repo, run = _init_repo(tmp_path)
        # Create a worktree dir without git init (so rev-parse fails)
        wt = tmp_path / "broken_wt"
        wt.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            _resolve_merge_ref(repo, "agent/nonexistent", wt)
        assert exc_info.value.code == 1


# ── _check_working_tree_clean ────────────────────────────────────────────────


class TestCheckWorkingTreeClean:
    def test_clean_tree_passes(self, tmp_path):
        """No error when working tree is clean."""
        repo, run = _init_repo(tmp_path)
        config = _make_config()
        # Should not raise
        _check_working_tree_clean(repo, "main", config, "issue-1")

    def test_dirty_tree_exits(self, tmp_path, capsys):
        """Exits with code 1 when there are modified tracked files."""
        repo, run = _init_repo(tmp_path)
        (repo / "file.txt").write_text("modified\n")
        config = _make_config()

        with patch("host.cli._report_accept_failure") as mock_report, \
             pytest.raises(SystemExit) as exc_info:
            _check_working_tree_clean(repo, "main", config, "issue-1")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not clean" in err
        mock_report.assert_called_once()

    def test_untracked_files_ignored(self, tmp_path):
        """Untracked files (prefixed with ??) should not trigger dirty check."""
        repo, run = _init_repo(tmp_path)
        (repo / "untracked.txt").write_text("untracked\n")
        config = _make_config()
        # Should not raise — untracked files are ignored
        _check_working_tree_clean(repo, "main", config, "issue-1")


# ── _merge_with_rebase_fallback ──────────────────────────────────────────────


class TestMergeWithRebaseFallback:
    def test_clean_merge_succeeds(self, tmp_path):
        """Successful merge returns without error."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/test1")
        (repo / "new.txt").write_text("agent work\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent commit")
        run("git", "checkout", "main")

        config = _make_config()
        # Should not raise
        _merge_with_rebase_fallback(
            repo, "agent/test1", "agent/test1", "main", "issue-1", config
        )

        # Verify merge happened
        log = run("git", "log", "--oneline").stdout
        assert "agent commit" in log

    def test_uncommitted_changes_error_exits(self, tmp_path, capsys):
        """When merge fails due to local changes, exits immediately."""
        repo, run = _init_repo(tmp_path)

        # Use mock to simulate the "local changes" merge failure
        mock_merge_result = MagicMock(
            returncode=1,
            stderr="error: Your local changes would be overwritten by merge"
        )

        config = _make_config()
        with patch("host.cli.subprocess.run", return_value=mock_merge_result), \
             patch("host.cli._report_accept_failure") as mock_report, \
             pytest.raises(SystemExit) as exc_info:
            _merge_with_rebase_fallback(
                repo, "agent/test1", "agent/test1", "main", "issue-1", config
            )

        assert exc_info.value.code == 1
        mock_report.assert_called_once()

    def test_conflict_triggers_rebase(self, tmp_path):
        """On merge conflict, should abort merge and call _rebase_and_retry_merge."""
        repo, run = _init_repo(tmp_path)

        mock_merge_result = MagicMock(
            returncode=1,
            stderr="CONFLICT (content): Merge conflict in file.txt"
        )
        mock_abort_result = MagicMock(returncode=0)

        config = _make_config()
        with patch("host.cli.subprocess.run") as mock_run, \
             patch("host.cli._rebase_and_retry_merge") as mock_rebase:
            mock_run.side_effect = [mock_merge_result, mock_abort_result]
            _merge_with_rebase_fallback(
                repo, "agent/test1", "agent/test1", "main", "issue-1", config
            )

        mock_rebase.assert_called_once_with(
            repo, "agent/test1", "main", "issue-1", config
        )


# ── _rebase_and_retry_merge ─────────────────────────────────────────────────


class TestRebaseAndRetryMerge:
    def test_rebase_success_then_merge_success(self, tmp_path):
        """Successful rebase + merge completes without error."""
        repo, run = _init_repo(tmp_path)
        # Create diverged branches
        run("git", "checkout", "-b", "agent/test1")
        (repo / "agent_file.txt").write_text("agent\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "agent commit")
        run("git", "checkout", "main")
        (repo / "main_file.txt").write_text("main change\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "main commit")

        config = _make_config()
        # Should not raise — rebase should succeed, then merge
        _rebase_and_retry_merge(repo, "agent/test1", "main", "issue-1", config)

        log = run("git", "log", "--oneline").stdout
        assert "agent commit" in log

    def test_rebase_fails_exits(self, tmp_path, capsys):
        """When rebase fails, aborts rebase, checks out old branch, exits."""
        repo, run = _init_repo(tmp_path)

        # Mock subprocess calls for: show-current, checkout, rebase(fail), rebase --abort, checkout
        call_results = [
            MagicMock(stdout="main\n", returncode=0),       # git branch --show-current
            MagicMock(returncode=0),                          # git checkout branch
            MagicMock(returncode=1, stderr="CONFLICT\n"),     # git rebase (fails)
            MagicMock(returncode=0),                          # git rebase --abort
            MagicMock(returncode=0),                          # git checkout old_branch
        ]

        config = _make_config()
        with patch("host.cli.subprocess.run", side_effect=call_results), \
             patch("host.cli._report_accept_failure") as mock_report, \
             pytest.raises(SystemExit) as exc_info:
            _rebase_and_retry_merge(repo, "agent/test1", "main", "issue-1", config)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Rebase failed" in err
        mock_report.assert_called_once()
        # Check that the report message suggests manual resolution
        call_msg = mock_report.call_args[0][3]
        assert "manual resolution" in call_msg.lower() or "revise" in call_msg.lower()

    def test_rebase_succeeds_but_merge_fails_exits(self, tmp_path, capsys):
        """When rebase succeeds but retry merge still fails, exits."""
        repo, run = _init_repo(tmp_path)

        # Mock: show-current, checkout, rebase(ok), checkout-back, merge(fail)
        call_results = [
            MagicMock(stdout="main\n", returncode=0),       # git branch --show-current
            MagicMock(returncode=0),                          # git checkout branch
            MagicMock(returncode=0, stderr=""),                # git rebase (succeeds)
            MagicMock(returncode=0),                          # git checkout old_branch
            MagicMock(returncode=1, stderr="merge failed\n"), # git merge --no-ff (fails)
        ]

        config = _make_config()
        with patch("host.cli.subprocess.run", side_effect=call_results), \
             patch("host.cli._report_accept_failure") as mock_report, \
             pytest.raises(SystemExit) as exc_info:
            _rebase_and_retry_merge(repo, "agent/test1", "main", "issue-1", config)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "still failed" in err.lower()
        mock_report.assert_called_once()


# ── _verify_no_conflict_markers ──────────────────────────────────────────────


class TestVerifyNoConflictMarkers:
    def test_clean_merge_passes(self, tmp_path):
        """No conflict markers means no error."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/clean1")
        (repo / "clean.txt").write_text("clean content\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "clean commit")
        run("git", "checkout", "main")
        run("git", "merge", "--no-ff", "agent/clean1", "-m", "Merge")

        sessions = repo / ".nightshift" / "sessions" / "clean1"
        sessions.mkdir(parents=True)
        (sessions / "state.json").write_text(json.dumps({"status": "working"}))

        config = _make_config()
        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"):
            # Should not raise
            _verify_no_conflict_markers(repo, config, "issue-1", "clean1")

    def test_markers_found_resets_and_exits(self, tmp_path, capsys):
        """Conflict markers trigger reset and sys.exit(1)."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/markers1")
        (repo / "file.txt").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "commit with markers")
        run("git", "checkout", "main")
        pre_merge = run("git", "rev-parse", "HEAD").stdout.strip()
        run("git", "merge", "--no-ff", "agent/markers1", "-m", "Merge")

        sessions = repo / ".nightshift" / "sessions" / "markers1"
        sessions.mkdir(parents=True)
        (sessions / "state.json").write_text(json.dumps({"status": "working"}))

        config = _make_config()
        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"), \
             patch("host.cli._report_accept_failure") as mock_report, \
             pytest.raises(SystemExit) as exc_info:
            _verify_no_conflict_markers(repo, config, "issue-1", "markers1")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Conflict markers" in err

        # HEAD should be reset
        post_head = run("git", "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_merge

        # State should be updated
        state = json.loads((sessions / "state.json").read_text())
        assert state["status"] == "error:merge-conflict"

        mock_report.assert_called_once()

    def test_no_session_state_still_exits(self, tmp_path, capsys):
        """Even without state.json, still exits on conflict markers."""
        repo, run = _init_repo(tmp_path)
        run("git", "checkout", "-b", "agent/nostate1")
        (repo / "file.txt").write_text("<<<<<<< HEAD\nbad\n=======\nworse\n>>>>>>> x\n")
        run("git", "add", ".")
        run("git", "commit", "-m", "markers")
        run("git", "checkout", "main")
        run("git", "merge", "--no-ff", "agent/nostate1", "-m", "Merge")

        # No session dir at all
        config = _make_config()
        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"), \
             patch("host.cli._report_accept_failure"), \
             pytest.raises(SystemExit) as exc_info:
            _verify_no_conflict_markers(repo, config, "issue-1", "nostate1")

        assert exc_info.value.code == 1


# ── cmd_logs ─────────────────────────────────────────────────────────────────


class TestCmdLogs:
    def test_no_log_file(self, tmp_path, capsys):
        """Prints error when log file doesn't exist."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sd = repo / ".nightshift" / "sessions" / "logtest12345"
        sd.mkdir(parents=True)
        # No raw-output.log

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.resolve_session", return_value="logtest12345"):
            cmd_logs(_make_args(issue_id="logtest12345"))

        err = capsys.readouterr().err
        assert "No log file" in err

    def test_log_file_exists_calls_tail(self, tmp_path):
        """When log file exists, calls tail -f on it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sd = repo / ".nightshift" / "sessions" / "logtest67890"
        sd.mkdir(parents=True)
        (sd / "raw-output.log").write_text("some log output\n")

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.resolve_session", return_value="logtest67890"), \
             patch("host.cli.subprocess.run") as mock_run:
            cmd_logs(_make_args(issue_id="logtest67890"))

        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert cmd_args[0] == "tail"
        assert cmd_args[1] == "-f"
        assert "raw-output.log" in cmd_args[2]


# ── resolve_session ──────────────────────────────────────────────────────────


class TestResolveSession:
    def test_exact_match(self, tmp_path):
        """Single matching session dir returns its name."""
        repo = tmp_path / "repo"
        sd = repo / ".nightshift" / "sessions" / "abc123456789"
        sd.mkdir(parents=True)

        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"):
            result = resolve_session("abc123456789")
        assert result == "abc123456789"

    def test_prefix_match(self, tmp_path):
        """Prefix of session ID resolves to full ID."""
        repo = tmp_path / "repo"
        sd = repo / ".nightshift" / "sessions" / "abc123456789"
        sd.mkdir(parents=True)

        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"):
            result = resolve_session("abc123456789full")
        assert result == "abc123456789"

    def test_ambiguous_match_exits(self, tmp_path):
        """Multiple matches cause sys.exit(1)."""
        repo = tmp_path / "repo"
        sessions = repo / ".nightshift" / "sessions"
        # Both dirs share the same first 12 chars "aabbccddee11"
        (sessions / "aabbccddee11_first").mkdir(parents=True)
        (sessions / "aabbccddee11_second").mkdir(parents=True)

        with patch("host.cli.sessions_dir", return_value=sessions), \
             pytest.raises(SystemExit) as exc_info:
            resolve_session("aabbccddee11anything")
        assert exc_info.value.code == 1

    def test_no_sessions_dir_exits(self, tmp_path):
        """Missing sessions dir causes sys.exit(1)."""
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("host.cli.sessions_dir", return_value=repo / ".nightshift" / "sessions"), \
             pytest.raises(SystemExit) as exc_info:
            resolve_session("anything")
        assert exc_info.value.code == 1

    def test_no_match_returns_truncated(self, tmp_path):
        """No match returns first 12 chars of the issue_id."""
        repo = tmp_path / "repo"
        sessions = repo / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        with patch("host.cli.sessions_dir", return_value=sessions):
            result = resolve_session("abcdef1234567890")
        assert result == "abcdef123456"


# ── _detect_default_branch ───────────────────────────────────────────────────


class TestDetectDefaultBranch:
    def test_returns_current_branch(self, tmp_path):
        """Falls back to current branch when no remote."""
        repo, run = _init_repo(tmp_path, branch_name="develop")
        result = _detect_default_branch(repo)
        assert result == "develop"

    def test_fallback_to_main(self, tmp_path):
        """Returns 'main' when all detection methods fail."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Not a git repo — both commands will fail
        result = _detect_default_branch(repo)
        assert result == "main"


# ── cmd_init edge cases ──────────────────────────────────────────────────────


class TestCmdInitEdgeCases:
    def test_not_in_git_repo_exits(self, tmp_path, capsys):
        """cmd_init exits when not inside a git repository."""
        with patch("host.cli.repo_root", side_effect=subprocess.CalledProcessError(1, "git")), \
             pytest.raises(SystemExit) as exc_info:
            cmd_init(_make_args(force=False))
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Not inside a git repository" in err

    def test_detects_non_main_default_branch(self, tmp_path, capsys):
        """cmd_init uses detected default branch name in WORKFLOW.md."""
        repo, run = _init_repo(tmp_path, branch_name="develop")

        with patch("host.cli.repo_root", return_value=repo):
            cmd_init(_make_args(force=False))

        content = (repo / "WORKFLOW.md").read_text()
        assert "base_branch: develop" in content

    def test_gitignore_appended_to_existing(self, tmp_path, capsys):
        """Entries are appended to existing .gitignore with a newline separator."""
        repo, run = _init_repo(tmp_path)
        (repo / ".gitignore").write_text("*.pyc\n__pycache__/")

        with patch("host.cli.repo_root", return_value=repo):
            cmd_init(_make_args(force=False))

        content = (repo / ".gitignore").read_text()
        assert "*.pyc" in content
        assert ".env" in content
        assert ".nightshift/" in content


# ── cmd_cleanup edge cases ───────────────────────────────────────────────────


class TestCmdCleanupEdgeCases:
    def test_no_session_dir_still_completes(self, tmp_path, capsys):
        """Cleanup completes even when session dir doesn't exist."""
        repo, run = _init_repo(tmp_path)
        (repo / "WORKFLOW.md").write_text(
            "---\n"
            "agent:\n  kind: claude-code\n"
            "tracker:\n  kind: git-bug\n"
            "workspace:\n  kind: worktree\n  base_branch: main\n  root: .worktrees\n"
            "---\nPrompt\n"
        )
        # No session dir exists
        (repo / ".nightshift" / "sessions").mkdir(parents=True)

        with patch("host.cli.repo_root", return_value=repo), \
             patch("host.cli.resolve_session", return_value="nosess123456"), \
             patch("host.session_utils.subprocess.run") as mock_git:
            mock_git.return_value = MagicMock(returncode=0)
            cmd_cleanup(_make_args(issue_id="nosess123456", keep_session=False))

        out = capsys.readouterr().out
        assert "nosess123456" in out


# ── cmd_status edge cases ────────────────────────────────────────────────────


class TestCmdStatusEdgeCases:
    def test_empty_sessions_dir(self, tmp_path, capsys):
        """Empty sessions dir prints header but no entries."""
        repo = tmp_path / "repo"
        sessions = repo / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        with patch("host.cli.repo_root", return_value=repo):
            cmd_status(_make_args())

        out = capsys.readouterr().out
        assert "SESSION" in out
        assert "STATUS" in out

    def test_missing_fields_in_state(self, tmp_path, capsys):
        """State with missing fields uses defaults (? for status, 0 for step)."""
        repo = tmp_path / "repo"
        sd = repo / ".nightshift" / "sessions" / "minimal12345"
        sd.mkdir(parents=True)
        (sd / "state.json").write_text(json.dumps({}))

        with patch("host.cli.repo_root", return_value=repo):
            cmd_status(_make_args())

        out = capsys.readouterr().out
        assert "minimal12345" in out
        assert "?" in out


# ── _build_parser ─────────────────────────────────────────────────────────


class TestBuildParser:
    def test_parser_has_all_commands(self):
        p = _build_parser()
        no_arg_cmds = {"watcher", "status", "init"}
        two_arg_cmds = {"answer": ["test-id", "msg"]}
        for cmd in ["start", "resume", "answer", "watcher", "status",
                     "logs", "history", "init", "accept", "reject",
                     "revise", "cleanup"]:
            if cmd in no_arg_cmds:
                argv = [cmd]
            elif cmd in two_arg_cmds:
                argv = [cmd] + two_arg_cmds[cmd]
            else:
                argv = [cmd, "test-id"]
            args = p.parse_args(argv)
            assert hasattr(args, "func")

    def test_start_with_max_turns(self):
        p = _build_parser()
        args = p.parse_args(["start", "issue-1", "--max-turns", "20"])
        assert args.max_turns == 20

    def test_cleanup_keep_session(self):
        p = _build_parser()
        args = p.parse_args(["cleanup", "issue-1", "--keep-session"])
        assert args.keep_session is True


# ── _scaffold_file ────────────────────────────────────────────────────────


class TestScaffoldFile:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "test.md"
        _scaffold_file(path, "content", force=False)
        assert path.read_text() == "content"

    def test_skips_existing(self, tmp_path, capsys):
        path = tmp_path / "test.md"
        path.write_text("original")
        _scaffold_file(path, "new", force=False)
        assert path.read_text() == "original"
        assert "already exists" in capsys.readouterr().out

    def test_force_overwrites(self, tmp_path):
        path = tmp_path / "test.md"
        path.write_text("original")
        _scaffold_file(path, "new", force=True)
        assert path.read_text() == "new"

    def test_with_label(self, tmp_path, capsys):
        path = tmp_path / "test.md"
        _scaffold_file(path, "x", force=False, label="main branch")
        assert "main branch" in capsys.readouterr().out


# ── _update_gitignore ─────────────────────────────────────────────────────


class TestUpdateGitignore:
    def test_creates_gitignore(self, tmp_path, capsys):
        _update_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert ".env" in content
        assert ".nightshift/" in content

    def test_skips_existing_entries(self, tmp_path, capsys):
        (tmp_path / ".gitignore").write_text(".env\n.worktrees/\n.nightshift/\n")
        _update_gitignore(tmp_path)
        assert "already has" in capsys.readouterr().out
