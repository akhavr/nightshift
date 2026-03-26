"""Socket-based tracker client for single-writer architecture.

Implements IssueTracker by sending JSON-lines requests over a Unix domain
socket to the watcher's TrackerSocketServer. Used by CLI commands and
launch.py when the watcher is running.
"""

import logging
import socket
from pathlib import Path
from typing import Optional

from core.protocols import TrackerIssue, TrackerComment
from core.tracker_ipc import (
    TrackerRequest, TrackerResponse,
    deserialize_tracker_issue, deserialize_tracker_comment,
)
from host.constants import TRACKER_IPC_TIMEOUT_S

log = logging.getLogger(__name__)


class TrackerUnavailableError(Exception):
    """Raised when the tracker socket is not available (watcher not running)."""


class SocketTrackerClient:
    """IssueTracker implementation that communicates via Unix socket.

    Each method call opens a short-lived connection to the watcher's
    socket server, sends a JSON-lines request, and reads the response.
    """

    def __init__(self, socket_path: str | Path):
        self._socket_path = str(socket_path)

    def _call(self, method: str, **kwargs) -> TrackerResponse:
        """Send a request and return the response."""
        request = TrackerRequest(method=method, args=kwargs)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(TRACKER_IPC_TIMEOUT_S)
            sock.connect(self._socket_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            raise TrackerUnavailableError(
                f"Cannot connect to tracker socket at {self._socket_path}: {e}"
            ) from e

        try:
            sock.sendall((request.to_json() + "\n").encode())
            data = self._recv_line(sock)
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

    @staticmethod
    def _recv_line(sock: socket.socket) -> str:
        """Read data from socket until newline."""
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return buf.decode() if buf else ""
            buf += chunk
            if b"\n" in buf:
                return buf.split(b"\n", 1)[0].decode()

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        resp = self._call("get_issue", issue_id=issue_id)
        if not resp.ok:
            log.warning("SocketTrackerClient.get_issue failed: %s", resp.error)
            return None
        return deserialize_tracker_issue(resp.result)

    def list_issues(self, status=None) -> list[TrackerIssue]:
        resp = self._call("list_issues", status=status)
        if not resp.ok:
            log.warning("SocketTrackerClient.list_issues failed: %s", resp.error)
            return []
        return [deserialize_tracker_issue(d) for d in (resp.result or [])]

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        resp = self._call("get_comments", issue_id=issue_id)
        if not resp.ok:
            log.warning("SocketTrackerClient.get_comments failed: %s", resp.error)
            return []
        return [deserialize_tracker_comment(d) for d in (resp.result or [])]

    def add_comment(self, issue_id: str, body: str) -> None:
        resp = self._call("add_comment", issue_id=issue_id, body=body)
        if not resp.ok:
            log.warning("SocketTrackerClient.add_comment failed: %s", resp.error)

    def set_status(self, issue_id: str, status: str) -> None:
        resp = self._call("set_status", issue_id=issue_id, status=status)
        if not resp.ok:
            log.warning("SocketTrackerClient.set_status failed: %s", resp.error)

    def add_label(self, issue_id: str, label: str) -> None:
        resp = self._call("add_label", issue_id=issue_id, label=label)
        if not resp.ok:
            log.warning("SocketTrackerClient.add_label failed: %s", resp.error)

    def remove_label(self, issue_id: str, label: str) -> None:
        resp = self._call("remove_label", issue_id=issue_id, label=label)
        if not resp.ok:
            log.warning("SocketTrackerClient.remove_label failed: %s", resp.error)

    def sync(self) -> None:
        resp = self._call("sync")
        if not resp.ok:
            log.warning("SocketTrackerClient.sync failed: %s", resp.error)

    def run_raw(self, *args: str) -> str:
        resp = self._call("run_raw", raw_args=list(args))
        if not resp.ok:
            log.warning("SocketTrackerClient.run_raw failed: %s", resp.error)
            return ""
        return resp.result or ""
