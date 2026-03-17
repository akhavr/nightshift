"""Tests for host/config_discovery.py — workflow file discovery."""

import argparse
import subprocess

import pytest
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

from host.config_discovery import (
    discover_workflow,
    write_local_config,
    LOCAL_CONFIG_FILENAME,
    DEFAULT_WORKFLOW_FILENAME,
    _read_workflow_pointer,
)


def _init_repo(tmp_path):
    """Create a git repo with an initial commit on main."""
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
    return repo


# ── discover_workflow: discovery order ────────────────────────────────────────


class TestDiscoveryOrder:
    """Discovery order: CLI flag > .nightshift.yaml > WORKFLOW.md."""

    def test_cli_override_wins_over_all(self, tmp_path):
        """CLI --workflow flag takes highest priority."""
        repo = tmp_path / "repo"
        repo.mkdir()

        cli_wf = tmp_path / "cli-workflow.md"
        cli_wf.write_text("---\n---\ncli")

        pointer_wf = tmp_path / "pointer-workflow.md"
        pointer_wf.write_text("---\n---\npointer")
        (repo / LOCAL_CONFIG_FILENAME).write_text(f"workflow: {pointer_wf}\n")

        (repo / DEFAULT_WORKFLOW_FILENAME).write_text("---\n---\ndefault")

        result = discover_workflow(repo, cli_override=str(cli_wf))
        assert result == cli_wf.resolve()

    def test_local_config_wins_over_default(self, tmp_path):
        """.nightshift.yaml pointer takes priority over WORKFLOW.md."""
        repo = tmp_path / "repo"
        repo.mkdir()

        pointer_wf = tmp_path / "pointer-workflow.md"
        pointer_wf.write_text("---\n---\npointer")
        (repo / LOCAL_CONFIG_FILENAME).write_text(f"workflow: {pointer_wf}\n")

        (repo / DEFAULT_WORKFLOW_FILENAME).write_text("---\n---\ndefault")

        result = discover_workflow(repo)
        assert result == pointer_wf.resolve()

    def test_default_workflow_used_when_no_other_source(self, tmp_path):
        """Falls back to WORKFLOW.md when no CLI flag or .nightshift.yaml."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / DEFAULT_WORKFLOW_FILENAME).write_text("---\n---\ndefault")

        result = discover_workflow(repo)
        assert result == repo / DEFAULT_WORKFLOW_FILENAME

    def test_exits_when_no_workflow_found(self, tmp_path, capsys):
        """Exits with clear error when no workflow file is found."""
        repo = tmp_path / "repo"
        repo.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            discover_workflow(repo)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "No workflow file found" in err
        assert "nightshift init" in err

    def test_exits_when_cli_override_missing(self, tmp_path, capsys):
        """Exits when CLI --workflow points to a non-existent file."""
        repo = tmp_path / "repo"
        repo.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            discover_workflow(repo, cli_override="/nonexistent/workflow.md")
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_exits_when_pointer_target_missing(self, tmp_path, capsys):
        """Exits when .nightshift.yaml points to a non-existent file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / LOCAL_CONFIG_FILENAME).write_text("workflow: /nonexistent/wf.md\n")

        with pytest.raises(SystemExit) as exc_info:
            discover_workflow(repo)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert LOCAL_CONFIG_FILENAME in err

    def test_skips_invalid_local_config(self, tmp_path):
        """Falls back to WORKFLOW.md when .nightshift.yaml has no workflow key."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / LOCAL_CONFIG_FILENAME).write_text("other: value\n")
        (repo / DEFAULT_WORKFLOW_FILENAME).write_text("---\n---\ndefault")

        result = discover_workflow(repo)
        assert result == repo / DEFAULT_WORKFLOW_FILENAME


# ── _read_workflow_pointer ────────────────────────────────────────────────────


class TestReadWorkflowPointer:
    def test_reads_absolute_path(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("workflow: /abs/path/wf.md\n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result == Path("/abs/path/wf.md")

    def test_reads_relative_path(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("workflow: configs/workflow.md\n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result == (tmp_path / "configs" / "workflow.md").resolve()

    def test_expands_tilde(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("workflow: ~/my-workflow.md\n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result == Path("~/my-workflow.md").expanduser().resolve()

    def test_returns_none_for_missing_key(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("other_key: value\n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result is None

    def test_returns_none_for_invalid_yaml(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text(": invalid: yaml: [[[")
        result = _read_workflow_pointer(config, tmp_path)
        assert result is None

    def test_returns_none_for_non_dict(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("just a string\n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result is None

    def test_returns_none_for_empty_workflow_value(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("workflow: \n")
        result = _read_workflow_pointer(config, tmp_path)
        assert result is None


# ── write_local_config ────────────────────────────────────────────────────────


class TestWriteLocalConfig:
    def test_creates_new_config(self, tmp_path):
        result = write_local_config(tmp_path, "/path/to/workflow.md")
        assert result == tmp_path / LOCAL_CONFIG_FILENAME
        data = yaml.safe_load(result.read_text())
        assert data["workflow"] == "/path/to/workflow.md"

    def test_updates_existing_config(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text("other_key: preserved\nworkflow: old_path\n")
        write_local_config(tmp_path, "/new/path.md")
        data = yaml.safe_load(config.read_text())
        assert data["workflow"] == "/new/path.md"
        assert data["other_key"] == "preserved"

    def test_overwrites_corrupt_config(self, tmp_path):
        config = tmp_path / LOCAL_CONFIG_FILENAME
        config.write_text(": broken: yaml: [[[")
        write_local_config(tmp_path, "/path/to/workflow.md")
        data = yaml.safe_load(config.read_text())
        assert data["workflow"] == "/path/to/workflow.md"


# ── cmd_init --workflow-path ──────────────────────────────────────────────────


class TestCmdInitWorkflowPath:
    def test_init_with_workflow_path_creates_at_custom_location(self, tmp_path):
        """nightshift init --workflow-path creates file at custom location."""
        from host.cli import cmd_init

        repo = _init_repo(tmp_path)
        custom_path = tmp_path / "configs" / "my-workflow.md"

        with patch("host.cli.repo_root", return_value=repo):
            cmd_init(argparse.Namespace(force=False, workflow_path=str(custom_path)))

        assert custom_path.exists()
        assert "agent:" in custom_path.read_text()

        # Should also create .nightshift.yaml pointer
        local_config = repo / LOCAL_CONFIG_FILENAME
        assert local_config.exists()
        data = yaml.safe_load(local_config.read_text())
        assert data["workflow"] == str(custom_path)

    def test_init_without_workflow_path_uses_default(self, tmp_path):
        """nightshift init without --workflow-path creates WORKFLOW.md in repo root."""
        from host.cli import cmd_init

        repo = _init_repo(tmp_path)

        with patch("host.cli.repo_root", return_value=repo):
            cmd_init(argparse.Namespace(force=False, workflow_path=None))

        assert (repo / DEFAULT_WORKFLOW_FILENAME).exists()
        assert not (repo / LOCAL_CONFIG_FILENAME).exists()


# ── cmd_watcher passes --workflow ─────────────────────────────────────────────


class TestCmdWatcherWorkflowArg:
    def test_cmd_watcher_passes_workflow_to_subprocess(self, tmp_path):
        """cmd_watcher includes --workflow in the exec args."""
        from host.cli import cmd_watcher

        args = MagicMock()
        args.no_auto_start = False
        args.workflow = None

        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\n---\n")

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli.sessions_dir", return_value=tmp_path / "sessions"), \
             patch("host.cli.os.execvpe") as mock_exec:
            (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
            cmd_watcher(args)

            cmd = mock_exec.call_args[0][1]
            assert "--workflow" in cmd
            wf_idx = cmd.index("--workflow")
            assert cmd[wf_idx + 1] == str(wf)

    def test_cmd_watcher_prints_workflow_path(self, tmp_path, capsys):
        """cmd_watcher prints the resolved workflow path on startup."""
        from host.cli import cmd_watcher

        args = MagicMock()
        args.no_auto_start = False
        args.workflow = None

        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\n---\n")

        with patch("host.cli.repo_root", return_value=tmp_path), \
             patch("host.cli.sessions_dir", return_value=tmp_path / "sessions"), \
             patch("host.cli.os.execvpe"):
            (tmp_path / ".nightshift").mkdir(parents=True, exist_ok=True)
            cmd_watcher(args)

            captured = capsys.readouterr()
            assert f"Using workflow: {wf}" in captured.out
