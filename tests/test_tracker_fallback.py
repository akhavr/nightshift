"""Tests for host/tracker_client.py — fallback logic."""

import socket
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from host.tracker_client import get_tracker_with_fallback, _probe_socket, _socket_path
from host.constants import TRACKER_SOCKET_FILENAME


class TestSocketPath:
    def test_derives_path(self, tmp_path):
        result = _socket_path(tmp_path)
        assert result == tmp_path / ".nightshift" / TRACKER_SOCKET_FILENAME


class TestProbeSocket:
    def test_returns_false_when_no_file(self, tmp_path):
        assert _probe_socket(tmp_path / "nonexistent.sock") is False

    def test_returns_false_for_regular_file(self, tmp_path):
        f = tmp_path / "not-a-socket"
        f.write_text("nope")
        assert _probe_socket(f) is False

    def test_returns_true_for_listening_socket(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        try:
            assert _probe_socket(sock_path) is True
        finally:
            server.close()

    def test_returns_false_for_stale_socket(self, tmp_path):
        sock_path = tmp_path / "stale.sock"
        # Create a socket, bind it, then close it (stale)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.close()
        # File still exists but no one is listening
        assert _probe_socket(sock_path) is False


class TestGetTrackerWithFallback:
    def test_uses_socket_when_available(self, tmp_path):
        sock_path = tmp_path / ".nightshift" / TRACKER_SOCKET_FILENAME
        sock_path.parent.mkdir(parents=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        try:
            config = MagicMock()
            result = get_tracker_with_fallback(config, tmp_path)
            from adapters.trackers.socket_client import SocketTrackerClient
            assert isinstance(result, SocketTrackerClient)
        finally:
            server.close()

    def test_falls_back_to_direct_tracker(self, tmp_path):
        config = MagicMock()
        mock_tracker = MagicMock()

        with patch("host.tracker_client.create_tracker", return_value=mock_tracker):
            result = get_tracker_with_fallback(config, tmp_path)
            assert result is mock_tracker

    def test_falls_back_when_socket_stale(self, tmp_path):
        sock_path = tmp_path / ".nightshift" / TRACKER_SOCKET_FILENAME
        sock_path.parent.mkdir(parents=True)
        # Create a stale socket file
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.close()

        config = MagicMock()
        mock_tracker = MagicMock()

        with patch("host.tracker_client.create_tracker", return_value=mock_tracker):
            result = get_tracker_with_fallback(config, tmp_path)
            assert result is mock_tracker


class TestDeadSocketDetection:
    """Tests for stale/dead socket detection (kill -9 scenario)."""

    def test_dead_socket_detected_on_probe(self, tmp_path):
        """Probe returns False for a socket file with no listener.

        Simulates kill -9: server accepts connection then dies (EOF).
        """
        sock_path = tmp_path / "dead.sock"
        # Create a server that accepts one connection then immediately closes
        # (simulating a process that died — client sees EOF)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def accept_and_close():
            conn, _ = server.accept()
            conn.close()  # Immediate close = EOF for client

        t = threading.Thread(target=accept_and_close)
        t.start()

        assert _probe_socket(sock_path) is False

        t.join(timeout=2)
        server.close()

    def test_stale_socket_file_removed_on_probe(self, tmp_path):
        """Stale socket file is cleaned up after failed probe."""
        sock_path = tmp_path / "stale.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.close()

        assert sock_path.exists()
        _probe_socket(sock_path)
        # Socket file should be removed after detecting it's stale
        assert not sock_path.exists()

    def test_stale_socket_falls_back_quickly(self, tmp_path):
        """When socket exists but no process listens,
        get_tracker_with_fallback() returns direct tracker within 2s."""
        sock_path = tmp_path / ".nightshift" / TRACKER_SOCKET_FILENAME
        sock_path.parent.mkdir(parents=True)

        # Create a server that accepts then immediately closes (simulates
        # a watcher that was killed — connection succeeds but EOF on read)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def accept_and_close():
            conn, _ = server.accept()
            conn.close()

        t = threading.Thread(target=accept_and_close)
        t.start()

        config = MagicMock()
        mock_tracker = MagicMock()

        start = time.monotonic()
        with patch("host.tracker_client.create_tracker", return_value=mock_tracker):
            result = get_tracker_with_fallback(config, tmp_path)
        elapsed = time.monotonic() - start

        assert result is mock_tracker
        assert elapsed < 2.0, f"Fallback took {elapsed:.1f}s, expected < 2s"

        t.join(timeout=2)
        server.close()
