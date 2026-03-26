"""Tests for adapters/trackers/socket_client.py — Unix socket tracker client."""

import json
import os
import socket
import threading
import pytest

from core.protocols import TrackerIssue, TrackerComment
from core.tracker_ipc import TrackerRequest, TrackerResponse, serialize_tracker_issue
from adapters.trackers.socket_client import SocketTrackerClient
from tests.conftest import make_test_issue


def _start_mock_server(sock_path, handler, shutdown_event):
    """Start a simple mock socket server that calls handler(request_json) -> response_json."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(2)
    server.settimeout(2)

    def serve():
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
                    conn.sendall((resp_json + "\n").encode())
            except Exception:
                pass
            finally:
                conn.close()
        server.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


class TestSocketTrackerClient:
    def test_get_issue_success(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()
        issue = make_test_issue(issue_id="abc", title="Hello")

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            resp = TrackerResponse(id=req.id, ok=True,
                                   result=serialize_tracker_issue(issue))
            return resp.to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            result = client.get_issue("abc")
            assert result is not None
            assert result.id == "abc"
            assert result.title == "Hello"
        finally:
            shutdown.set()

    def test_add_comment(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()
        received = {}

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            received.update(req.args)
            return TrackerResponse(id=req.id, ok=True).to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            client.add_comment("i1", "hello world")
            assert received["issue_id"] == "i1"
            assert received["body"] == "hello world"
        finally:
            shutdown.set()

    def test_list_issues(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()
        issues = [make_test_issue(issue_id="i1"), make_test_issue(issue_id="i2")]

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            result = [serialize_tracker_issue(i) for i in issues]
            return TrackerResponse(id=req.id, ok=True, result=result).to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            result = client.list_issues()
            assert len(result) == 2
        finally:
            shutdown.set()

    def test_run_raw(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            return TrackerResponse(id=req.id, ok=True,
                                   result="raw output").to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            result = client.run_raw("bug", "show", "abc")
            assert result == "raw output"
        finally:
            shutdown.set()

    def test_connection_refused_returns_error_response(self, tmp_path):
        """Connection failure returns error TrackerResponse, not exception."""
        sock_path = tmp_path / "nonexistent.sock"
        client = SocketTrackerClient(sock_path)
        resp = client._call("get_issue", issue_id="abc")
        assert not resp.ok
        assert "Tracker unavailable" in resp.error

    def test_connection_refused_degrades_gracefully(self, tmp_path):
        """TrackerIPCBase methods return empty/None when socket unavailable."""
        sock_path = tmp_path / "nonexistent.sock"
        client = SocketTrackerClient(sock_path)
        assert client.get_issue("abc") is None
        assert client.list_issues() == []
        assert client.get_comments("abc") == []
        assert client.run_raw("bug", "show") == ""

    def test_server_error_response(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            return TrackerResponse(id=req.id, ok=False,
                                   error="internal error").to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            # Error responses don't raise, they return empty/None
            result = client.get_issue("abc")
            assert result is None
        finally:
            shutdown.set()

    def test_sync(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()
        called = []

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            called.append(req.method)
            return TrackerResponse(id=req.id, ok=True).to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            client.sync()
            assert "sync" in called
        finally:
            shutdown.set()

    def test_set_status(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        shutdown = threading.Event()
        received = {}

        def handler(req_json):
            req = TrackerRequest.from_json(req_json)
            received.update(req.args)
            return TrackerResponse(id=req.id, ok=True).to_json()

        _start_mock_server(sock_path, handler, shutdown)

        try:
            client = SocketTrackerClient(sock_path)
            client.set_status("i1", "closed")
            assert received["status"] == "closed"
        finally:
            shutdown.set()
