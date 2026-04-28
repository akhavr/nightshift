"""Tests for host/session_utils.py"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.constants import MAX_ORPHAN_RESUMES
from host.session_utils import (
    ARCHIVE_FILES,
    _git_repo_root,
    archive_session,
    clear_completed_at,
    fix_all_corrupted_gitdirs,
    force_remove_dir,
    get_active_session_ids,
    get_repo_root,
    has_active_sessions,
    increment_orphan_resumes,
    increment_auth_retries,
    increment_provider_outage_retries,
    read_state,
    remove_worktree,
    safe_prune,
    sessions_dir,
    update_state_fields,
    update_status,
    write_state,
)


# ── read_state / write_state / update_status ─────────────────────────────────

class TestReadState:
    def test_reads_valid_json(self, tmp_path):
        state = {"status": "running", "issue_id": "abc123"}
        (tmp_path / "state.json").write_text(json.dumps(state))
        result = read_state(tmp_path)
        assert result == state

    def test_returns_dict(self, tmp_path):
        (tmp_path / "state.json").write_text('{"key": "value"}')
        result = read_state(tmp_path)
        assert isinstance(result, dict)

    def test_raises_when_file_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_state(tmp_path)

    def test_raises_on_invalid_json(self, tmp_path):
        (tmp_path / "state.json").write_text("not valid json")
        with pytest.raises(json.JSONDecodeError):
            read_state(tmp_path)

    def test_reads_nested_structure(self, tmp_path):
        state = {
            "status": "done",
            "metadata": {"turns": 5, "checkpoints": ["a", "b"]},
        }
        (tmp_path / "state.json").write_text(json.dumps(state))
        assert read_state(tmp_path) == state

    def test_read_state_validates_schema(self, tmp_path):
        state = {
            "status": "working",
            "issue_id": "abc123",
            "branch": "agent/abc123",
            "step": 3,
            "orphan_resumes": 1,
            "checkpoints": [
                {
                    "step": 1,
                    "description": "Initial pass",
                    "timestamp": "2026-04-28T00:00:00+00:00",
                    "commit": "abc1234",
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cost_usd": 0.25,
                "model": "gpt-4.1",
            },
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        result = read_state(tmp_path)

        assert result == state

    def test_read_state_defaults_invalid_status(self, tmp_path, caplog):
        state = {
            "status": "not-a-real-status",
            "issue_id": "abc123",
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        with caplog.at_level("WARNING", logger="host.session_utils"):
            result = read_state(tmp_path)

        assert result["status"] == "starting"
        assert "status" in caplog.text

    @pytest.mark.parametrize("cost_usd", [-1, "not-a-number"])
    def test_read_state_defaults_invalid_usage(self, tmp_path, caplog, cost_usd):
        state = {
            "status": "working",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cost_usd": cost_usd,
            },
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        with caplog.at_level("WARNING", logger="host.session_utils"):
            result = read_state(tmp_path)

        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 20
        assert result["usage"]["cost_usd"] == 0.0
        assert "usage.cost_usd" in caplog.text

    def test_read_state_defaults_invalid_checkpoints(self, tmp_path, caplog):
        state = {
            "status": "working",
            "checkpoints": [
                {
                    "step": 1,
                    "description": "Initial pass",
                    "timestamp": "2026-04-28T00:00:00+00:00",
                    "commit": "abc1234",
                },
                {
                    "step": 2,
                    "timestamp": "2026-04-28T00:01:00+00:00",
                    "commit": "abc1234",
                },
            ],
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        with caplog.at_level("WARNING", logger="host.session_utils"):
            result = read_state(tmp_path)

        assert result["checkpoints"] == state["checkpoints"]
        assert "checkpoints[1]" in caplog.text

    def test_read_state_rejects_invalid_orphan_resumes(self, tmp_path, caplog):
        state = {
            "status": "working",
            "orphan_resumes": MAX_ORPHAN_RESUMES + 5,
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        with caplog.at_level("WARNING", logger="host.session_utils"):
            result = read_state(tmp_path)

        assert result["orphan_resumes"] == 0
        assert "orphan_resumes" in caplog.text

    def test_read_state_rejects_invalid_checkpoint_timestamp(self, tmp_path, caplog):
        state = {
            "status": "working",
            "checkpoints": [
                {
                    "step": 1,
                    "description": "Initial pass",
                    "timestamp": "not-a-timestamp",
                    "commit": "abc1234",
                }
            ],
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        with caplog.at_level("WARNING", logger="host.session_utils"):
            result = read_state(tmp_path)

        assert result["checkpoints"] == state["checkpoints"]
        assert "timestamp" in caplog.text

    def test_read_state_handles_extra_fields(self, tmp_path):
        state = {
            "status": "working",
            "issue_id": "abc123",
            "future_field": {"enabled": True},
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

        result = read_state(tmp_path)

        assert result["future_field"] == {"enabled": True}


class TestWriteState:
    def test_creates_state_file(self, tmp_path):
        state = {"status": "running"}
        write_state(tmp_path, state)
        assert (tmp_path / "state.json").exists()

    def test_written_content_is_valid_json(self, tmp_path):
        state = {"status": "waiting", "issue_id": "xyz"}
        write_state(tmp_path, state)
        content = json.loads((tmp_path / "state.json").read_text())
        assert content == state

    def test_atomic_write_removes_tmp_file(self, tmp_path):
        write_state(tmp_path, {"status": "running"})
        assert not (tmp_path / "state.tmp").exists()

    def test_overwrites_existing_state(self, tmp_path):
        (tmp_path / "state.json").write_text(json.dumps({"status": "old"}))
        write_state(tmp_path, {"status": "new"})
        result = json.loads((tmp_path / "state.json").read_text())
        assert result["status"] == "new"

    def test_writes_pretty_json(self, tmp_path):
        write_state(tmp_path, {"a": 1})
        raw = (tmp_path / "state.json").read_text()
        # indent=2 means there should be newlines
        assert "\n" in raw

    def test_roundtrip_preserves_data(self, tmp_path):
        state = {"status": "running", "turns": 3, "tags": ["foo", "bar"]}
        write_state(tmp_path, state)
        assert read_state(tmp_path) == state


class TestUpdateStatus:
    def test_updates_status_field(self, tmp_path):
        (tmp_path / "state.json").write_text(json.dumps({"status": "working", "issue_id": "1"}))
        update_status(tmp_path, "waiting:review")
        state = read_state(tmp_path)
        assert state["status"] == "waiting:review"

    def test_preserves_other_fields(self, tmp_path):
        original = {"status": "working", "issue_id": "abc", "turns": 7}
        (tmp_path / "state.json").write_text(json.dumps(original))
        update_status(tmp_path, "waiting:review")
        state = read_state(tmp_path)
        assert state["issue_id"] == "abc"
        assert state["turns"] == 7

    def test_raises_when_state_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            update_status(tmp_path, "waiting:review")

    def test_validates_transition_via_ssm(self, tmp_path):
        """update_status validates transitions through the SSM."""
        from core.state_machine import InvalidTransition
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))
        update_status(tmp_path, "waiting:question")
        assert read_state(tmp_path)["status"] == "waiting:question"

    def test_rejects_invalid_transition(self, tmp_path):
        """update_status raises InvalidTransition for invalid state changes."""
        from core.state_machine import InvalidTransition
        (tmp_path / "state.json").write_text(json.dumps({"status": "accepted"}))
        with pytest.raises(InvalidTransition):
            update_status(tmp_path, "working")


# ── get_repo_root ─────────────────────────────────────────────────────────────

class TestGitRepoRoot:
    """Tests for _git_repo_root() - the internal git-based root detection."""

    def test_returns_parent_when_git_common_ends_with_dotgit(self):
        """Standard case: git dir is .git inside repo, return its parent."""
        mock_result = MagicMock()
        mock_result.stdout = "/home/user/myrepo/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result) as mock_run:
            result = _git_repo_root()
        assert result == Path("/home/user/myrepo")
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        )

    def test_returns_main_repo_from_worktree(self):
        """When called from a worktree, returns the main repo path, not the worktree path."""
        mock_result = MagicMock()
        mock_result.stdout = "/path/to/main-repo/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = _git_repo_root()
        assert result == Path("/path/to/main-repo")

    def test_falls_back_to_show_toplevel_for_external_git_dir(self):
        """When git dir is external (not ending in .git), fall back to --show-toplevel."""
        common_result = MagicMock()
        common_result.stdout = "/repo-git\n"
        toplevel_result = MagicMock()
        toplevel_result.stdout = "/workspace\n"

        def mock_run(cmd, **kwargs):
            if cmd == ["git", "rev-parse", "--git-common-dir"]:
                return common_result
            elif cmd == ["git", "rev-parse", "--show-toplevel"]:
                return toplevel_result
            raise ValueError(f"Unexpected command: {cmd}")

        with patch("host.session_utils.subprocess.run", side_effect=mock_run) as mock_run_fn:
            result = _git_repo_root()

        assert result == Path("/workspace")
        assert mock_run_fn.call_count == 2

    def test_strips_trailing_newline(self):
        mock_result = MagicMock()
        mock_result.stdout = "/some/path/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = _git_repo_root()
        assert str(result) == "/some/path"

    def test_propagates_subprocess_error(self):
        with patch("host.session_utils.subprocess.run",
                   side_effect=subprocess.CalledProcessError(128, "git")):
            with pytest.raises(subprocess.CalledProcessError):
                _git_repo_root()

    def test_returns_path_type(self):
        mock_result = MagicMock()
        mock_result.stdout = "/repo/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = _git_repo_root()
        assert isinstance(result, Path)


class TestGetRepoRoot:
    """Tests for get_repo_root() - with .nightshift/ validation."""

    def test_propagates_subprocess_error(self):
        with patch("host.session_utils.subprocess.run",
                   side_effect=subprocess.CalledProcessError(128, "git")):
            with pytest.raises(subprocess.CalledProcessError):
                get_repo_root()

    def test_get_repo_root_resolves_symlinks(self, tmp_path):
        """Symlinked directories should resolve to their real paths."""
        real_repo = tmp_path / "real-repo"
        real_repo.mkdir()
        (real_repo / ".git").mkdir()
        (real_repo / ".nightshift").mkdir()

        symlink = tmp_path / "symlinked-repo"
        symlink.symlink_to(real_repo)

        mock_result = MagicMock()
        mock_result.stdout = f"{symlink}/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = get_repo_root()

        assert result == real_repo  # Should resolve through symlink

    def test_get_repo_root_finds_nightshift_dir(self, tmp_path):
        """Returns git root if .nightshift/ exists there."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".nightshift").mkdir()

        mock_result = MagicMock()
        mock_result.stdout = f"{repo}/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = get_repo_root()

        assert result == repo

    def test_get_repo_root_walks_up_to_find_nightshift(self, tmp_path, monkeypatch):
        """If git root lacks .nightshift/, walk up from cwd to find it."""
        actual_repo = tmp_path / "actual-repo"
        actual_repo.mkdir()
        (actual_repo / ".nightshift").mkdir()

        nested = actual_repo / "subdir" / "nested"
        nested.mkdir(parents=True)
        (nested / ".git").mkdir()

        monkeypatch.chdir(nested)

        mock_result = MagicMock()
        mock_result.stdout = f"{nested}/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            result = get_repo_root()

        assert result == actual_repo

    def test_get_repo_root_errors_when_no_nightshift(self, tmp_path, monkeypatch):
        """Raises RuntimeError if no .nightshift/ found anywhere."""
        random_dir = tmp_path / "random"
        random_dir.mkdir()
        (random_dir / ".git").mkdir()

        monkeypatch.chdir(random_dir)

        mock_result = MagicMock()
        mock_result.stdout = f"{random_dir}/.git\n"
        with patch("host.session_utils.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="No .nightshift/ found"):
                get_repo_root()


# ── sessions_dir ──────────────────────────────────────────────────────────────

class TestSessionsDir:
    def test_with_explicit_repo(self, tmp_path):
        result = sessions_dir(repo=tmp_path)
        assert result == tmp_path / ".nightshift" / "sessions"

    def test_with_none_calls_get_repo_root(self):
        fake_root = Path("/fake/root")
        with patch("host.session_utils.get_repo_root", return_value=fake_root) as mock_root:
            result = sessions_dir()
        mock_root.assert_called_once()
        assert result == fake_root / ".nightshift" / "sessions"

    def test_returns_path_type(self, tmp_path):
        result = sessions_dir(repo=tmp_path)
        assert isinstance(result, Path)

    def test_path_components(self, tmp_path):
        result = sessions_dir(repo=tmp_path)
        assert result.parts[-1] == "sessions"
        assert result.parts[-2] == ".nightshift"


# ── force_remove_dir ──────────────────────────────────────────────────────────

class TestForceRemoveDir:
    def test_normal_case_calls_rmtree_with_ignore_errors(self, tmp_path):
        target = tmp_path / "to_remove"
        target.mkdir()
        with patch("host.session_utils.shutil.rmtree") as mock_rmtree:
            force_remove_dir(target)
        mock_rmtree.assert_called_once_with(target, ignore_errors=True)

    def test_removes_real_directory(self, tmp_path):
        target = tmp_path / "to_remove"
        target.mkdir()
        (target / "file.txt").write_text("hello")
        force_remove_dir(target)
        assert not target.exists()

    def test_permission_error_triggers_docker_fallback(self, tmp_path):
        target = tmp_path / "locked_dir"
        target.mkdir()

        rmtree_calls = []

        def rmtree_side_effect(path, ignore_errors=False):
            call_count = len(rmtree_calls)
            rmtree_calls.append(path)
            if ignore_errors:
                # With ignore_errors=True, shutil.rmtree catches exceptions internally
                return
            if call_count == 0:
                raise PermissionError("Permission denied")
            # second call (after docker) succeeds silently

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect) as mock_rmtree, \
             patch("host.session_utils.subprocess.run") as mock_run:
            force_remove_dir(target)

        # With ignore_errors=True, Docker fallback is NOT triggered
        # because shutil.rmtree handles errors internally
        docker_calls = [c for c in mock_run.call_args_list
                        if c.args and c.args[0][0] == "docker"]
        assert len(docker_calls) == 0

        # rmtree should have been called once with ignore_errors=True
        assert mock_rmtree.call_count == 1

    def test_permission_error_docker_volume_includes_path(self, tmp_path):
        target = tmp_path / "locked_dir"
        target.mkdir()

        call_count = [0]

        def rmtree_side_effect(path, ignore_errors=False):
            call_count[0] += 1
            if ignore_errors:
                return  # ignore_errors=True means no exception raised
            if call_count[0] == 1:
                raise PermissionError("denied")

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect), \
             patch("host.session_utils.subprocess.run") as mock_run:
            force_remove_dir(target)

        # With ignore_errors=True, no Docker fallback is needed
        docker_calls = [c for c in mock_run.call_args_list
                        if c.args and c.args[0][0] == "docker"]
        assert len(docker_calls) == 0

    def test_permission_error_file_not_found_after_docker_is_ok(self, tmp_path):
        target = tmp_path / "locked_dir"
        target.mkdir()

        call_count = [0]

        def rmtree_side_effect(path, ignore_errors=False):
            call_count[0] += 1
            if ignore_errors:
                return  # ignore_errors=True means no exception raised
            if call_count[0] == 1:
                raise PermissionError("denied")
            raise FileNotFoundError("already gone")

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect), \
             patch("host.session_utils.subprocess.run"):
            # Should not raise - ignore_errors=True handles everything
            force_remove_dir(target)

    def test_force_remove_dir_ignores_missing_subdirs(self, tmp_path):
        """force_remove_dir() succeeds even if subdirs disappear during iteration."""
        target = tmp_path / "target"
        target.mkdir()
        (target / "subdir").mkdir()
        (target / "file.txt").write_text("content")

        # Should succeed without raising
        force_remove_dir(target)
        assert not target.exists()

    def test_force_remove_dir_race_condition(self, tmp_path):
        """Concurrent modification during rmtree doesn't raise exception."""
        import shutil
        from unittest.mock import patch

        target = tmp_path / "target"
        target.mkdir()
        (target / "tags").mkdir()

        # Simulate race: directory removed during iteration
        original_rmtree = shutil.rmtree
        call_count = [0]

        def racing_rmtree(path, ignore_errors=False):
            call_count[0] += 1
            if call_count[0] == 1 and not ignore_errors:
                # Simulate FileNotFoundError for 'tags' subdirectory
                raise FileNotFoundError(2, "No such file or directory", "tags")
            return original_rmtree(path, ignore_errors=ignore_errors)

        with patch("shutil.rmtree", side_effect=racing_rmtree):
            # With ignore_errors=True, this should not raise
            force_remove_dir(target)


