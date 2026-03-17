"""Docker container management utilities."""

import logging
import subprocess

log = logging.getLogger(__name__)


def docker_pause(container: str) -> bool:
    """Pause a Docker container. Returns True on success."""
    return subprocess.run(
        ["docker", "pause", container], capture_output=True,
    ).returncode == 0


def docker_unpause(container: str) -> bool:
    """Unpause a Docker container. Returns True on success."""
    return subprocess.run(
        ["docker", "unpause", container], capture_output=True,
    ).returncode == 0


def docker_stop(container: str) -> bool:
    """Stop a Docker container. Returns True on success."""
    return subprocess.run(
        ["docker", "stop", container], capture_output=True,
    ).returncode == 0


def docker_remove(container: str) -> bool:
    """Force-remove a Docker container. Returns True on success.

    This is a no-op if the container doesn't exist (docker rm -f exits 0).
    """
    return subprocess.run(
        ["docker", "rm", "-f", container], capture_output=True,
    ).returncode == 0


def docker_container_status(container: str) -> str | None:
    """Get the status of a Docker container (running, paused, etc.).

    Returns None if the container doesn't exist.
    """
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None
