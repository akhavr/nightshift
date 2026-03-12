"""Tests for host/launch.py helper functions."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.models import WorkflowConfig, AgentConfig, WorkspaceConfig
from core.protocols import TrackerIssue
from host.launch import _create_worktree, _dump_issue_data, _build_docker_cmd


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    """Fake repo root with a .git dir."""
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def config():
    """Minimal WorkflowConfig for tests."""
    return WorkflowConfig(
        agent=AgentConfig(max_turns=30),
        workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
    )


@pytest.fixture
def sample_issue():
    return TrackerIssue(
        id="abc123def456",
        identifier="abc123def456",
        title="Fix the widget",
        body="The widget is broken.",
        status="open",
        labels=["bug"],
    )


# ── _create_worktree tests ───────────────────────────────

class TestCreateWorktree:

    @patch("host.launch.subprocess.run")
    def test_creates_worktree_and_writes_state(self, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"
        branch = "agent/abc123"
        base_branch = "master"
        issue_id = "abc123def456"

        # Simulate subprocess calls:
        # 1. git worktree prune  -> ok
        # 2. git branch          -> ok
        # 3. git worktree add    -> ok (creates the wt dir with a file)
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="")
            if cmd[1] == "worktree" and cmd[2] == "add":
                # simulate worktree directory being created with content
                wt_path.mkdir(exist_ok=True)
                (wt_path / "README.md").write_text("hello")
                (wt_path / ".git").write_text("gitdir: ...")
            return result

        mock_run.side_effect = side_effect

        _create_worktree(repo, wt_path, branch, base_branch, session_dir, issue_id)

        # Session dir created
        assert session_dir.exists()

        # state.json written
        state = json.loads((session_dir / "state.json").read_text())
        assert state["issue_id"] == issue_id
        assert state["branch"] == branch
        assert state["status"] == "starting"
        assert state["step"] == 0
        assert "started_at" in state
        assert state["checkpoints"] == []
        assert state["human_answers"] == []

        # Correct git commands were called
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[0][0][0] == ["git", "worktree", "prune"]
        assert mock_run.call_args_list[1][0][0] == ["git", "branch", branch, base_branch]
        assert mock_run.call_args_list[2][0][0] == ["git", "worktree", "add", str(wt_path), branch]

    @patch("host.launch.subprocess.run")
    def test_exits_on_worktree_failure(self, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"

        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        # Third call (worktree add) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # prune
            MagicMock(returncode=0),  # branch
            MagicMock(returncode=1, stderr="fatal: already exists"),  # worktree add
        ]

        with pytest.raises(SystemExit) as exc_info:
            _create_worktree(repo, wt_path, "agent/x", "master", session_dir, "x")
        assert exc_info.value.code == 1

    @patch("host.launch.subprocess.run")
    def test_exits_on_empty_worktree(self, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"

        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="")
            if cmd[1] == "worktree" and cmd[2] == "add":
                # Create worktree dir with only .git (counts as empty)
                wt_path.mkdir(exist_ok=True)
                (wt_path / ".git").write_text("gitdir: ...")
            return result

        mock_run.side_effect = side_effect

        with pytest.raises(SystemExit) as exc_info:
            _create_worktree(repo, wt_path, "agent/x", "master", session_dir, "x")
        assert exc_info.value.code == 1

    @patch("host.launch.force_remove_dir")
    @patch("host.launch.subprocess.run")
    def test_removes_existing_worktree_dir(self, mock_run, mock_force_rm, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()  # pre-existing
        session_dir = tmp_path / "session"

        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="")
            if cmd[1] == "worktree" and cmd[2] == "add":
                wt_path.mkdir(exist_ok=True)
                (wt_path / "file.py").write_text("code")
                (wt_path / ".git").write_text("gitdir: ...")
            return result

        mock_run.side_effect = side_effect

        _create_worktree(repo, wt_path, "agent/x", "master", session_dir, "issue1")

        mock_force_rm.assert_called_once_with(wt_path)

    @patch("host.launch.subprocess.run")
    def test_copies_gitignore_if_exists(self, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".gitignore").write_text("*.pyc\n__pycache__/\n")
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"

        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="")
            if cmd[1] == "worktree" and cmd[2] == "add":
                wt_path.mkdir(exist_ok=True)
                (wt_path / "file.py").write_text("code")
                (wt_path / ".git").write_text("gitdir: ...")
            return result

        mock_run.side_effect = side_effect

        _create_worktree(repo, wt_path, "agent/x", "master", session_dir, "issue1")

        assert (wt_path / ".gitignore").exists()
        assert (wt_path / ".gitignore").read_text() == "*.pyc\n__pycache__/\n"


# ── _dump_issue_data tests ───────────────────────────────

class TestDumpIssueData:

    @patch("host.launch.create_tracker")
    def test_dumps_issue_and_all_issues(self, mock_create_tracker, tmp_path, config, sample_issue):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        repo = tmp_path / "repo"

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = sample_issue
        mock_tracker.list_issues.return_value = [sample_issue]
        mock_create_tracker.return_value = mock_tracker

        _dump_issue_data(config, repo, session_dir, "abc123def456",
                         is_review=False, is_resume=False)

        # issue.json written
        issue_data = json.loads((session_dir / "issue.json").read_text())
        assert issue_data["id"] == "abc123def456"
        assert issue_data["title"] == "Fix the widget"

        # issues.json written
        all_data = json.loads((session_dir / "issues.json").read_text())
        assert len(all_data) == 1
        assert all_data[0]["id"] == "abc123def456"

    @patch("host.launch.create_tracker")
    def test_skips_dump_for_review_with_existing_issue(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "issue.json").write_text('{"id": "existing"}')

        _dump_issue_data(config, tmp_path, session_dir, "abc123",
                         is_review=True, is_resume=False)

        # Tracker should not even be created
        mock_create_tracker.assert_not_called()

    @patch("host.launch.create_tracker")
    def test_exits_when_issue_not_found_and_no_cache(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        with pytest.raises(SystemExit) as exc_info:
            _dump_issue_data(config, tmp_path, session_dir, "missing",
                             is_review=False, is_resume=False)
        assert exc_info.value.code == 1

    @patch("host.launch.create_tracker")
    def test_reuses_cache_on_resume_when_tracker_fails(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "issue.json").write_text('{"id": "cached"}')

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        # Should not exit -- reuses cached data
        _dump_issue_data(config, tmp_path, session_dir, "abc",
                         is_review=False, is_resume=True)

        # issue.json should remain untouched
        assert json.loads((session_dir / "issue.json").read_text())["id"] == "cached"

    @patch("host.launch.create_tracker")
    def test_exits_on_resume_without_cache(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        # No issue.json exists

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        with pytest.raises(SystemExit):
            _dump_issue_data(config, tmp_path, session_dir, "abc",
                             is_review=False, is_resume=True)


# ── _build_docker_cmd tests ──────────────────────────────

class TestBuildDockerCmd:

    def _call(self, repo=None, workspace_mount="/ws", session_dir=None,
              container_name="nightshift-abc", worktree_name="agent-abc",
              issue_id="abc123def456", short_id="abc123def456",
              max_turns=30, step="coder", is_resume=False,
              workflow_path="/repo/WORKFLOW.md", image="nightshift:latest",
              **env_overrides):
        if repo is None:
            repo = Path("/fake/repo")
        if session_dir is None:
            session_dir = Path("/fake/session")

        # Clear env vars that affect the output
        env_to_clear = [
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_WEBHOOK_URL",
            "SLACK_WEBHOOK", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
        ]
        saved = {}
        for var in env_to_clear:
            if var in os.environ:
                saved[var] = os.environ.pop(var)

        # Set overrides
        for k, v in env_overrides.items():
            os.environ[k] = v

        try:
            with patch("host.launch.sys") as mock_sys:
                mock_sys.stdin.isatty.return_value = False
                return _build_docker_cmd(
                    repo, workspace_mount, session_dir, container_name,
                    worktree_name, issue_id, short_id, max_turns,
                    step, is_resume, workflow_path, image,
                )
        finally:
            # Restore env
            for k, v in saved.items():
                os.environ[k] = v
            for k in env_overrides:
                os.environ.pop(k, None)

    def test_basic_command_structure(self):
        cmd = self._call()

        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--rm" in cmd
        assert cmd[-1] == "nightshift:latest"

    def test_container_name(self):
        cmd = self._call(container_name="nightshift-test123")
        idx = cmd.index("--name")
        assert cmd[idx + 1] == "nightshift-test123"

    def test_volume_mounts(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        cmd = self._call(repo=repo, workspace_mount="/ws", session_dir=session_dir,
                         workflow_path=str(tmp_path / "WORKFLOW.md"))

        cmd_str = " ".join(cmd)
        assert "/ws:/workspace:rw" in cmd_str
        assert f"{session_dir}:/session:rw" in cmd_str
        assert f"{repo / '.git'}:/repo-git:rw" in cmd_str

    def test_env_vars_set(self):
        cmd = self._call(issue_id="issue-42", short_id="issue-42",
                         worktree_name="agent-42", max_turns=25, step="coder")

        assert "ISSUE_ID=issue-42" in " ".join(cmd)
        assert "SHORT_ID=issue-42" in " ".join(cmd)
        assert "WORKTREE_NAME=agent-42" in " ".join(cmd)
        assert "MAX_TURNS=25" in " ".join(cmd)
        assert "STEP=coder" in " ".join(cmd)

    def test_resume_flag_passed(self):
        cmd = self._call(is_resume=True)
        assert "RESUME=--resume" in " ".join(cmd)

    def test_resume_flag_not_set(self):
        cmd = self._call(is_resume=False)
        assert "RESUME=" in " ".join(cmd)

    def test_no_tty_flags_when_not_tty(self):
        cmd = self._call()
        assert "-it" not in cmd

    def test_auth_mounts_when_dirs_exist(self, tmp_path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude.json").write_text("{}")

        with patch("host.launch.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/claude-auth:ro" in cmd_str
        assert ".claude.json" in cmd_str

    def test_no_auth_mounts_when_dirs_missing(self, tmp_path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()  # No .claude or .claude.json

        with patch("host.launch.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/claude-auth:ro" not in cmd_str

    def test_notify_env_vars_forwarded(self):
        cmd = self._call(TELEGRAM_BOT_TOKEN="tok123", ANTHROPIC_API_KEY="sk-xxx")

        cmd_str = " ".join(cmd)
        assert "TELEGRAM_BOT_TOKEN=tok123" in cmd_str
        assert "ANTHROPIC_API_KEY=sk-xxx" in cmd_str

    def test_notify_env_vars_omitted_when_empty(self):
        cmd = self._call()  # env cleared by _call
        cmd_str = " ".join(cmd)
        assert "TELEGRAM_BOT_TOKEN" not in cmd_str

    def test_ssh_auth_sock_mounted(self):
        cmd = self._call(SSH_AUTH_SOCK="/tmp/ssh-agent.sock")

        cmd_str = " ".join(cmd)
        assert "/tmp/ssh-agent.sock:/ssh-agent" in cmd_str
        assert "SSH_AUTH_SOCK=/ssh-agent" in cmd_str

    def test_ssh_auth_sock_not_mounted_when_absent(self):
        cmd = self._call()  # SSH_AUTH_SOCK cleared
        cmd_str = " ".join(cmd)
        assert "/ssh-agent" not in cmd_str

    def test_user_flag_set(self):
        cmd = self._call()
        idx = cmd.index("--user")
        uid_gid = cmd[idx + 1]
        assert ":" in uid_gid  # should be "uid:gid"

    def test_project_name_env(self, tmp_path):
        repo = tmp_path / "myproject"
        repo.mkdir()
        (repo / ".git").mkdir()

        cmd = self._call(repo=repo)
        assert "PROJECT_NAME=myproject" in " ".join(cmd)

    def test_custom_image(self):
        cmd = self._call(image="nightshift:custom")
        assert cmd[-1] == "nightshift:custom"


# ── main() tests ─────────────────────────────────────────

class TestMain:

    @patch("host.launch.subprocess.run")
    @patch("host.launch._dump_issue_data")
    @patch("host.launch._create_worktree")
    @patch("host.launch._build_docker_cmd", return_value=["docker", "run", "test"])
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    @patch("host.launch._post_container")
    def test_main_start_flow(self, mock_post, mock_repo_root, mock_dotenv,
                             mock_load_wf, mock_build_cmd,
                             mock_create_wt, mock_dump, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        mock_repo_root.return_value = repo

        mock_load_wf.return_value = WorkflowConfig(
            workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
            agent=AgentConfig(max_turns=50),
        )
        mock_run.return_value = MagicMock(returncode=0)

        with patch("sys.argv", ["launch.py", "abc123def456ef"]):
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            assert exc_info.value.code == 0

        mock_create_wt.assert_called_once()
        mock_dump.assert_called_once()
        mock_build_cmd.assert_called_once()
        mock_run.assert_called_once_with(["docker", "run", "test"])

    @patch("host.launch.subprocess.run")
    @patch("host.launch._dump_issue_data")
    @patch("host.launch._build_docker_cmd", return_value=["docker", "run", "test"])
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    @patch("host.launch._post_container")
    def test_main_resume_requires_state(self, mock_post, mock_repo_root, mock_dotenv,
                                        mock_load_wf, mock_build_cmd,
                                        mock_dump, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        mock_repo_root.return_value = repo

        mock_load_wf.return_value = WorkflowConfig(
            workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
            agent=AgentConfig(max_turns=50),
        )

        with patch("sys.argv", ["launch.py", "abc123def456ef", "--resume"]):
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            # Should exit with 1 because no state.json exists
            assert exc_info.value.code == 1

    @patch("host.launch.subprocess.run")
    @patch("host.launch._dump_issue_data")
    @patch("host.launch._build_docker_cmd", return_value=["docker", "run", "test"])
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    @patch("host.launch._post_container")
    def test_main_resume_with_state(self, mock_post, mock_repo_root, mock_dotenv,
                                    mock_load_wf, mock_build_cmd,
                                    mock_dump, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        mock_repo_root.return_value = repo

        short_id = "abc123def456"
        session_dir = repo / ".nightshift" / "sessions" / short_id
        session_dir.mkdir(parents=True)
        (session_dir / "state.json").write_text('{"status": "suspended"}')

        mock_load_wf.return_value = WorkflowConfig(
            workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
            agent=AgentConfig(max_turns=50),
        )
        mock_run.return_value = MagicMock(returncode=0)

        with patch("sys.argv", ["launch.py", "abc123def456ef", "--resume"]):
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            assert exc_info.value.code == 0

        # _create_worktree should NOT be called on resume
        # (we'd need to check, but it's not patched here -- it shouldn't be
        #  reached because args.resume is True)


# ── _post_container tests ────────────────────────────────

class TestPostContainer:

    def test_noop_when_no_state_file(self, tmp_path, config):
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        # No state.json -- should return without error
        _post_container(session_dir, config, tmp_path, "issue1")

    def test_noop_when_status_not_waiting_review(self, tmp_path, config):
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text(json.dumps({
            "status": "running", "checkpoints": [], "human_answers": [],
        }))
        # Should return without error (status != waiting:review)
        _post_container(session_dir, config, tmp_path, "issue1")

    @patch("host.launch.create_tracker")
    @patch("host.launch.subprocess.run")
    def test_posts_summary_when_waiting_review(self, mock_run, mock_create_tracker,
                                                tmp_path, config):
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "branch": "agent/abc123",
            "checkpoints": [{"description": "Fixed bug"}],
            "human_answers": [{"q": "why?", "a": "because"}],
        }))

        mock_run.return_value = MagicMock(returncode=0, stdout="1 file changed")
        mock_tracker = MagicMock()
        mock_create_tracker.return_value = mock_tracker

        _post_container(session_dir, config, tmp_path, "issue1")

        mock_tracker.add_comment.assert_called_once()
        comment_body = mock_tracker.add_comment.call_args[0][1]
        assert "Fixed bug" in comment_body
        assert "1" in comment_body  # Q&A count
        mock_tracker.add_label.assert_called_once_with("issue1", "needs-review")
        mock_tracker.sync.assert_called_once()

    @patch("host.launch.create_tracker")
    @patch("host.launch.subprocess.run")
    def test_handles_tracker_failure_gracefully(self, mock_run, mock_create_tracker,
                                                 tmp_path, config):
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "branch": "agent/abc123",
            "checkpoints": [],
            "human_answers": [],
        }))

        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_create_tracker.side_effect = Exception("tracker down")

        # Should not raise
        _post_container(session_dir, config, tmp_path, "issue1")


# ── _prepare_review_session tests ────────────────────────

class TestPrepareReviewSession:

    @patch("host.launch.subprocess.run")
    def test_copies_issue_data_and_generates_diff(self, mock_run, tmp_path, config):
        from host.launch import _prepare_review_session
        repo = tmp_path / "repo"
        short_id = "abc123def456"

        coder_session = repo / ".nightshift" / "sessions" / short_id
        coder_session.mkdir(parents=True)
        (coder_session / "issue.json").write_text('{"id": "test"}')
        (coder_session / "issues.json").write_text('[{"id": "test"}]')

        review_session = tmp_path / "review_session"
        review_session.mkdir()

        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/f b/f\n+hello")

        _prepare_review_session(repo, review_session, short_id, config)

        assert (review_session / "issue.json").read_text() == '{"id": "test"}'
        assert (review_session / "issues.json").read_text() == '[{"id": "test"}]'
        assert "hello" in (review_session / "diff.patch").read_text()

    @patch("host.launch.subprocess.run")
    def test_writes_na_on_diff_failure(self, mock_run, tmp_path, config):
        from host.launch import _prepare_review_session
        repo = tmp_path / "repo"
        short_id = "abc123def456"

        review_session = tmp_path / "review_session"
        review_session.mkdir()

        mock_run.return_value = MagicMock(returncode=1, stdout="")

        _prepare_review_session(repo, review_session, short_id, config)

        assert (review_session / "diff.patch").read_text() == "N/A"