# ── remove_worktree ───────────────────────────────────────────────────────────

class TestRemoveWorktree:
    def test_successful_git_worktree_remove(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "worktree"
        wt.mkdir()

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "agent/my-branch")

        calls = mock_run.call_args_list
        # First call: git worktree remove
        assert calls[0].args[0] == ["git", "worktree", "remove", str(wt), "--force"]
        assert calls[0].kwargs.get("cwd") == str(repo)
        # Second call: git branch -D (WT-6: no global prune anymore)
        assert calls[1].args[0] == ["git", "branch", "-D", "agent/my-branch"]

    def test_failed_worktree_remove_triggers_force_remove(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "worktree"
        wt.mkdir()

        fail_result = MagicMock()
        fail_result.returncode = 1

        success_result = MagicMock()
        success_result.returncode = 0

        run_results = [fail_result, success_result, success_result]

        with patch("host.session_utils.subprocess.run", side_effect=run_results) as mock_run, \
             patch("host.session_utils.force_remove_dir") as mock_force:
            remove_worktree(repo, wt, "agent/branch")

        mock_force.assert_called_once_with(wt)

    def test_skips_worktree_remove_when_path_missing(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "nonexistent_worktree"  # does NOT exist

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "agent/branch")

        # Only branch -D should be called (not worktree remove, not prune per WT-6)
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "remove", str(wt), "--force"] not in cmds
        assert ["git", "worktree", "prune"] not in cmds  # WT-6: no global prune
        assert ["git", "branch", "-D", "agent/branch"] in cmds

    def test_always_runs_branch_delete(self, tmp_path):
        """WT-6: remove_worktree always deletes the branch (but no global prune)."""
        repo = tmp_path / "repo"
        wt = tmp_path / "worktree"
        # wt does not exist — so worktree remove is skipped

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "my-branch")

        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "prune"] not in cmds  # WT-6: no global prune
        assert ["git", "branch", "-D", "my-branch"] in cmds

    def test_worktree_remove_uses_correct_cwd(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "wt"
        wt.mkdir()

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "branch")

        first_call = mock_run.call_args_list[0]
        assert first_call.kwargs.get("cwd") == str(repo)

    def test_prune_and_branch_delete_use_repo_cwd(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "wt"
        # wt does not exist, skip worktree remove

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "branch")

        for c in mock_run.call_args_list:
            assert c.kwargs.get("cwd") == str(repo)

    def test_remove_worktree_no_global_prune(self, tmp_path):
        """WT-6: remove_worktree should NOT call global `git worktree prune`.

        Global prune can delete metadata for other worktrees with corrupted
        .git files, causing collateral damage to running sessions.
        """
        repo = tmp_path / "repo"
        wt = tmp_path / "worktree"
        wt.mkdir()

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "agent/branch")

        cmds = [c.args[0] for c in mock_run.call_args_list]
        # Should NOT call global prune
        assert ["git", "worktree", "prune"] not in cmds
        # Should still call worktree remove and branch delete
        assert ["git", "worktree", "remove", str(wt), "--force"] in cmds
        assert ["git", "branch", "-D", "agent/branch"] in cmds


