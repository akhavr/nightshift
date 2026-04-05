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
    """Check if the tracker socket has a live listener.

    After connecting, attempts a short recv to distinguish a live server
    (recv times out — no unsolicited data) from a stale socket (recv
    returns EOF immediately because no process owns the other end).
    If the socket is stale, the file is removed to prevent future hangs.
    """
    if not sock_path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2)
        s.connect(str(sock_path))
        # Verify the server is alive: a live server sends nothing
        # unsolicited, so recv() will timeout. A dead/stale socket
        # returns EOF (empty bytes) immediately.
        s.settimeout(0.5)
        try:
            data = s.recv(1)
            # Empty bytes = EOF = no process on the other end
            if data == b"":
                log.debug("Stale tracker socket (EOF on probe), removing %s",
                          sock_path)
                _remove_stale_socket(sock_path)
                return False
            # Got unexpected data — treat as alive
            return True
        except socket.timeout:
            # Timeout = server is alive but sent nothing (expected)
            return True
        except OSError as e:
            log.debug("Probe recv failed: %s", e)
            _remove_stale_socket(sock_path)
            return False
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        _remove_stale_socket(sock_path)
        return False
    finally:
        s.close()


def _remove_stale_socket(sock_path: Path) -> None:
    """Remove a stale socket file, logging any errors."""
    try:
        sock_path.unlink(missing_ok=True)
        log.debug("Removed stale socket file %s", sock_path)
    except OSError as e:
        log.warning("Could not remove stale socket %s: %s", sock_path, e)


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
    # Repair corrupt lamport clocks before direct git-bug access.
    # When the watcher is running (socket path above), it already repairs
    # on startup, so this only runs in the fallback path.
    from adapters.trackers.git_bug import repair_lamport_clocks
    repair_lamport_clocks(repo_dir)
    return create_tracker(config, repo_dir=str(repo_dir))
