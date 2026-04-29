"""Tests for host/launch.py and its extracted modules."""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.models import WorkflowConfig, AgentConfig, WorkspaceConfig
from core.protocols import TrackerIssue
from host.workspace_setup import create_worktree
from host.issue_dump import dump_issue_data
from host.docker_cmd import build_docker_cmd, run_container
from host.launch import _resolve_names


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


def _mock_git_run(cmd, status_stdout="", diff_stdout="", fsck_stdout="", commit_returncode=0):
    """Return a subprocess result tailored to the git command under test."""
    result = MagicMock(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["git", "status", "--porcelain"]:
        result.stdout = status_stdout
    elif len(cmd) >= 4 and cmd[:2] == ["git", "--git-dir"] and cmd[3] == "fsck":
        result.stdout = fsck_stdout
    elif cmd[:2] == ["git", "diff"]:
        result.stdout = diff_stdout
    elif cmd[:2] == ["git", "add"]:
        result.stdout = ""
    elif cmd[:2] == ["git", "commit"]:
        result.returncode = commit_returncode
    return result


# ── create_worktree tests ───────────────────────────────

class TestCreateWorktree:

    @patch("host.workspace_setup.safe_prune")
    @patch("host.workspace_setup.subprocess.run")
    def test_creates_worktree_and_writes_state(self, mock_run, mock_safe_prune, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"
        branch = "agent/abc123"
        base_branch = "master"
        issue_id = "abc123def456"

        # Simulate subprocess calls:
        # 1. git cat-file -t     -> ok (verify base commit exists)
        # 2. git branch          -> ok
        # 3. git worktree add    -> ok (creates the wt dir with a file)
        # (WT-6: safe_prune is mocked, not called via subprocess)
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="commit")
            if cmd[1] == "worktree" and cmd[2] == "add":
                # simulate worktree directory being created with content
                wt_path.mkdir(exist_ok=True)
                (wt_path / "README.md").write_text("hello")
                (wt_path / ".git").write_text("gitdir: ...")
            return result

        mock_run.side_effect = side_effect

        create_worktree(repo, wt_path, branch, base_branch, session_dir, issue_id)

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

        # WT-6: safe_prune is called instead of direct git worktree prune
        mock_safe_prune.assert_called_once_with(repo)

        # Correct git commands were called
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[0][0][0] == ["git", "cat-file", "-t", base_branch]
        assert mock_run.call_args_list[1][0][0] == ["git", "branch", branch, base_branch]
        assert mock_run.call_args_list[2][0][0] == ["git", "worktree", "add", str(wt_path), branch]

    @patch("host.workspace_setup.safe_prune")
    @patch("host.workspace_setup.subprocess.run")
    def test_exits_on_worktree_failure(self, mock_run, mock_safe_prune, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktree"
        session_dir = tmp_path / "session"

        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        # WT-6: safe_prune is mocked, so only 3 subprocess calls
        mock_run.side_effect = [
            MagicMock(returncode=0),  # cat-file -t (verify base exists)
            MagicMock(returncode=0),  # branch
            MagicMock(returncode=1, stderr="fatal: already exists"),  # worktree add
        ]

        with pytest.raises(SystemExit) as exc_info:
            create_worktree(repo, wt_path, "agent/x", "master", session_dir, "x")
        assert exc_info.value.code == 1

    @patch("host.workspace_setup.safe_prune")
    @patch("host.workspace_setup.subprocess.run")
    def test_exits_on_empty_worktree(self, mock_run, mock_safe_prune, tmp_path):
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
            create_worktree(repo, wt_path, "agent/x", "master", session_dir, "x")
        assert exc_info.value.code == 1

    @patch("host.workspace_setup.safe_prune")
    @patch("host.workspace_setup.force_remove_dir")
    @patch("host.workspace_setup.subprocess.run")
    def test_removes_existing_worktree_dir(self, mock_run, mock_force_rm, mock_safe_prune, tmp_path):
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

        create_worktree(repo, wt_path, "agent/x", "master", session_dir, "issue1")

        mock_force_rm.assert_called_once_with(wt_path)

    @patch("host.workspace_setup.subprocess.run")
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

        create_worktree(repo, wt_path, "agent/x", "master", session_dir, "issue1")

        assert (wt_path / ".gitignore").exists()
        assert (wt_path / ".gitignore").read_text() == "*.pyc\n__pycache__/\n"


# ── dump_issue_data tests ───────────────────────────────

class TestDumpIssueData:

    @patch("host.issue_dump.get_tracker_with_fallback")
    def test_dumps_issue_and_all_issues(self, mock_create_tracker, tmp_path, config, sample_issue):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        repo = tmp_path / "repo"

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = sample_issue
        mock_tracker.list_issues.return_value = [sample_issue]
        mock_create_tracker.return_value = mock_tracker

        dump_issue_data(config, repo, session_dir, "abc123def456",
                        is_review=False, is_resume=False)

        # issue.json written
        issue_data = json.loads((session_dir / "issue.json").read_text())
        assert issue_data["id"] == "abc123def456"
        assert issue_data["title"] == "Fix the widget"

        # issues.json written
        all_data = json.loads((session_dir / "issues.json").read_text())
        assert len(all_data) == 1
        assert all_data[0]["id"] == "abc123def456"

    @patch("host.issue_dump.get_tracker_with_fallback")
    def test_skips_dump_for_review_with_existing_issue(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "issue.json").write_text('{"id": "existing"}')

        dump_issue_data(config, tmp_path, session_dir, "abc123",
                        is_review=True, is_resume=False)

        # Tracker should not even be created
        mock_create_tracker.assert_not_called()

    @patch("host.issue_dump.get_tracker_with_fallback")
    def test_exits_when_issue_not_found_and_no_cache(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        with pytest.raises(SystemExit) as exc_info:
            dump_issue_data(config, tmp_path, session_dir, "missing",
                            is_review=False, is_resume=False)
        assert exc_info.value.code == 1

    @patch("host.issue_dump.get_tracker_with_fallback")
    def test_reuses_cache_on_resume_when_tracker_fails(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "issue.json").write_text('{"id": "cached"}')

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        # Should not exit -- reuses cached data
        dump_issue_data(config, tmp_path, session_dir, "abc",
                        is_review=False, is_resume=True)

        # issue.json should remain untouched
        assert json.loads((session_dir / "issue.json").read_text())["id"] == "cached"

    @patch("host.issue_dump.get_tracker_with_fallback")
    def test_exits_on_resume_without_cache(self, mock_create_tracker, tmp_path, config):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        # No issue.json exists

        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = None
        mock_create_tracker.return_value = mock_tracker

        with pytest.raises(SystemExit):
            dump_issue_data(config, tmp_path, session_dir, "abc",
                            is_review=False, is_resume=True)


# ── build_docker_cmd tests ──────────────────────────────

class TestBuildDockerCmd:

    def _call(self, repo=None, workspace_mount="/ws", session_dir=None,
              container_name="nightshift-abc", worktree_name="agent-abc",
              issue_id="abc123def456", short_id="abc123def456",
              max_turns=30, step="coder", is_resume=False,
              workflow_path="/repo/WORKFLOW.md", image="nightshift:latest",
              git_mount_path=None,
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
            return build_docker_cmd(
                repo, workspace_mount, session_dir, container_name,
                worktree_name, issue_id, short_id, max_turns,
                step, is_resume, workflow_path, image,
                git_mount_path=git_mount_path,
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

    def test_git_mount_path_override(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        git_mount_path = tmp_path / "session" / "git-merged"
        git_mount_path.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir(exist_ok=True)

        cmd = self._call(
            repo=repo,
            workspace_mount="/ws",
            session_dir=session_dir,
            workflow_path=str(tmp_path / "WORKFLOW.md"),
            git_mount_path=git_mount_path,
        )

        cmd_str = " ".join(cmd)
        assert f"{git_mount_path}:/repo-git:rw" in cmd_str
        assert f"{repo / '.git'}:/repo-git:rw" not in cmd_str

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

    def test_no_tty_flags(self):
        """Containers are fire-and-forget (-p mode), never need -it."""
        cmd = self._call()
        assert "-it" not in cmd
        assert "-i" not in cmd
        assert "-t" not in cmd

    def test_auth_mounts_when_dirs_exist(self, tmp_path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude.json").write_text("{}")

        with patch("host.docker_cmd.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/claude-auth:ro" in cmd_str
        assert ".claude.json" in cmd_str

    def test_no_auth_mounts_when_dirs_missing(self, tmp_path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()  # No .claude or .claude.json

        with patch("host.docker_cmd.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/claude-auth:ro" not in cmd_str

    def test_codex_auth_mounted(self, tmp_path):
        """When ~/.codex exists, docker command includes -v mount for /codex-auth."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".codex").mkdir()

        with patch("host.docker_cmd.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/codex-auth:ro" in cmd_str

    def test_codex_auth_not_mounted_when_missing(self, tmp_path):
        """When ~/.codex doesn't exist, no /codex-auth mount."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()  # No .codex

        with patch("host.docker_cmd.Path.home", return_value=fake_home):
            cmd = self._call()

        cmd_str = " ".join(cmd)
        assert "/codex-auth:ro" not in cmd_str

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


# ── run_container tests ──────────────────────────────────

class TestRunContainerStaleCleanup:
    """Verify that run_container removes stale containers before docker run."""

    @patch("host.docker_cmd.docker_remove")
    @patch("host.docker_cmd.subprocess.run")
    def test_removes_stale_container_before_run(self, mock_run, mock_remove, tmp_path):
        """docker_remove must be called with the container name before docker run."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: /repo-git/worktrees/agent-abc")
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mock_run.return_value = MagicMock(returncode=0)

        names = {
            "container_name": "nightshift-abc123",
            "worktree_name": "agent-abc123",
            "short_id": "abc123",
        }

        with patch("host.docker_cmd.Path.home", return_value=tmp_path):
            run_container(
                repo=tmp_path, workspace_mount=str(workspace),
                session_dir=session_dir, names=names,
                issue_id="abc123def456", max_turns=30,
                step="coder", is_resume=False,
                workflow_path=str(tmp_path / "WORKFLOW.md"),
                image="nightshift:latest",
            )

        mock_remove.assert_called_once_with("nightshift-abc123")
        mock_run.assert_called_once()

    @patch("host.docker_cmd.docker_remove")
    @patch("host.docker_cmd.subprocess.run")
    def test_launch_fails_if_container_removal_fails(self, mock_run, mock_remove, tmp_path):
        """If docker_remove returns False, run_container raises RuntimeError."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: /repo-git/worktrees/agent-abc")
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mock_remove.return_value = False
        mock_run.return_value = MagicMock(returncode=0)

        names = {
            "container_name": "nightshift-abc123",
            "worktree_name": "agent-abc123",
            "short_id": "abc123",
        }

        with patch("host.docker_cmd.Path.home", return_value=tmp_path):
            with pytest.raises(RuntimeError) as exc_info:
                run_container(
                    repo=tmp_path, workspace_mount=str(workspace),
                    session_dir=session_dir, names=names,
                    issue_id="abc123def456", max_turns=30,
                    step="coder", is_resume=False,
                    workflow_path=str(tmp_path / "WORKFLOW.md"),
                    image="nightshift:latest",
                )

        assert "Failed to remove stale container nightshift-abc123" in str(exc_info.value)
        mock_remove.assert_called_once()
        mock_run.assert_not_called()

    @patch("host.docker_cmd.docker_remove")
    @patch("host.docker_cmd.subprocess.run")
    def test_remove_called_before_docker_run(self, mock_run, mock_remove, tmp_path):
        """Verify ordering: docker_remove happens before subprocess.run."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: /repo-git/worktrees/agent-abc")
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        call_order = []
        mock_remove.side_effect = lambda _: call_order.append("remove") or True
        mock_run.side_effect = lambda *a, **kw: (
            call_order.append("run") or MagicMock(returncode=0)
        )

        names = {
            "container_name": "nightshift-abc123",
            "worktree_name": "agent-abc123",
            "short_id": "abc123",
        }

        with patch("host.docker_cmd.Path.home", return_value=tmp_path):
            run_container(
                repo=tmp_path, workspace_mount=str(workspace),
                session_dir=session_dir, names=names,
                issue_id="abc123def456", max_turns=30,
                step="coder", is_resume=False,
                workflow_path=str(tmp_path / "WORKFLOW.md"),
                image="nightshift:latest",
            )

        assert call_order == ["remove", "run"]


# ── main() tests ─────────────────────────────────────────

class TestMain:

    @patch("host.docker_cmd.docker_remove")
    @patch("host.launch.subprocess.run")
    @patch("host.launch.dump_issue_data")
    @patch("host.workspace_setup.create_worktree")
    @patch("host.docker_cmd.build_docker_cmd", return_value=["docker", "run", "test"])
    @patch("host.launch._setup_git_overlay")
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    @patch("host.launch._post_container")
    def test_main_start_flow(self, mock_post, mock_repo_root, mock_dotenv,
                             mock_load_wf, mock_setup_overlay, mock_build_cmd,
                             mock_create_wt, mock_dump, mock_run,
                             mock_docker_rm, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        (repo / "WORKFLOW.md").write_text("---\n---\n")
        mock_repo_root.return_value = repo

        mock_load_wf.return_value = WorkflowConfig(
            workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
            agent=AgentConfig(max_turns=50),
        )
        mock_run.return_value = MagicMock(returncode=0)
        git_copy_path = repo / ".nightshift" / "sessions" / "abc123def456" / "git-copy"
        git_copy_path.mkdir(parents=True, exist_ok=True)
        (git_copy_path / "HEAD").write_text("ref: refs/heads/master\n")
        mock_setup_overlay.return_value = git_copy_path

        with patch("sys.argv", ["launch.py", "abc123def456ef"]):
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            assert exc_info.value.code == 0

        mock_create_wt.assert_called_once()
        mock_dump.assert_called_once()
        mock_build_cmd.assert_called_once()
        mock_run.assert_called_once_with(["docker", "run", "test"])
        mock_docker_rm.assert_called_once()

    @patch("host.launch.run_container", side_effect=RuntimeError("launch failed"))
    @patch("host.launch._setup_git_overlay")
    @patch("host.launch._teardown_git_overlay")
    @patch("host.launch._post_container")
    @patch("host.launch.setup_workspace")
    @patch("host.launch.dump_issue_data")
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    def test_main_cleans_up_overlay_when_launch_fails(self, mock_repo_root,
                                                      mock_dotenv, mock_load_wf,
                                                      mock_dump_issue_data,
                                                      mock_setup_workspace, mock_post,
                                                      mock_teardown,
                                                      mock_setup_overlay,
                                                      mock_run_container, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        (repo / "WORKFLOW.md").write_text("---\n---\n")
        mock_repo_root.return_value = repo

        mock_load_wf.return_value = WorkflowConfig(
            workspace=WorkspaceConfig(base_branch="master", root=".worktrees"),
            agent=AgentConfig(max_turns=50),
        )
        mock_dump_issue_data.return_value = None
        mock_setup_workspace.return_value = repo / ".worktrees" / "agent-abc123def456"
        git_merged_path = repo / ".nightshift" / "sessions" / "abc123def456" / "git-merged"
        git_merged_path.mkdir(parents=True, exist_ok=True)
        (git_merged_path / "HEAD").write_text("ref: refs/heads/master\n")
        mock_setup_overlay.return_value = git_merged_path

        with patch("sys.argv", ["launch.py", "abc123def456ef"]):
            with pytest.raises(RuntimeError, match="launch failed"):
                from host.launch import main
                main()

        mock_post.assert_not_called()
        mock_teardown.assert_called_once_with(
            repo / ".nightshift" / "sessions" / "abc123def456" / "git-merged",
            repo / ".nightshift" / "sessions" / "abc123def456",
        )

    @patch("host.launch.subprocess.run")
    @patch("host.launch.dump_issue_data")
    @patch("host.docker_cmd.build_docker_cmd", return_value=["docker", "run", "test"])
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
        (repo / "WORKFLOW.md").write_text("---\n---\n")
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
    @patch("host.launch.dump_issue_data")
    @patch("host.docker_cmd.build_docker_cmd", return_value=["docker", "run", "test"])
    @patch("host.launch._setup_git_overlay")
    @patch("host.launch.load_workflow")
    @patch("host.launch.load_all_dotenv")
    @patch("host.launch.get_repo_root")
    @patch("host.launch._post_container")
    def test_main_resume_with_state(self, mock_post, mock_repo_root, mock_dotenv,
                                    mock_load_wf, mock_setup_overlay, mock_build_cmd,
                                    mock_dump, mock_run, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".worktrees").mkdir()
        (repo / "WORKFLOW.md").write_text("---\n---\n")
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
        git_copy_path = session_dir / "git-copy"
        git_copy_path.mkdir(parents=True, exist_ok=True)
        (git_copy_path / "HEAD").write_text("ref: refs/heads/master\n")
        mock_setup_overlay.return_value = git_copy_path

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

    @patch("host.launch.get_tracker_with_fallback")
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

        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
            diff_stdout="1 file changed",
        )
        mock_tracker = MagicMock()
        mock_create_tracker.return_value = mock_tracker

        _post_container(session_dir, config, tmp_path, "issue1")

        mock_tracker.add_comment.assert_called_once()
        comment_body = mock_tracker.add_comment.call_args[0][1]
        assert "Fixed bug" in comment_body
        assert "1" in comment_body  # Q&A count
        mock_tracker.add_label.assert_called_once_with("issue1", "needs-review")
        mock_tracker.sync.assert_called_once()

    @patch("host.launch.get_tracker_with_fallback")
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

        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )
        mock_create_tracker.side_effect = Exception("tracker down")

        # Should not raise
        _post_container(session_dir, config, tmp_path, "issue1")

    @patch("host.launch.get_tracker_with_fallback")
    @patch("host.launch.subprocess.run")
    def test_post_container_includes_cost_in_comment(self, mock_run, mock_create_tracker,
                                                      tmp_path, config):
        """Proof-of-work comment should contain 'Cost:' line with resumes when usage data exists."""
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
        (session_dir / "issue.json").write_text(json.dumps({"title": "Fix the widget"}))
        (session_dir / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "branch": "agent/abc123",
            "step": 3,
            "checkpoints": [{"description": "Fixed bug"}],
            "human_answers": [],
            "usage": {
                "input_tokens": 45000,
                "output_tokens": 12000,
                "cost_usd": 0.38,
                "model": "claude-sonnet-4-6",
            },
        }))

        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
            diff_stdout="1 file changed",
        )
        mock_tracker = MagicMock()
        mock_create_tracker.return_value = mock_tracker

        _post_container(session_dir, config, tmp_path, "issue1")

        comment_body = mock_tracker.add_comment.call_args[0][1]
        assert "Cost:" in comment_body
        assert "45K input" in comment_body
        assert "$0.38" in comment_body
        assert "claude-sonnet-4-6" in comment_body
        assert "3 resumes" in comment_body

    @patch("host.launch.get_tracker_with_fallback")
    @patch("host.launch.subprocess.run")
    def test_usage_appended_to_jsonl_on_done(self, mock_run, mock_create_tracker,
                                              tmp_path, config):
        """After _post_container, usage.jsonl should have one new line with correct fields."""
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
        (session_dir / "issue.json").write_text(json.dumps({"title": "Fix the widget"}))
        (session_dir / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "branch": "agent/abc123",
            "started_at": "2025-01-01T00:00:00",
            "completed_at": "2025-01-01T01:00:00",
            "step": 3,
            "checkpoints": [],
            "human_answers": [],
            "usage": {
                "input_tokens": 45000,
                "output_tokens": 12000,
                "cost_usd": 0.38,
                "model": "claude-sonnet-4-6",
            },
        }))

        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )
        mock_tracker = MagicMock()
        mock_create_tracker.return_value = mock_tracker

        _post_container(session_dir, config, tmp_path, "issue1")

        usage_file = tmp_path / ".nightshift" / "usage.jsonl"
        assert usage_file.exists()
        entry = json.loads(usage_file.read_text().strip())
        assert entry["issue_id"] == "issue1"
        assert entry["title"] == "Fix the widget"
        assert entry["agent_kind"] == "claude-code"
        assert entry["input_tokens"] == 45000
        assert entry["output_tokens"] == 12000
        assert entry["cost_usd"] == 0.38
        assert entry["model"] == "claude-sonnet-4-6"
        assert entry["resumes"] == 3

    @patch("host.launch.get_tracker_with_fallback")
    @patch("host.launch.subprocess.run")
    def test_usage_jsonl_survives_cleanup(self, mock_run, mock_create_tracker,
                                          tmp_path, config):
        """After cleanup (session dir removed), usage.jsonl still contains the entry."""
        from host.launch import _post_container
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
        (session_dir / "issue.json").write_text(json.dumps({"title": "Test issue"}))
        (session_dir / "state.json").write_text(json.dumps({
            "status": "waiting:review",
            "branch": "agent/abc123",
            "checkpoints": [],
            "human_answers": [],
            "usage": {
                "input_tokens": 10000, "output_tokens": 5000,
                "cost_usd": 0.12, "model": "claude-sonnet-4-6",
            },
        }))

        mock_run.side_effect = lambda cmd, **kwargs: _mock_git_run(
            cmd,
            status_stdout="",
        )
        mock_tracker = MagicMock()
        mock_create_tracker.return_value = mock_tracker

        _post_container(session_dir, config, tmp_path, "issue1")

        # Simulate cleanup: remove session dir
        import shutil
        shutil.rmtree(session_dir)

        # usage.jsonl should still exist
        usage_file = tmp_path / ".nightshift" / "usage.jsonl"
        assert usage_file.exists()
        entry = json.loads(usage_file.read_text().strip())
        assert entry["input_tokens"] == 10000

    @patch("host.launch._copy_git_changes")
    def test_overlay_extraction_runs_after_container(self, mock_copy,
                                                     tmp_path, config):
        from host.launch import _post_container

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "state.json").write_text(json.dumps({
            "status": "running",
            "checkpoints": [],
            "human_answers": [],
        }))
        (session_dir / "git-upper").mkdir()
        (session_dir / "git-merged").mkdir()
        (tmp_path / ".git").mkdir()

        _post_container(session_dir, config, tmp_path, "issue1")

        mock_copy.assert_called_once_with(session_dir, tmp_path)


class TestCopyGitChanges:

    @patch("host.launch.subprocess.run")
    def test_copy_git_changes_runs_fsck(self, mock_run, tmp_path):
        from host.launch import _copy_git_changes

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        source = session_dir / "git-copy"
        source.mkdir()
        (source / "objects").mkdir()
        (source / "refs" / "heads").mkdir(parents=True)
        (source / "refs" / "heads" / "agent-123").write_text("0123456789abcdef0123456789abcdef01234567\n")

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        _copy_git_changes(session_dir, repo)

        mock_run.assert_called_once_with(
            ["git", "--git-dir", str(source), "fsck", "--connectivity-only"],
            capture_output=True,
            text=True,
        )

    @patch("host.launch.subprocess.run")
    def test_fsck_filters_gitbug_noise(self, mock_run, tmp_path, caplog):
        from host.launch import _copy_git_changes

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        source = session_dir / "git-copy"
        source.mkdir()
        (source / "objects").mkdir()
        (source / "refs" / "heads").mkdir(parents=True)
        (source / "refs" / "heads" / "agent" / "good").parent.mkdir(parents=True)
        (source / "refs" / "heads" / "agent" / "good").write_text(
            "0123456789abcdef0123456789abcdef01234567\n"
        )

        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr=(
                "error: Unknown object type for b6f32d868edadd0a96e6f0256fdc568c6688deae\n"
                "error: Could not read b619a2a6fb4d71eda862f66e0f9847ed7784aa18\n"
                "fatal: not a git repository (or any of the parent directories): .git\n"
            ),
        )

        with caplog.at_level(logging.ERROR, logger="host.launch"):
            result = _copy_git_changes(session_dir, repo)

        assert result == 0
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)
        assert (repo / ".git" / "refs" / "heads" / "agent" / "good").read_text().strip() == \
            "0123456789abcdef0123456789abcdef01234567"

    @patch("host.launch.subprocess.run")
    def test_fsck_reports_real_corruption(self, mock_run, tmp_path, caplog):
        from host.launch import _copy_git_changes

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        source = session_dir / "git-copy"
        source.mkdir()
        (source / "objects" / "aa").mkdir(parents=True)
        (source / "objects" / "aa" / "badpack").write_text("corrupt")
        (source / "refs" / "heads").mkdir(parents=True)
        (source / "refs" / "heads" / "agent-123").write_text("0123456789abcdef0123456789abcdef01234567\n")

        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr=(
                "error: Unknown object type for b6f32d868edadd0a96e6f0256fdc568c6688deae\n"
                "fatal: bad object 0123456789abcdef0123456789abcdef01234567\n"
            ),
        )

        with caplog.at_level(logging.ERROR, logger="host.launch"):
            result = _copy_git_changes(session_dir, repo)

        assert result != 0
        assert "git fsck found issues" in caplog.text
        assert "fatal: bad object" in caplog.text
        assert "Unknown object type" not in caplog.text
        assert not (repo / ".git" / "objects" / "aa" / "badpack").exists()
        assert not (repo / ".git" / "refs" / "heads" / "agent-123").exists()

    @patch("host.launch.subprocess.run")
    def test_copy_git_changes_whitelists_refs(self, mock_run, tmp_path, caplog):
        from host.launch import _copy_git_changes

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        source = session_dir / "git-copy"
        source.mkdir()
        (source / "objects").mkdir()
        (source / "refs" / "heads").mkdir(parents=True)
        # Use agent/xxx format (slash, not hyphen) per current ref whitelist
        (source / "refs" / "heads" / "agent" / "good").parent.mkdir(parents=True)
        (source / "refs" / "heads" / "agent" / "good").write_text("0123456789abcdef0123456789abcdef01234567\n")
        (source / "refs" / "heads" / "agent" / "bad" / "evil").parent.mkdir(parents=True)
        (source / "refs" / "heads" / "agent" / "bad" / "evil").write_text(
            "fedcba9876543210fedcba9876543210fedcba98\n"
        )
        (source / "refs" / "heads" / "main").write_text("89abcdef0123456789abcdef0123456789abcdef\n")

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with caplog.at_level(logging.WARNING, logger="host.git_overlay"):
            result = _copy_git_changes(session_dir, repo)

        assert result == 0
        assert (repo / ".git" / "refs" / "heads" / "agent" / "good").read_text().strip() == \
            "0123456789abcdef0123456789abcdef01234567"
        assert not (repo / ".git" / "refs" / "heads" / "agent" / "bad" / "evil").exists()
        assert not (repo / ".git" / "refs" / "heads" / "main").exists()
        assert "skipped" in caplog.text.lower()


# ── Git overlay wiring tests ────────────────────────────

class TestGitOverlayWiring:

    @patch("host.launch.is_fuse_overlayfs_available", return_value=True)
    @patch("host.launch.setup_overlay")
    @patch("host.launch.setup_git_copy")
    def test_overlay_setup_creates_merged_mount(self, mock_copy, mock_setup,
                                                mock_available, tmp_path):
        from host.launch import _setup_git_overlay

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        merged = session_dir / "git-merged"
        mock_setup.return_value = merged

        result = _setup_git_overlay(repo, session_dir)

        assert result == merged
        mock_setup.assert_called_once_with(repo / ".git", session_dir)
        mock_copy.assert_not_called()

    @patch("host.launch.teardown_overlay")
    def test_overlay_teardown_unmounts(self, mock_teardown, tmp_path):
        from host.launch import _teardown_git_overlay

        merged = tmp_path / "session" / "git-merged"
        merged.parent.mkdir(parents=True)
        merged.mkdir()

        _teardown_git_overlay(merged, merged.parent)

        mock_teardown.assert_called_once_with(merged)

    @patch("host.launch.teardown_overlay")
    def test_overlay_teardown_removes_session_dirs(self, mock_teardown, tmp_path):
        from host.launch import _teardown_git_overlay

        session_dir = tmp_path / "session"
        merged = session_dir / "git-merged"
        upper = session_dir / "git-upper"
        work = session_dir / "git-work"
        for path in (merged, upper, work):
            path.mkdir(parents=True)

        _teardown_git_overlay(merged, session_dir)

        mock_teardown.assert_called_once_with(merged)
        assert not merged.exists()
        assert not upper.exists()
        assert not work.exists()


# ── prepare_review_session tests ────────────────────────

class TestPrepareReviewSession:

    @patch("host.workspace_setup.subprocess.run")
    def test_copies_issue_data_and_generates_diff(self, mock_run, tmp_path, config):
        from host.workspace_setup import prepare_review_session
        repo = tmp_path / "repo"
        short_id = "abc123def456"

        coder_session = repo / ".nightshift" / "sessions" / short_id
        coder_session.mkdir(parents=True)
        (coder_session / "issue.json").write_text('{"id": "test"}')
        (coder_session / "issues.json").write_text('[{"id": "test"}]')

        review_session = tmp_path / "review_session"
        review_session.mkdir()

        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/f b/f\n+hello")

        prepare_review_session(repo, review_session, short_id, config)

        assert (review_session / "issue.json").read_text() == '{"id": "test"}'
        assert (review_session / "issues.json").read_text() == '[{"id": "test"}]'
        assert "hello" in (review_session / "diff.patch").read_text()

    @patch("host.workspace_setup.subprocess.run")
    def test_writes_na_on_diff_failure(self, mock_run, tmp_path, config):
        from host.workspace_setup import prepare_review_session
        repo = tmp_path / "repo"
        short_id = "abc123def456"

        review_session = tmp_path / "review_session"
        review_session.mkdir()

        mock_run.return_value = MagicMock(returncode=1, stdout="")

        prepare_review_session(repo, review_session, short_id, config)

        assert (review_session / "diff.patch").read_text() == "N/A"


# ── _resolve_names tests ─────────────────────────────────

class TestResolveNames:

    def test_coder_container_name(self, config):
        names = _resolve_names("abc123def456ef", "coder", config)
        assert names["container_name"] == "nightshift-abc123def456"
        assert not names["is_review"]
        assert names["session_name"] == "abc123def456"

    def test_review_container_name(self, config):
        names = _resolve_names("abc123def456ef", "review", config)
        assert names["container_name"] == "nightshift-review-abc123def456"
        assert names["is_review"]
        assert names["session_name"] == "review-abc123def456"

    def test_no_container_name_collision(self, config):
        coder = _resolve_names("abc123def456ef", "coder", config)
        review = _resolve_names("abc123def456ef", "review", config)
        assert coder["container_name"] != review["container_name"]


# ── Review overflow isolation tests ─────────────────────

class TestReviewOverflowIsolation:

    @patch("host.docker_cmd.docker_remove")
    @patch("host.docker_cmd.subprocess.run")
    def test_review_launch_ignores_overflow(self, mock_run, mock_remove, tmp_path):
        """Review launch with overflow=None does not inject overflow env vars."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: /repo-git/worktrees/agent-abc")
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mock_run.return_value = MagicMock(returncode=0)

        names = {
            "container_name": "nightshift-review-abc123",
            "worktree_name": "agent-abc123",
            "short_id": "abc123",
        }

        with patch("host.docker_cmd.Path.home", return_value=tmp_path):
            run_container(
                repo=tmp_path, workspace_mount=str(workspace),
                session_dir=session_dir, names=names,
                issue_id="abc123def456", max_turns=30,
                step="review", is_resume=False,
                workflow_path=str(tmp_path / "WORKFLOW.md"),
                image="nightshift:latest",
                overflow=None,
            )

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "sk-overflow" not in cmd_str
        assert "ANTHROPIC_BASE_URL" not in cmd_str


# ── OpenHands env var passthrough ──────────────────────────


# ── Codex Docker support (REQ-033) ──────────────────────────


class TestCodexDockerSupport:

    def test_codex_config_mounted_in_container(self):
        """When overflow env vars are set, they're passed through so
        docker-entrypoint.sh can generate ~/.codex/config.toml at runtime."""
        from core.config.models import OverflowConfig

        overflow = OverflowConfig(
            env={
                "OVERFLOW_API_KEY": "sk-codex-test",
                "OVERFLOW_BASE_URL": "https://openrouter.ai/api/v1",
                "OVERFLOW_MODEL": "qwen/qwen3-coder",
            },
        )
        cmd = build_docker_cmd(
            repo=Path("/repo"),
            workspace_mount="/workspace",
            session_dir=Path("/session"),
            container_name="nightshift-abc123",
            worktree_name="agent-abc123",
            issue_id="abc123",
            short_id="abc123",
            max_turns=50,
            step="coder",
            is_resume=False,
            workflow_path="/repo/WORKFLOW.md",
            image="nightshift:latest",
            overflow=overflow,
        )

        env_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "-e" and i + 1 < len(cmd):
                env_pairs.append(cmd[i + 1])

        # Overflow env vars needed by docker-entrypoint.sh for codex config
        assert "OVERFLOW_API_KEY=sk-codex-test" in env_pairs
        assert "OVERFLOW_BASE_URL=https://openrouter.ai/api/v1" in env_pairs
        assert "OVERFLOW_MODEL=qwen/qwen3-coder" in env_pairs
        assert "OVERFLOW_ACTIVE=1" in env_pairs

    def test_codex_env_vars_passthrough(self):
        """CODEX_API_KEY, CODEX_BASE_URL, CODEX_MODEL, OPENAI_API_KEY are forwarded to the container."""
        env_to_clear = [
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_WEBHOOK_URL",
            "SLACK_WEBHOOK", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
        ]
        saved = {}
        for var in env_to_clear:
            if var in os.environ:
                saved[var] = os.environ.pop(var)

        os.environ["CODEX_API_KEY"] = "sk-test"
        os.environ["CODEX_BASE_URL"] = "https://example.com/v1"
        os.environ["CODEX_MODEL"] = "test-model"
        os.environ["OPENAI_API_KEY"] = "sk-openai-test"

        try:
            cmd = build_docker_cmd(
                repo=Path("/repo"),
                workspace_mount="/workspace",
                session_dir=Path("/session"),
                container_name="nightshift-abc123",
                worktree_name="agent-abc123",
                issue_id="abc123",
                short_id="abc123",
                max_turns=50,
                step="coder",
                is_resume=False,
                workflow_path="/repo/WORKFLOW.md",
                image="nightshift:latest",
            )

            env_pairs = []
            for i, arg in enumerate(cmd):
                if arg == "-e" and i + 1 < len(cmd):
                    env_pairs.append(cmd[i + 1])

            assert "CODEX_API_KEY=sk-test" in env_pairs
            assert "CODEX_BASE_URL=https://example.com/v1" in env_pairs
            assert "CODEX_MODEL=test-model" in env_pairs
            assert "OPENAI_API_KEY=sk-openai-test" in env_pairs
        finally:
            os.environ.pop("CODEX_API_KEY", None)
            os.environ.pop("CODEX_BASE_URL", None)
            os.environ.pop("CODEX_MODEL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            for k, v in saved.items():
                os.environ[k] = v


class TestCodexAgentKindEnv:

    def test_codex_agent_gets_agent_kind_env(self):
        """When agent_kind is codex, AGENT_KIND=codex is in docker command
        so docker-entrypoint.sh can generate ~/.codex/config.toml."""
        cmd = build_docker_cmd(
            repo=Path("/repo"),
            workspace_mount="/workspace",
            session_dir=Path("/session"),
            container_name="nightshift-abc123",
            worktree_name="agent-abc123",
            issue_id="abc123",
            short_id="abc123",
            max_turns=50,
            step="coder",
            is_resume=False,
            workflow_path="/repo/WORKFLOW.md",
            image="nightshift:latest",
            agent_kind="codex",
        )

        env_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "-e" and i + 1 < len(cmd):
                env_pairs.append(cmd[i + 1])

        assert "AGENT_KIND=codex" in env_pairs

    def test_non_codex_agent_no_codex_agent_kind(self):
        """When agent_kind is claude-code, AGENT_KIND=claude-code (not codex)."""
        cmd = build_docker_cmd(
            repo=Path("/repo"),
            workspace_mount="/workspace",
            session_dir=Path("/session"),
            container_name="nightshift-abc123",
            worktree_name="agent-abc123",
            issue_id="abc123",
            short_id="abc123",
            max_turns=50,
            step="coder",
            is_resume=False,
            workflow_path="/repo/WORKFLOW.md",
            image="nightshift:latest",
            agent_kind="claude-code",
        )

        env_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "-e" and i + 1 < len(cmd):
                env_pairs.append(cmd[i + 1])

        assert "AGENT_KIND=claude-code" in env_pairs
        # Should NOT have AGENT_KIND=codex
        assert "AGENT_KIND=codex" not in env_pairs


class TestOpenHandsEnvVars:

    def test_openhands_env_vars_forwarded(self):
        """When LLM_API_KEY, LLM_MODEL, LLM_BASE_URL are set, they appear in the docker command."""
        env_to_clear = [
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_WEBHOOK_URL",
            "SLACK_WEBHOOK", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
        ]
        saved = {}
        for var in env_to_clear:
            if var in os.environ:
                saved[var] = os.environ.pop(var)

        os.environ["LLM_API_KEY"] = "sk-test-llm"
        os.environ["LLM_MODEL"] = "gpt-4"
        os.environ["LLM_BASE_URL"] = "https://api.example.com"

        try:
            cmd = build_docker_cmd(
                repo=Path("/repo"),
                workspace_mount="/workspace",
                session_dir=Path("/session"),
                container_name="nightshift-abc123",
                worktree_name="agent-abc123",
                issue_id="abc123",
                short_id="abc123",
                max_turns=50,
                step="coder",
                is_resume=False,
                workflow_path="/repo/WORKFLOW.md",
                image="nightshift:latest",
            )

            env_pairs = []
            for i, arg in enumerate(cmd):
                if arg == "-e" and i + 1 < len(cmd):
                    env_pairs.append(cmd[i + 1])

            assert "LLM_API_KEY=sk-test-llm" in env_pairs
            assert "LLM_MODEL=gpt-4" in env_pairs
            assert "LLM_BASE_URL=https://api.example.com" in env_pairs
        finally:
            os.environ.pop("LLM_API_KEY", None)
            os.environ.pop("LLM_MODEL", None)
            os.environ.pop("LLM_BASE_URL", None)
            for k, v in saved.items():
                os.environ[k] = v


# ── Duplicate session detection tests ────────────────────

class TestDuplicateSessionDetection:
    """launch.py main() should reject starts when a session already exists by prefix."""

    def _create_session(self, sessions_dir, session_name, issue_id, status="working"):
        sd = sessions_dir / session_name
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "state.json").write_text(json.dumps({
            "issue_id": issue_id,
            "status": status,
        }))

    def test_find_existing_session_allows_review_when_coder_exists(self, tmp_path):
        """Review launch should NOT be blocked when coder session exists.

        When launching --step review, the coder session for the same issue
        is expected to exist. The duplicate check should only block if a
        review session already exists, not when a coder session exists.
        """
        from host.session_utils import find_existing_session_by_prefix
        from host.constants import REVIEW_SESSION_PREFIX

        sessions_dir = tmp_path / ".nightshift" / "sessions"
        issue_id = "64dd71361d31full"
        short_id = "64dd71361d31"

        # Create coder session
        self._create_session(sessions_dir, short_id, issue_id, status="waiting:review")

        # When launching a review, we should NOT find a blocking session
        # because the coder session exists but we're launching a DIFFERENT type
        existing = find_existing_session_by_prefix(sessions_dir, issue_id, step="review")
        assert existing is None, \
            "Review launch should not be blocked by existing coder session"

        # BUT if a review session already exists, it SHOULD block
        self._create_session(sessions_dir, f"{REVIEW_SESSION_PREFIX}{short_id}",
                            issue_id, status="working")
        existing = find_existing_session_by_prefix(sessions_dir, issue_id, step="review")
        assert existing == issue_id, \
            "Review launch should be blocked when review session already exists"

    def test_find_existing_session_blocks_coder_when_coder_exists(self, tmp_path):
        """Coder launch should be blocked when coder session already exists."""
        from host.session_utils import find_existing_session_by_prefix

        sessions_dir = tmp_path / ".nightshift" / "sessions"
        issue_id = "64dd71361d31full"
        short_id = "64dd71361d31"

        # Create coder session
        self._create_session(sessions_dir, short_id, issue_id)

        # When launching a coder, we SHOULD find a blocking session
        existing = find_existing_session_by_prefix(sessions_dir, issue_id, step="coder")
        assert existing == issue_id, \
            "Coder launch should be blocked by existing coder session"

    def test_start_rejects_duplicate_session_by_prefix(self, tmp_path):
        """Starting with short ID when full-ID session exists fails."""
        sessions_dir = tmp_path / ".nightshift" / "sessions"
        self._create_session(sessions_dir, "64dd71361d31", "64dd71361d31")

        with patch("host.launch.get_repo_root", return_value=tmp_path), \
             patch("host.launch.load_all_dotenv"), \
             patch("host.launch.discover_workflow", return_value=tmp_path / "WF.md"), \
             patch("host.launch.load_workflow") as mock_lw, \
             patch("sys.argv", ["launch.py", "64dd713"]):
            mock_lw.return_value = WorkflowConfig(
                agent=AgentConfig(max_turns=30),
                workspace=WorkspaceConfig(base_branch="master"),
            )
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            assert exc_info.value.code == 1

    def test_start_rejects_duplicate_session_by_full_id(self, tmp_path):
        """Starting with full ID when short-ID session exists fails."""
        sessions_dir = tmp_path / ".nightshift" / "sessions"
        self._create_session(sessions_dir, "64dd713", "64dd713")

        with patch("host.launch.get_repo_root", return_value=tmp_path), \
             patch("host.launch.load_all_dotenv"), \
             patch("host.launch.discover_workflow", return_value=tmp_path / "WF.md"), \
             patch("host.launch.load_workflow") as mock_lw, \
             patch("sys.argv", ["launch.py", "64dd71361d31"]):
            mock_lw.return_value = WorkflowConfig(
                agent=AgentConfig(max_turns=30),
                workspace=WorkspaceConfig(base_branch="master"),
            )
            with pytest.raises(SystemExit) as exc_info:
                from host.launch import main
                main()
            assert exc_info.value.code == 1


# ── GAP-001: Git validation tests ─────────────────────────────────────────────

from host.launch import _copy_git_changes, _auto_commit_uncommitted_changes


class TestCopyGitChangesValidatesFsck:
    """Tests for git fsck validation in _copy_git_changes (GAP-001)."""

    def test_copy_git_changes_validates_fsck(self, tmp_path):
        """_copy_git_changes runs fsck and fails on real corruption."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        git_merged = session_dir / "git-merged"
        git_merged.mkdir()

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch("host.launch.validate_git_objects") as mock_validate, \
             patch("host.launch.extract_commits"):
            # Simulate corruption
            mock_validate.return_value = (False, ["error: corrupt loose object abc123"])
            result = _copy_git_changes(session_dir, repo)
            assert result == 1
            mock_validate.assert_called_once_with(git_merged)

    def test_copy_git_changes_passes_clean_repo(self, tmp_path):
        """_copy_git_changes passes when fsck finds no issues."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        git_merged = session_dir / "git-merged"
        git_merged.mkdir()

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch("host.launch.validate_git_objects") as mock_validate, \
             patch("host.launch.extract_commits") as mock_extract:
            mock_validate.return_value = (True, [])
            mock_extract.return_value = []
            result = _copy_git_changes(session_dir, repo)
            assert result == 0

    def test_copy_git_changes_no_git_dir_returns_zero(self, tmp_path):
        """_copy_git_changes returns 0 when no git dir exists."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        result = _copy_git_changes(session_dir, repo)
        assert result == 0


class TestAutoCommitUncommittedChanges:
    """Tests for auto-commit fallback (GAP-001)."""

    def test_auto_commit_uncommitted_changes(self, tmp_path):
        """Auto-commit is called when worktree has changes."""
        with patch("host.launch.auto_commit_dirty_worktree") as mock_auto:
            mock_auto.return_value = True
            result = _auto_commit_uncommitted_changes(tmp_path, "test-session")
            assert result is True
            mock_auto.assert_called_once_with(
                tmp_path,
                message="WIP: auto-commit uncommitted changes on @@DONE@@",
            )

    def test_auto_commit_clean_worktree_no_commit(self, tmp_path):
        """No commit made when worktree is clean."""
        with patch("host.launch.auto_commit_dirty_worktree") as mock_auto:
            mock_auto.return_value = False
            result = _auto_commit_uncommitted_changes(tmp_path, "test-session")
            assert result is False


class TestGitOverlayValidation:
    """Tests for git overlay validation guardrails."""

    def test_git_overlay_validation_fails_on_empty(self, tmp_path):
        """_setup_git_overlay followed by empty directory raises RuntimeError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "WORKFLOW.md").write_text("---\n---\n")
        session_dir = repo / ".nightshift" / "sessions" / "abc123def456"
        session_dir.mkdir(parents=True)

        # Simulate _setup_git_overlay returning an empty directory
        empty_mount = session_dir / "git-merged"
        empty_mount.mkdir()

        with patch("host.launch._setup_git_overlay", return_value=empty_mount), \
             patch("host.launch.setup_workspace", return_value=tmp_path / "workspace"), \
             patch("host.launch.dump_issue_data"), \
             patch("host.launch.load_workflow") as mock_lw, \
             patch("host.launch.load_all_dotenv"), \
             patch("host.launch.discover_workflow", return_value=repo / "WORKFLOW.md"), \
             patch("host.launch.get_repo_root", return_value=repo), \
             patch("sys.argv", ["launch.py", "abc123def456ef"]):
            from core.config.models import WorkflowConfig, AgentConfig, WorkspaceConfig
            mock_lw.return_value = WorkflowConfig(
                agent=AgentConfig(max_turns=30),
                workspace=WorkspaceConfig(base_branch="master"),
            )
            with pytest.raises(RuntimeError) as exc_info:
                from host.launch import main
                main()

            assert "empty or invalid" in str(exc_info.value)
            assert "missing HEAD file" in str(exc_info.value)

    def test_git_overlay_validation_passes_with_head(self, tmp_path):
        """Validation passes when HEAD file exists."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        git_mount = session_dir / "git-merged"
        git_mount.mkdir()
        (git_mount / "HEAD").write_text("ref: refs/heads/master\n")

        # Should not raise when HEAD exists
        assert (git_mount / "HEAD").exists()
