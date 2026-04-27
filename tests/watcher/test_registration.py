"""Tests for watcher auto-registration in projects.d."""

import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from host.watcher.registration import register, unregister, PROJECTS_D


class TestRegistration:
    def setup_method(self):
        self.test_projects_d = None

    def teardown_method(self):
        if self.test_projects_d and self.test_projects_d.exists():
            for f in self.test_projects_d.iterdir():
                f.unlink()
            self.test_projects_d.rmdir()

    def test_watcher_creates_registration_file(self, tmp_path):
        """On watcher start, ~/.nightshift/projects.d/{project_name}.yaml is created."""
        projects_d = tmp_path / "projects.d"
        repo_path = tmp_path / "repo"
        log_path = tmp_path / "watcher.log"

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            reg_file = register("my-project", repo_path, log_path)

        assert reg_file.exists()
        assert reg_file.name == "my-project.yaml"
        assert reg_file.parent == projects_d

    def test_registration_file_contains_required_fields(self, tmp_path):
        """File contains path, log, pid, started fields."""
        projects_d = tmp_path / "projects.d"
        repo_path = tmp_path / "repo"
        log_path = tmp_path / "watcher.log"

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            reg_file = register("my-project", repo_path, log_path)

        content = yaml.safe_load(reg_file.read_text())

        assert "path" in content
        assert "log" in content
        assert "pid" in content
        assert "started" in content

        assert content["path"] == str(repo_path)
        assert content["log"] == str(log_path)
        assert content["pid"] == os.getpid()
        # YAML parser may auto-convert ISO timestamp to datetime
        assert isinstance(content["started"], (str, datetime))

    def test_watcher_removes_registration_on_clean_shutdown(self, tmp_path):
        """On graceful shutdown (SIGTERM), registration file is deleted."""
        projects_d = tmp_path / "projects.d"
        repo_path = tmp_path / "repo"
        log_path = tmp_path / "watcher.log"

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            register("my-project", repo_path, log_path)
            reg_file = projects_d / "my-project.yaml"
            assert reg_file.exists()

            unregister("my-project")
            assert not reg_file.exists()

    def test_registration_survives_crash(self, tmp_path):
        """On crash (no cleanup), file remains for watchdog stale detection."""
        projects_d = tmp_path / "projects.d"
        repo_path = tmp_path / "repo"
        log_path = tmp_path / "watcher.log"

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            reg_file = register("my-project", repo_path, log_path)

        # Simulating crash: no unregister() call
        # File should still exist
        assert reg_file.exists()

        # Verify PID is recorded so watchdog can detect staleness
        content = yaml.safe_load(reg_file.read_text())
        assert content["pid"] == os.getpid()

    def test_unregister_missing_ok(self, tmp_path):
        """Unregister does not raise if file doesn't exist."""
        projects_d = tmp_path / "projects.d"
        projects_d.mkdir()

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            # Should not raise
            unregister("nonexistent-project")

    def test_register_creates_projects_d_if_missing(self, tmp_path):
        """Register creates ~/.nightshift/projects.d/ if it doesn't exist."""
        projects_d = tmp_path / "nonexistent" / "projects.d"
        repo_path = tmp_path / "repo"
        log_path = tmp_path / "watcher.log"

        assert not projects_d.exists()

        with patch.object(
            sys.modules["host.watcher.registration"], "PROJECTS_D", projects_d
        ):
            register("my-project", repo_path, log_path)

        assert projects_d.exists()
