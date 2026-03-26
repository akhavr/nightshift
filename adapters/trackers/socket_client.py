"""Socket-based tracker client for single-writer architecture.

Implements IssueTracker by sending JSON-lines requests over a Unix domain
socket to the watcher's TrackerSocketServer. Used by CLI commands and
launch.py when the watcher is running.
"""

import logging
import socket
from pathlib import Path

from core.constants import TRACKER_IPC_TIMEOUT_S
from core.tracker_ipc import (
    TrackerRequest, TrackerResponse, TrackerIPCBase,
    recv_json_line,
)

log = logging.getLogger(__name__)


class TrackerUnavailableError(Exception):
    """Raised when the tracker socket is not available (watcher not running)."""


class SocketTrackerClient(TrackerIPCBase):
    """IssueTracker implementation that communicates via Unix socket.

    Each method call opens a short-lived connection to the watcher's
    socket server, sends a JSON-lines request, and reads the response.
    """

    def __init__(self, socket_path: str | Path):
        self._socket_path = str(socket_path)

    def _call(self, method: str, **kwargs) -> TrackerResponse:
        """Send a request and return the response."""
        request = TrackerRequest(method=method, args=kwargs)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(TRACKER_IPC_TIMEOUT_S)
            sock.connect(self._socket_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            sock.close()
            raise TrackerUnavailableError(
                f"Cannot connect to tracker socket at {self._socket_path}: {e}"
            ) from e

        try:
            sock.sendall((request.to_json() + "\n").encode())
            data = recv_json_line(sock)
            if not data:
                return TrackerResponse(id=request.id, ok=False,
                                       error="Empty response from server")
            return TrackerResponse.from_json(data)
        except socket.timeout:
            return TrackerResponse(id=request.id, ok=False,
                                   error="Socket request timed out")
        except Exception as e:
            return TrackerResponse(id=request.id, ok=False,
                                   error=f"Socket communication error: {e}")
        finally:
            sock.close()
