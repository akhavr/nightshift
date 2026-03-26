"""Tests for host/watcher/tracker_writer.py — writer thread, socket server, queue proxy."""

import json
import os
import socket
import threading
import time
import pytest

from core.protocols import TrackerIssue, TrackerComment
from core.tracker_ipc import TrackerRequest, TrackerResponse
from host.watcher.tracker_writer import (
    TrackerWriter, TrackerSocketServer, QueueTrackerProxy,
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
