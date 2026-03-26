"""Tracker client with watcher socket fallback.

Provides get_tracker_with_fallback() which CLI commands and launch.py
use instead of create_tracker(). Tries the watcher's Unix socket first
for zero-contention access; falls back to direct GitBugTracker if the
watcher is not running.
"""

import logging
import socket
from pathlib import Path

from core.config import create_tracker
from core.config.models import WorkflowConfig
from host.constants import TRACKER_SOCKET_FILENAME

log = logging.getLogger(__name__)


def _socket_path(repo_dir: str | Path) -> Path:
    """Derive the tracker socket path from the repo directory."""
    return Path(repo_dir) / ".nightshift" / TRACKER_SOCKET_FILENAME


def _probe_socket(sock_path: Path) -> bool:
    """Check if the tracker socket is accepting connections."""
    if not sock_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(str(sock_path))
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def get_tracker_with_fallback(config: WorkflowConfig, repo_dir: str | Path):
    """Get a tracker, preferring the watcher socket when available.

    If the watcher is running and its socket is accepting connections,
    returns a SocketTrackerClient for zero-contention access.
    Otherwise falls back to create_tracker() (direct GitBugTracker with
    lock retry).
    """
    sock_path = _socket_path(repo_dir)
    if _probe_socket(sock_path):
        # Late import to avoid circular dependency — socket_client imports
        # from core but host modules import from adapters
        from adapters.trackers.socket_client import SocketTrackerClient
        log.debug("Using tracker socket at %s", sock_path)
        return SocketTrackerClient(sock_path)

    log.debug("Tracker socket not available, using direct tracker")
    return create_tracker(config, repo_dir=str(repo_dir))
