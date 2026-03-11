"""Tests for host/docker_utils.py."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.docker_utils import (
    docker_pause, docker_unpause, docker_stop, docker_container_status,
)


class TestDockerPause:
    def test_pause_success(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert docker_pause("test-container") is True
            mock_run.assert_called_once_with(
                ["docker", "pause", "test-container"], capture_output=True,
            )

    def test_pause_failure(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert docker_pause("test-container") is False


class TestDockerUnpause:
    def test_unpause_success(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert docker_unpause("test-container") is True

    def test_unpause_failure(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert docker_unpause("test-container") is False


class TestDockerStop:
    def test_stop_success(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert docker_stop("test-container") is True

    def test_stop_failure(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert docker_stop("test-container") is False


class TestDockerContainerStatus:
    def test_running(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="running\n")
            assert docker_container_status("test") == "running"

    def test_paused(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="paused\n")
            assert docker_container_status("test") == "paused"

    def test_not_found(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert docker_container_status("test") is None

    def test_inspect_command(self):
        with patch("host.docker_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="running")
            docker_container_status("my-container")
            args = mock_run.call_args[0][0]
            assert "docker" in args
            assert "inspect" in args
            assert "my-container" in args