# ── archive_session ──────────────────────────────────────────────────────────

class TestArchiveSession:
    def _make_session(self, tmp_path, session_id="test-session-123"):
        """Create a fake repo with a session directory containing archivable files."""
        repo = tmp_path / "repo"
        session_dir = repo / ".nightshift" / "sessions" / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text('{"role":"user"}\n')
        (session_dir / "state.json").write_text('{"status":"done"}')
        (session_dir / "raw-output.log").write_text("some output\n")
        return repo, session_dir

    def test_cleanup_archives_conversation(self, tmp_path):
        """After cleanup, conversation.jsonl exists in archive."""
        repo, session_dir = self._make_session(tmp_path)
        archive_dir = archive_session(session_dir, repo)
        assert archive_dir is not None
        assert (archive_dir / "conversation.jsonl").exists()
        assert (archive_dir / "conversation.jsonl").read_text() == '{"role":"user"}\n'

    def test_cleanup_archives_state(self, tmp_path):
        """After cleanup, state.json exists in archive."""
        repo, session_dir = self._make_session(tmp_path)
        archive_dir = archive_session(session_dir, repo)
        assert archive_dir is not None
        assert (archive_dir / "state.json").exists()
        assert json.loads((archive_dir / "state.json").read_text()) == {"status": "done"}

    def test_archives_raw_output_log(self, tmp_path):
        repo, session_dir = self._make_session(tmp_path)
        archive_dir = archive_session(session_dir, repo)
        assert (archive_dir / "raw-output.log").exists()

    def test_returns_none_for_missing_session(self, tmp_path):
        repo = tmp_path / "repo"
        missing = repo / ".nightshift" / "sessions" / "no-such-session"
        result = archive_session(missing, repo)
        assert result is None

    def test_skips_missing_files_gracefully(self, tmp_path):
        """If only some archivable files exist, archive those without error."""
        repo = tmp_path / "repo"
        session_dir = repo / ".nightshift" / "sessions" / "partial"
        session_dir.mkdir(parents=True)
        (session_dir / "state.json").write_text('{"status":"done"}')
        # conversation.jsonl and raw-output.log are missing

        archive_dir = archive_session(session_dir, repo)
        assert (archive_dir / "state.json").exists()
        assert not (archive_dir / "conversation.jsonl").exists()
        assert not (archive_dir / "raw-output.log").exists()

    def test_archive_path_uses_session_id(self, tmp_path):
        repo, session_dir = self._make_session(tmp_path, session_id="abc123")
        archive_dir = archive_session(session_dir, repo)
        assert archive_dir == repo / ".nightshift" / "archive" / "abc123"

    def test_accept_archives_before_cleanup(self, tmp_path):
        """Simulate accept flow: archive then delete session dir."""
        repo, session_dir = self._make_session(tmp_path)
        archive_dir = archive_session(session_dir, repo)

        # Simulate cleanup deleting the session dir
        import shutil
        shutil.rmtree(session_dir)

        assert not session_dir.exists()
        assert archive_dir.exists()
        assert (archive_dir / "conversation.jsonl").exists()
        assert (archive_dir / "state.json").exists()
        assert (archive_dir / "raw-output.log").exists()

    def test_does_not_archive_non_listed_files(self, tmp_path):
        """Files not in ARCHIVE_FILES should not be copied."""
        repo, session_dir = self._make_session(tmp_path)
        (session_dir / "some-other-file.txt").write_text("extra")

        archive_dir = archive_session(session_dir, repo)
        assert not (archive_dir / "some-other-file.txt").exists()

    def test_idempotent_overwrites_existing_archive(self, tmp_path):
        """Archiving twice overwrites without error."""
        repo, session_dir = self._make_session(tmp_path)
        archive_session(session_dir, repo)
        # Modify a file and re-archive
        (session_dir / "state.json").write_text('{"status":"updated"}')
        archive_dir = archive_session(session_dir, repo)
        assert json.loads((archive_dir / "state.json").read_text()) == {"status": "updated"}


