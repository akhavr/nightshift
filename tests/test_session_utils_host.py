"""Tests for host/session_utils.py"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.session_utils import (
    ARCHIVE_FILES,
    _git_repo_root,
    archive_session,
    clear_completed_at,
    force_remove_dir,
    get_repo_root,
    increment_orphan_resumes,
    increment_auth_retries,
    increment_provider_outage_retries,
    read_state,
    remove_worktree,
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
        state = {"status": "done", "metadata": {"turns": 5, "checkpoints": ["a", "b"]}}
        (tmp_path / "state.json").write_text(json.dumps(state))
        assert read_state(tmp_path) == state


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
        (tmp_path / "state.json").write_text(json.dumps({"status": "running", "issue_id": "1"}))
        update_status(tmp_path, "done")
        state = read_state(tmp_path)
        assert state["status"] == "done"

    def test_preserves_other_fields(self, tmp_path):
        original = {"status": "running", "issue_id": "abc", "turns": 7}
        (tmp_path / "state.json").write_text(json.dumps(original))
        update_status(tmp_path, "waiting:review")
        state = read_state(tmp_path)
        assert state["issue_id"] == "abc"
        assert state["turns"] == 7

    def test_raises_when_state_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            update_status(tmp_path, "done")

    def test_can_set_arbitrary_status_string(self, tmp_path):
        (tmp_path / "state.json").write_text(json.dumps({"status": "running"}))
        update_status(tmp_path, "waiting:question")
        assert read_state(tmp_path)["status"] == "waiting:question"


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
    def test_normal_case_calls_rmtree(self, tmp_path):
        target = tmp_path / "to_remove"
        target.mkdir()
        with patch("host.session_utils.shutil.rmtree") as mock_rmtree:
            force_remove_dir(target)
        mock_rmtree.assert_called_once_with(target)

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

        def rmtree_side_effect(path):
            call_count = len(rmtree_calls)
            rmtree_calls.append(path)
            if call_count == 0:
                raise PermissionError("Permission denied")
            # second call (after docker) succeeds silently

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect) as mock_rmtree, \
             patch("host.session_utils.subprocess.run") as mock_run:
            force_remove_dir(target)

        # Docker should have been called
        docker_calls = [c for c in mock_run.call_args_list
                        if c.args[0][0] == "docker"]
        assert len(docker_calls) == 1
        docker_cmd = docker_calls[0].args[0]
        assert "ubuntu:24.04" in docker_cmd
        assert "rm" in docker_cmd
        assert "-rf" in docker_cmd

        # rmtree should have been called twice
        assert mock_rmtree.call_count == 2

    def test_permission_error_docker_volume_includes_path(self, tmp_path):
        target = tmp_path / "locked_dir"
        target.mkdir()

        call_count = [0]

        def rmtree_side_effect(path):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("denied")

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect), \
             patch("host.session_utils.subprocess.run") as mock_run:
            force_remove_dir(target)

        docker_call = next(c for c in mock_run.call_args_list
                           if c.args[0][0] == "docker")
        cmd = docker_call.args[0]
        assert any(str(target) in part for part in cmd)

    def test_permission_error_file_not_found_after_docker_is_ok(self, tmp_path):
        target = tmp_path / "locked_dir"
        target.mkdir()

        call_count = [0]

        def rmtree_side_effect(path):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("denied")
            raise FileNotFoundError("already gone")

        with patch("host.session_utils.shutil.rmtree", side_effect=rmtree_side_effect), \
             patch("host.session_utils.subprocess.run"):
            # Should not raise
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
        # Second call: git worktree prune
        assert calls[1].args[0] == ["git", "worktree", "prune"]
        # Third call: git branch -D
        assert calls[2].args[0] == ["git", "branch", "-D", "agent/my-branch"]

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

        # Only prune and branch -D should be called (not worktree remove)
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "remove", str(wt), "--force"] not in cmds
        assert ["git", "worktree", "prune"] in cmds
        assert ["git", "branch", "-D", "agent/branch"] in cmds

    def test_always_runs_prune_and_branch_delete(self, tmp_path):
        repo = tmp_path / "repo"
        wt = tmp_path / "worktree"
        # wt does not exist — so worktree remove is skipped

        success_result = MagicMock()
        success_result.returncode = 0

        with patch("host.session_utils.subprocess.run", return_value=success_result) as mock_run:
            remove_worktree(repo, wt, "my-branch")

        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "worktree", "prune"] in cmds
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

        update_state_fields(tmp_path, status="suspended", orphan_resumes=5)

        state = read_state(tmp_path)
        assert state["status"] == "suspended"
        assert state["orphan_resumes"] == 5
        assert state["other"] == "value"

    def test_update_state_fields_creates_lock_file(self, tmp_path):
        """update_state_fields should create state.json.lock file."""
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))

        update_state_fields(tmp_path, status="done")
        assert (tmp_path / "state.json.lock").exists()

    def test_update_status_creates_lock_file(self, tmp_path):
        """update_status should create state.json.lock file."""
        (tmp_path / "state.json").write_text(json.dumps({"status": "working"}))

        update_status(tmp_path, "done")
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
