"""Tests for host/watcher/tracker_writer.py — writer thread, socket server, queue proxy."""

import json
import os
import queue
import socket
import threading
import time
import pytest

from core.protocols import TrackerIssue, TrackerComment
from core.tracker_ipc import TrackerRequest, TrackerResponse
from host.watcher.tracker_writer import (
    TrackerWriter, TrackerSocketServer, QueueTrackerProxy,
    _PendingResult,
)
from tests.conftest import MockTracker, make_test_issue


class TestTrackerWriter:
    def test_submit_and_get_result(self):
        shutdown = threading.Event()
        tracker = MockTracker(issues={"i1": make_test_issue(issue_id="i1")})
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        try:
            req = TrackerRequest(method="get_issue", args={"issue_id": "i1"})
            resp = writer.submit(req, timeout=5)
            assert resp.ok
            assert resp.result["id"] == "i1"
        finally:
            shutdown.set()
            writer.stop()

    def test_operations_are_serialized(self):
        """Concurrent submissions should be processed one at a time."""
        shutdown = threading.Event()
        execution_order = []
        call_count = 0

        class SlowTracker(MockTracker):
            def add_comment(self, issue_id, body):
                nonlocal call_count
                call_count += 1
                execution_order.append(body)
                time.sleep(0.05)  # simulate slow operation
                super().add_comment(issue_id, body)

        tracker = SlowTracker(issues={"i1": make_test_issue(issue_id="i1")})
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        try:
            threads = []
            for i in range(5):
                req = TrackerRequest(method="add_comment",
                                     args={"issue_id": "i1", "body": f"msg-{i}"})
                t = threading.Thread(target=writer.submit, args=(req, 10))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=10)

            assert call_count == 5
            assert len(execution_order) == 5
        finally:
            shutdown.set()
            writer.stop()

    def test_shutdown_drains_queue(self):
        shutdown = threading.Event()
        tracker = MockTracker(issues={"i1": make_test_issue(issue_id="i1")})
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        # Submit and wait for it
        req = TrackerRequest(method="sync", args={})
        resp = writer.submit(req, timeout=5)
        assert resp.ok
        assert tracker.synced == 1

        shutdown.set()
        writer.stop()

    def test_tracker_swap(self):
        """Writer's underlying tracker can be swapped for config reload."""
        shutdown = threading.Event()
        tracker1 = MockTracker()
        tracker2 = MockTracker()
        writer = TrackerWriter(tracker1, shutdown)
        writer.start()

        try:
            req = TrackerRequest(method="sync", args={})
            writer.submit(req, timeout=5)
            assert tracker1.synced == 1
            assert tracker2.synced == 0

            writer.tracker = tracker2
            writer.submit(req, timeout=5)
            assert tracker1.synced == 1
            assert tracker2.synced == 1
        finally:
            shutdown.set()
            writer.stop()

    def test_error_in_tracker_method(self):
        shutdown = threading.Event()

        class FailTracker:
            def sync(self):
                raise RuntimeError("boom")

        writer = TrackerWriter(FailTracker(), shutdown)
        writer.start()

        try:
            req = TrackerRequest(method="sync", args={})
            resp = writer.submit(req, timeout=5)
            assert not resp.ok
            assert "boom" in resp.error
        finally:
            shutdown.set()
            writer.stop()

    def test_submit_returns_error_when_queue_full(self):
        """submit() returns error response when the queue is full."""
        shutdown = threading.Event()

        class BlockingTracker(MockTracker):
            def __init__(self):
                super().__init__()
                self.block = threading.Event()

            def sync(self):
                self.block.wait(timeout=10)
                super().sync()

        tracker = BlockingTracker()
        # Use a tiny queue (size 1) to make it easy to fill
        writer = TrackerWriter(tracker, shutdown)
        writer._queue = queue.Queue(maxsize=1)
        writer.start()

        try:
            # First request blocks the writer thread
            req1 = TrackerRequest(method="sync", args={})
            t = threading.Thread(target=writer.submit, args=(req1, 10))
            t.start()
            time.sleep(0.1)  # let it start processing

            # Second fills the queue
            req2 = TrackerRequest(method="sync", args={})
            writer._queue.put((req2, _PendingResult()), timeout=1)

            # Third should get queue-full error
            req3 = TrackerRequest(method="sync", args={})
            resp = writer.submit(req3, timeout=1)
            assert not resp.ok
            assert "queue full" in resp.error.lower()
        finally:
            tracker.block.set()
            shutdown.set()
            writer.stop()
            t.join(timeout=5)

    def test_submit_returns_error_on_timeout(self):
        """submit() returns timeout error when writer doesn't respond in time."""
        shutdown = threading.Event()

        class BlockingTracker(MockTracker):
            def sync(self):
                time.sleep(5)  # block longer than timeout

        writer = TrackerWriter(BlockingTracker(), shutdown)
        writer.start()

        try:
            req = TrackerRequest(method="sync", args={})
            resp = writer.submit(req, timeout=0.1)
            assert not resp.ok
            assert "timed out" in resp.error.lower()
        finally:
            shutdown.set()
            writer.stop()

    def test_stop_when_queue_full(self):
        """stop() clears queue and resolves pending requests when queue is full."""
        shutdown = threading.Event()

        class BlockingTracker(MockTracker):
            def __init__(self):
                super().__init__()
                self.block = threading.Event()

            def sync(self):
                self.block.wait(timeout=10)

        tracker = BlockingTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer._queue = queue.Queue(maxsize=1)
        writer.start()

        # Submit a request that blocks the writer
        req1 = TrackerRequest(method="sync", args={})
        t1 = threading.Thread(target=writer.submit, args=(req1, 10))
        t1.start()
        time.sleep(0.1)

        # Fill the queue completely
        pending = _PendingResult()
        req2 = TrackerRequest(method="sync", args={})
        writer._queue.put((req2, pending), timeout=1)

        # Now stop() must handle the full queue
        tracker.block.set()
        shutdown.set()
        writer.stop()
        t1.join(timeout=5)

        # Pending request should have been resolved (either executed or errored)
        result = pending.wait(timeout=1)
        assert result is not None

    def test_drain_processes_remaining_items(self):
        """After shutdown, remaining queued items are still processed."""
        shutdown = threading.Event()

        class SlowTracker(MockTracker):
            def __init__(self):
                super().__init__()
                self.call_count = 0
                self.gate = threading.Event()

            def sync(self):
                self.call_count += 1
                if self.call_count == 1:
                    self.gate.wait(timeout=5)
                super().sync()

        tracker = SlowTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        # First sync blocks the writer
        req1 = TrackerRequest(method="sync", args={})
        t1 = threading.Thread(target=writer.submit, args=(req1, 10))
        t1.start()
        time.sleep(0.1)

        # Queue a second request while writer is blocked
        req2 = TrackerRequest(method="sync", args={})
        pending2 = _PendingResult()
        writer._queue.put((req2, pending2))

        # Signal shutdown and unblock the first request
        shutdown.set()
        tracker.gate.set()
        writer.stop()
        t1.join(timeout=5)

        # The drain loop should have processed req2
        result = pending2.wait(timeout=1)
        assert result is not None
        assert result.ok
        assert tracker.synced == 2