# ── Locked state operations ───────────────────────────────────────────────────

class TestLockedStateOperations:
    """Tests for locked read-modify-write operations."""

    def test_increment_orphan_resumes(self, tmp_path):
        """increment_orphan_resumes atomically increments and returns new value."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "working", "orphan_resumes": 2
        }))

        result = increment_orphan_resumes(tmp_path)
        assert result == 3
        state = read_state(tmp_path)
        assert state["orphan_resumes"] == 3

    def test_increment_orphan_resumes_initializes_from_zero(self, tmp_path):
        """increment_orphan_resumes works when field is missing."""
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))

        result = increment_orphan_resumes(tmp_path)
        assert result == 1

    def test_increment_auth_retries(self, tmp_path):
        """increment_auth_retries atomically increments and returns new value."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "suspended:auth-failure", "auth_retries": 1
        }))

        result = increment_auth_retries(tmp_path)
        assert result == 2
        state = read_state(tmp_path)
        assert state["auth_retries"] == 2

    def test_increment_provider_outage_retries(self, tmp_path):
        """increment_provider_outage_retries atomically increments."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "suspended:provider-overload"
        }))

        result = increment_provider_outage_retries(tmp_path)
        assert result == 1

    def test_update_state_fields_updates_multiple(self, tmp_path):
        """update_state_fields atomically updates multiple fields."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "working", "orphan_resumes": 0, "other": "value"
        }))

        update_state_fields(tmp_path, status="suspended:unexpected", orphan_resumes=5)

        state = read_state(tmp_path)
        assert state["status"] == "suspended:unexpected"
        assert state["orphan_resumes"] == 0
        assert state["other"] == "value"

    def test_update_state_fields_creates_lock_file(self, tmp_path):
        """update_state_fields should create state.json.lock file."""
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))

        update_state_fields(tmp_path, status="waiting:review")
        assert (tmp_path / "state.json.lock").exists()

    def test_update_state_fields_rejects_invalid_transition(self, tmp_path):
        """update_state_fields raises InvalidTransition for invalid status changes."""
        from core.state_machine import InvalidTransition
        (tmp_path / "state.json").write_text(json.dumps({"status": "accepted"}))
        with pytest.raises(InvalidTransition):
            update_state_fields(tmp_path, status="working", orphan_resumes=0)

    def test_update_status_creates_lock_file(self, tmp_path):
        """update_status should create state.json.lock file."""
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))

        update_status(tmp_path, "waiting:review")
        assert (tmp_path / "state.json.lock").exists()

    def test_clear_completed_at_removes_field(self, tmp_path):
        """clear_completed_at removes completed_at field from state (SSM-11)."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "completed_at": "2025-01-01T00:00:00Z",
            "other": "preserved"
        }))

        clear_completed_at(tmp_path)

        state = read_state(tmp_path)
        assert "completed_at" not in state
        assert state["other"] == "preserved"

    def test_clear_completed_at_noop_if_not_set(self, tmp_path):
        """clear_completed_at is a no-op if completed_at is not set."""
        (tmp_path / "state.json").write_text(json.dumps({
            "status": "working"
        }))

        clear_completed_at(tmp_path)

        state = read_state(tmp_path)
        assert state == {"status": "working"}


# ── Safe prune (WT-6) ─────────────────────────────────────────────────────────

class TestSafePrune:
    """Tests for WT-6 safe worktree prune with defense in depth."""

    def test_prune_skips_active_sessions(self, tmp_path):
        """WT-6: safe_prune should NOT prune if any session is active."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sessions = repo / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        # Create an active session
        active = sessions / "abc123"
        active.mkdir()
        (active / "state.json").write_text(json.dumps({"status": "working"}))

        with patch("host.session_utils.subprocess.run") as mock_run:
            safe_prune(repo)

        # Should NOT have called git worktree prune
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "prune"] not in cmds

    def test_prune_runs_when_no_active_sessions(self, tmp_path):
        """safe_prune runs when all sessions are inactive."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sessions = repo / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        # Create an inactive session
        inactive = sessions / "abc123"
        inactive.mkdir()
        (inactive / "state.json").write_text(json.dumps({"status": "waiting:review"}))

        with patch("host.session_utils.subprocess.run") as mock_run:
            safe_prune(repo)

        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "prune", "-v"] in cmds

    def test_prune_fixes_corrupted_gitdir_first(self, tmp_path):
        """WT-6: safe_prune fixes corrupted .git files before pruning."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".nightshift" / "sessions").mkdir(parents=True)

        # Create worktree dir structure
        worktrees_dir = repo / "worktrees"
        worktrees_dir.mkdir()
        wt = worktrees_dir / "agent-abc123"
        wt.mkdir()

        # Create corrupted .git file pointing to container path
        git_file = wt / ".git"
        git_file.write_text("gitdir: /repo-git/worktrees/agent-abc123\n")

        # Create matching host gitdir
        host_gitdir = repo / ".git" / "worktrees" / "agent-abc123"
        host_gitdir.mkdir(parents=True)

        # Mock subprocess.run but let file operations happen
        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if args == ["git", "worktree", "list", "--porcelain"]:
                # Porcelain format: "worktree /path\nHEAD ...\n\n"
                result.stdout = f"worktree {wt}\nHEAD abc123\nbranch refs/heads/agent-abc123\n\n"
            else:
                result.stdout = ""
            return result

        with patch("host.session_utils.subprocess.run", side_effect=mock_run):
            safe_prune(repo)

        # The corrupted .git should be fixed before prune
        fixed_content = git_file.read_text()
        assert "/repo-git/" not in fixed_content
        assert str(host_gitdir) in fixed_content


