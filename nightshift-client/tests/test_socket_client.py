"""Tests for nightshift_client._socket_client and NightshiftClient socket fallback."""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import MagicMock, patch

from nightshift_client import NightshiftClient
from nightshift_client._ipc import TrackerRequest, TrackerResponse
from nightshift_client._socket_client import SocketClient


def _start_mock_server(sock_path, handler, shutdown_event):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(2)
    server.settimeout(0.2)

    def serve():
        try:
            while not shutdown_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                try:
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        resp_json = handler(data.decode().strip())
                        if resp_json is not None:
                            conn.sendall((resp_json + "\n").encode())
                finally:
                    conn.close()
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


class TestSocketClient:
    def test_socket_client_call(self, tmp_path):
        sock_path = tmp_path / ".nightshift-client" / "tracker.sock"
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        shutdown = threading.Event()
        received = {}

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            received["method"] = req.method
            received["args"] = req.args
            return TrackerResponse(id=req.id, ok=True, result="issue-123").to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketClient(tmp_path)
            assert client.create_issue("Hello", "Body") == "issue-123"
            assert received == {
                "method": "create_issue",
                "args": {"title": "Hello", "body": "Body"},
            }
        finally:
            shutdown.set()

    def test_socket_client_timeout(self, tmp_path):
        sock_path = tmp_path / ".nightshift-client" / "tracker.sock"
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        shutdown = threading.Event()

        def handler(_req_json):
            time.sleep(1)
            return None

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketClient(tmp_path, timeout_s=0.1)
            resp = client._call("sync")
            assert not resp.ok
            assert "timed out" in resp.error.lower()
        finally:
            shutdown.set()

    def test_fallback_on_no_daemon(self, tmp_path):
        with patch("nightshift_client.probe_daemon_socket", return_value=False), \
             patch("nightshift_client.GitBug") as mock_gitbug:
            gitbug = mock_gitbug.return_value
            gitbug.add.return_value = "gitbug-123"
            client = NightshiftClient(repo_path=tmp_path, identity="user@example.com")

        assert client._socket_client is None
        assert client.create_issue("Title", "Body") == "gitbug-123"
        gitbug.add.assert_called_once()

    def test_auto_detect_daemon(self, tmp_path):
        socket_backend = MagicMock()
        socket_backend.create_issue.return_value = "socket-123"

        with patch("nightshift_client.probe_daemon_socket", return_value=True), \
             patch("nightshift_client.SocketClient", return_value=socket_backend) as mock_socket, \
             patch("nightshift_client.GitBug") as mock_gitbug:
            client = NightshiftClient(repo_path=tmp_path, identity="user@example.com")

        assert client._socket_client is socket_backend
        assert mock_socket.called
        assert mock_gitbug.called

        result = client.create_issue("Title", "Body")
        assert result == "socket-123"
        socket_backend.create_issue.assert_called_once_with("Title", "Body")