class TestTrackerSocketServer:
    def test_end_to_end_via_socket(self, tmp_path):
        shutdown = threading.Event()
        tracker = MockTracker(issues={"i1": make_test_issue(issue_id="i1", title="Socket test")})
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        try:
            # Connect as a client
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(str(sock_path))

            req = TrackerRequest(method="get_issue", args={"issue_id": "i1"})
            client.sendall((req.to_json() + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            client.close()

            resp = TrackerResponse.from_json(data.decode().strip())
            assert resp.ok
            assert resp.result["title"] == "Socket test"
        finally:
            shutdown.set()
            server.stop()
            writer.stop()

    def test_stale_socket_cleanup(self, tmp_path):
        """Server removes stale socket file on startup."""
        sock_path = tmp_path / "tracker.sock"
        sock_path.write_text("stale")

        shutdown = threading.Event()
        tracker = MockTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        try:
            assert sock_path.exists()
            # Verify it's an actual socket, not the stale file
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(sock_path))
            client.close()
        finally:
            shutdown.set()
            server.stop()
            writer.stop()

    def test_socket_cleanup_on_stop(self, tmp_path):
        shutdown = threading.Event()
        tracker = MockTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()
        assert sock_path.exists()

        shutdown.set()
        server.stop()
        writer.stop()
        assert not sock_path.exists()

    def test_multiple_concurrent_clients(self, tmp_path):
        shutdown = threading.Event()
        tracker = MockTracker(issues={"i1": make_test_issue(issue_id="i1")})
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        results = []

        def client_call(idx):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect(str(sock_path))
                req = TrackerRequest(method="add_comment",
                                     args={"issue_id": "i1", "body": f"msg-{idx}"})
                s.sendall((req.to_json() + "\n").encode())
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                resp = TrackerResponse.from_json(data.decode().strip())
                results.append(resp.ok)
            except Exception as e:
                results.append(False)

        try:
            threads = [threading.Thread(target=client_call, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert len(results) == 4
            assert all(results)
            assert len(tracker.comments.get("i1", [])) == 4
        finally:
            shutdown.set()
            server.stop()
            writer.stop()


    def test_handle_connection_invalid_json(self, tmp_path):
        """Server sends error response when client sends invalid JSON."""
        shutdown = threading.Event()
        tracker = MockTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(str(sock_path))
            # Send invalid JSON
            client.sendall(b"not valid json\n")
            data = b""
            while b"\n" not in data:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            client.close()

            resp = TrackerResponse.from_json(data.decode().strip())
            assert not resp.ok
            assert resp.id == "unknown"
        finally:
            shutdown.set()
            server.stop()
            writer.stop()

    def test_handle_connection_empty_data(self, tmp_path):
        """Server handles client that connects and immediately disconnects."""
        shutdown = threading.Event()
        tracker = MockTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(sock_path))
            client.close()
            # Just verify no crash — server should continue running
            time.sleep(0.1)
            # Verify server is still operational
            client2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client2.settimeout(2)
            client2.connect(str(sock_path))
            req = TrackerRequest(method="sync", args={})
            client2.sendall((req.to_json() + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = client2.recv(65536)
                if not chunk:
                    break
                data += chunk
            client2.close()
            resp = TrackerResponse.from_json(data.decode().strip())
            assert resp.ok
        finally:
            shutdown.set()
            server.stop()
            writer.stop()

    def test_run_exits_when_socket_closed_before_select(self, tmp_path):
        """Socket server _run() handles socket closed during select."""
        shutdown = threading.Event()
        tracker = MockTracker()
        writer = TrackerWriter(tracker, shutdown)
        writer.start()

        sock_path = tmp_path / "tracker.sock"
        server = TrackerSocketServer(sock_path, writer, shutdown)
        server.start()

        # Close the server socket directly to trigger the error path
        server._server_sock.close()
        time.sleep(0.5)  # let the select loop hit the error

        # Server thread should exit cleanly
        shutdown.set()
        server.stop()
        writer.stop()


class TestQueueTrackerProxy:
    def setup_method(self):
        self.shutdown = threading.Event()
        self.tracker = MockTracker(issues={
            "i1": make_test_issue(issue_id="i1", title="Proxy test")
        })
        self.tracker.comments["i1"] = [TrackerComment(author="a", body="hi")]
        self.writer = TrackerWriter(self.tracker, self.shutdown)
        self.writer.start()
        self.proxy = QueueTrackerProxy(self.writer)

    def teardown_method(self):
        self.shutdown.set()
        self.writer.stop()

    def test_get_issue(self):
        result = self.proxy.get_issue("i1")
        assert result is not None
        assert result.title == "Proxy test"

    def test_get_issue_not_found(self):
        result = self.proxy.get_issue("nope")
        assert result is None

    def test_list_issues(self):
        result = self.proxy.list_issues()
        assert len(result) == 1

    def test_get_comments(self):
        result = self.proxy.get_comments("i1")
        assert len(result) == 1
        assert result[0].body == "hi"

    def test_add_comment(self):
        self.proxy.add_comment("i1", "new comment")
        assert len(self.tracker.comments["i1"]) == 2

    def test_set_status(self):
        self.proxy.set_status("i1", "closed")
        assert self.tracker.issues["i1"].status == "closed"

    def test_add_label(self):
        self.proxy.add_label("i1", "bug")
        assert "bug" in self.tracker.issues["i1"].labels

    def test_remove_label(self):
        self.tracker.issues["i1"].labels.append("rm-me")
        self.proxy.remove_label("i1", "rm-me")
        assert "rm-me" not in self.tracker.issues["i1"].labels

    def test_sync(self):
        self.proxy.sync()
        assert self.tracker.synced == 1

    def test_run_raw(self):
        # MockTracker doesn't have run_raw, so the writer will return error
        result = self.proxy.run_raw("bug", "show")
        # Since MockTracker has no run_raw, it should fail gracefully
        assert isinstance(result, str)