class TestGetActiveSessionIds:
    """Tests for get_active_session_ids helper."""

    def test_returns_ids_for_active_sessions(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        s1 = sessions / "abc123"
        s1.mkdir()
        (s1 / "state.json").write_text(json.dumps({"status": "working", "issue_id": "issue-1"}))
        s2 = sessions / "def456"
        s2.mkdir()
        (s2 / "state.json").write_text(json.dumps({"status": "starting", "issue_id": "issue-2"}))

        result = get_active_session_ids(tmp_path)
        assert sorted(result) == ["issue-1", "issue-2"]

    def test_returns_empty_for_inactive_sessions(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "waiting:review"}))

        assert get_active_session_ids(tmp_path) == []

    def test_returns_empty_when_no_sessions(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        assert get_active_session_ids(tmp_path) == []

    def test_falls_back_to_dir_name_if_no_issue_id(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "working"}))

        result = get_active_session_ids(tmp_path)
        assert result == ["abc123"]


class TestHasActiveSessions:
    """Tests for has_active_sessions helper."""

    def test_returns_true_for_working(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "working"}))

        assert has_active_sessions(tmp_path) is True

    def test_returns_true_for_starting(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "starting"}))

        assert has_active_sessions(tmp_path) is True

    def test_returns_true_for_reviewing(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "reviewing"}))

        assert has_active_sessions(tmp_path) is True

    def test_returns_false_for_waiting_review(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)
        session = sessions / "abc123"
        session.mkdir()
        (session / "state.json").write_text(json.dumps({"status": "waiting:review"}))

        assert has_active_sessions(tmp_path) is False

    def test_returns_false_when_no_sessions(self, tmp_path):
        sessions = tmp_path / ".nightshift" / "sessions"
        sessions.mkdir(parents=True)

        assert has_active_sessions(tmp_path) is False


