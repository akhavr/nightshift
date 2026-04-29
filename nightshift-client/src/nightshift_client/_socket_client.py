"""Socket-based tracker client for the nightshift-client daemon."""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import Optional

from nightshift_client._ipc import (
    TrackerComment,
    TrackerIssue,
    TrackerRequest,
    TrackerResponse,
    recv_json_line,
)

log = logging.getLogger(__name__)

CLIENT_DIRNAME = ".nightshift-client"
SOCKET_FILENAME = "tracker.sock"
_REQUEST_TIMEOUT_S = 60.0
_PROBE_TIMEOUT_S = 0.2


def socket_path_for(repo_path: str | Path) -> Path:
    return Path(repo_path) / CLIENT_DIRNAME / SOCKET_FILENAME


def _remove_stale_socket(sock_path: Path) -> None:
    try:
        sock_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove stale socket %s: %s", sock_path, exc)


def probe_daemon_socket(sock_path: str | Path) -> bool:
    """Return True when the daemon socket is reachable and responsive."""
    sock_path = Path(sock_path)
    if not sock_path.exists():
        return False

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_PROBE_TIMEOUT_S)
        sock.connect(str(sock_path))
        try:
            sock.settimeout(_PROBE_TIMEOUT_S)
            data = sock.recv(1)
        except socket.timeout:
            return True
        if data == b"":
            _remove_stale_socket(sock_path)
            return False
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        _remove_stale_socket(sock_path)
        return False
    finally:
        sock.close()


def _deserialize_issue(data: dict | None) -> TrackerIssue | None:
    if data is None:
        return None
    return TrackerIssue(**data)


def _deserialize_comment(data: dict) -> TrackerComment:
    return TrackerComment(**data)


class TrackerIPCBase:
    """Shared tracker IPC method implementations."""

    def _call(self, method: str, **kwargs) -> TrackerResponse:
        raise NotImplementedError

    def create_issue(self, title: str, body: str) -> str:
        resp = self._call("create_issue", title=title, body=body)
        if not resp.ok:
            log.warning("%s.create_issue failed: %s", type(self).__name__, resp.error)
            return ""
        return resp.result or ""

    def get_issue(self, issue_id: str) -> Optional[TrackerIssue]:
        resp = self._call("get_issue", issue_id=issue_id)
        if not resp.ok:
            log.warning("%s.get_issue failed: %s", type(self).__name__, resp.error)
            return None
        return _deserialize_issue(resp.result)

    def list_issues(self, status=None) -> list[TrackerIssue]:
        resp = self._call("list_issues", status=status)
        if not resp.ok:
            log.warning("%s.list_issues failed: %s", type(self).__name__, resp.error)
            return []
        return [
            issue for item in (resp.result or [])
            if (issue := _deserialize_issue(item)) is not None
        ]

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        resp = self._call("get_comments", issue_id=issue_id)
        if not resp.ok:
            log.warning("%s.get_comments failed: %s", type(self).__name__, resp.error)
            return []
        return [_deserialize_comment(item) for item in (resp.result or [])]

    def add_comment(self, issue_id: str, body: str) -> None:
        resp = self._call("add_comment", issue_id=issue_id, body=body)
        if not resp.ok:
            log.warning("%s.add_comment failed: %s", type(self).__name__, resp.error)

    def set_status(self, issue_id: str, status: str) -> None:
        resp = self._call("set_status", issue_id=issue_id, status=status)
        if not resp.ok:
            log.warning("%s.set_status failed: %s", type(self).__name__, resp.error)

    def add_label(self, issue_id: str, label: str) -> None:
        resp = self._call("add_label", issue_id=issue_id, label=label)
        if not resp.ok:
            log.warning("%s.add_label failed: %s", type(self).__name__, resp.error)

    def remove_label(self, issue_id: str, label: str) -> None:
        resp = self._call("remove_label", issue_id=issue_id, label=label)
        if not resp.ok:
            log.warning("%s.remove_label failed: %s", type(self).__name__, resp.error)

    def sync(self) -> None:
        resp = self._call("sync")
        if not resp.ok:
            log.warning("%s.sync failed: %s", type(self).__name__, resp.error)

    def run_raw(self, *args: str) -> str:
        resp = self._call("run_raw", raw_args=list(args))
        if not resp.ok:
            log.warning("%s.run_raw failed: %s", type(self).__name__, resp.error)
            return ""
        return resp.result or ""


class SocketClient(TrackerIPCBase):
    """Tracker client that talks to the nightshift daemon over a Unix socket."""

    def __init__(self, repo_path: str | Path, timeout_s: float = _REQUEST_TIMEOUT_S):
        self._repo_path = Path(repo_path)
        self._socket_path = socket_path_for(self._repo_path)
        self._timeout_s = timeout_s

    def _call(self, method: str, **kwargs) -> TrackerResponse:
        request = TrackerRequest(method=method, args=kwargs)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout_s)
            sock.connect(str(self._socket_path))
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            sock.close()
            log.warning("Cannot connect to daemon socket at %s: %s",
                        self._socket_path, exc)
            return TrackerResponse(id=request.id, ok=False,
                                   error=f"Tracker unavailable: {exc}")

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
        except Exception as exc:
            return TrackerResponse(id=request.id, ok=False,
                                   error=f"Socket communication error: {exc}")
        finally:
            sock.close()