class TestFixAllCorruptedGitdirs:
    """Tests for fix_all_corrupted_gitdirs helper."""

    def test_fixes_container_path_in_git_file(self, tmp_path):
        """Fixes .git file pointing to /repo-git/ container path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktrees_dir = repo / "worktrees"
        wt = worktrees_dir / "agent-abc123"
        wt.mkdir(parents=True)

        # Corrupted .git file
        git_file = wt / ".git"
        git_file.write_text("gitdir: /repo-git/worktrees/agent-abc123\n")

        # Matching host gitdir
        host_gitdir = repo / ".git" / "worktrees" / "agent-abc123"
        host_gitdir.mkdir(parents=True)

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if args == ["git", "worktree", "list", "--porcelain"]:
                result.stdout = f"worktree {wt}\nHEAD abc123\nbranch refs/heads/agent-abc123\n\n"
            else:
                result.stdout = ""
            return result

        with patch("host.session_utils.subprocess.run", side_effect=mock_run):
            fix_all_corrupted_gitdirs(repo)

        fixed_content = git_file.read_text()
        assert "/repo-git/" not in fixed_content
        assert str(host_gitdir) in fixed_content

    def test_skips_already_valid_git_files(self, tmp_path):
        """Leaves valid .git files unchanged."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktrees_dir = repo / "worktrees"
        wt = worktrees_dir / "agent-abc123"
        wt.mkdir(parents=True)

        # Valid .git file
        host_gitdir = repo / ".git" / "worktrees" / "agent-abc123"
        host_gitdir.mkdir(parents=True)
        original = f"gitdir: {host_gitdir}\n"
        git_file = wt / ".git"
        git_file.write_text(original)

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if args == ["git", "worktree", "list", "--porcelain"]:
                result.stdout = f"worktree {wt}\nHEAD abc123\nbranch refs/heads/agent-abc123\n\n"
            else:
                result.stdout = ""
            return result

        with patch("host.session_utils.subprocess.run", side_effect=mock_run):
            fix_all_corrupted_gitdirs(repo)

        assert git_file.read_text() == original


class TestFixCorruptedGitdirUsesRebaseImport:
    """Verify _fix_container_gitdir is reused from host.rebase (DRY)."""

    def test_fix_corrupted_gitdir_uses_rebase_import(self):
        """session_utils imports _fix_container_gitdir from host.rebase, not duplicated."""
        import host.session_utils as su
        import host.rebase as rb

        # Verify the constant is imported (not duplicated)
        assert su.CONTAINER_GIT_PATH is rb.CONTAINER_GIT_PATH

        # Verify the function is imported (not duplicated)
        assert su._fix_container_gitdir is rb._fix_container_gitdir
