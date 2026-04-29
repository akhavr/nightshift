"""Tests for nightshift_client._daemon and cli daemon commands."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nightshift-client" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.protocols import TrackerIssue
from core.tracker_ipc import TrackerRequest, TrackerResponse
from nightshift_client._daemon import TrackerWriterDaemon, socket_path_for, pidfile_path_for
from nightshift_client import cli


class _FakeProc:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


class _SerialTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    def _enter(self, name: str):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            assert self.active == 1
            self.calls.append(name)

    def _exit(self):
        with self._lock:
            self.active -= 1

    def create_issue(self, title: str, body: str):
        self._enter(title)
        try:
            time.sleep(0.05)
            return "issue-1"
        finally:
            self._exit()

    def get_issue(self, issue_id: str):
        self._enter(issue_id)
        try:
            time.sleep(0.05)
            return TrackerIssue(
                id=issue_id,
                identifier=issue_id,
                title="Title",
                body="Body",
                status="open",
                labels=["nightshift"],
            )
        finally:
            self._exit()

    def list_issues(self, status=None):
        return []

    def get_comments(self, issue_id: str):
        return []

    def add_comment(self, issue_id: str, body: str):
        self._enter(body)
        try:
            time.sleep(0.05)
        finally:
            self._exit()

    def set_status(self, issue_id: str, status: str):
        self._enter(status)
        try:
            time.sleep(0.05)
        finally:
            self._exit()

    def add_label(self, issue_id: str, label: str):
        self._enter(label)
        try:
            time.sleep(0.05)
        finally:
            self._exit()

    def remove_label(self, issue_id: str, label: str):
        self._enter(label)
        try:
            time.sleep(0.05)
        finally:
            self._exit()

    def sync(self):
        self._enter("sync")
        try:
            time.sleep(0.05)
        finally:
            self._exit()

    def run_raw(self, *args: str):
        return " ".join(args)


def _make_daemon(tmp_path, tracker=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    daemon = TrackerWriterDaemon(repo_path=repo, tracker=tracker or _SerialTracker())
    return repo, daemon


def test_daemon_serializes_operations(tmp_path):
    repo, daemon = _make_daemon(tmp_path)
    daemon.start()
    try:
        threads = []
        for i in range(5):
            req = TrackerRequest(
                method="add_comment",
                args={"issue_id": "abc", "body": f"msg-{i}"},
            )
            t = threading.Thread(target=daemon.submit, args=(req, 5))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        assert daemon.tracker.max_active == 1
        assert len(daemon.tracker.calls) == 5
    finally:
        daemon.stop()


def test_daemon_socket_server(tmp_path):
    repo, daemon = _make_daemon(tmp_path)
    daemon.start()
    try:
        req = TrackerRequest(method="get_issue", args={"issue_id": "abc"})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(socket_path_for(repo)))
            sock.sendall((req.to_json() + "\n").encode())

            data = b""
            while b"\n" not in data:
                data += sock.recv(65536)
            resp = TrackerResponse.from_json(data.split(b"\n", 1)[0].decode())

        assert resp.ok is True
        assert resp.result["title"] == "Title"
    finally:
        daemon.stop()


def test_daemon_start_stop(tmp_path):
    repo, _ = _make_daemon(tmp_path)
    pidfile = pidfile_path_for(repo)
    socket_path = socket_path_for(repo)

    alive = {4321: True}

    def fake_kill(pid, sig):
        if sig == 0:
            if not alive.get(pid):
                raise ProcessLookupError(pid)
            return None
        alive[pid] = False
        return None

    with patch("nightshift_client.cli.subprocess.Popen", return_value=_FakeProc(4321)), \
         patch("nightshift_client.cli.os.kill", side_effect=fake_kill):
        rc = cli.main(["daemon", "start", "--repo", str(repo)])
        assert rc == 0
        assert pidfile.exists()
        assert pidfile.read_text().strip() == "4321"

        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.write_text("")

        rc = cli.main(["daemon", "stop", "--repo", str(repo)])
        assert rc == 0
        assert not pidfile.exists()
        assert not socket_path.exists()


def test_daemon_pidfile(tmp_path):
    repo, _ = _make_daemon(tmp_path)
    pidfile = pidfile_path_for(repo)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("999999\n")

    with patch("nightshift_client.cli.os.kill", side_effect=ProcessLookupError):
        rc = cli.main(["daemon", "status", "--repo", str(repo)])

    assert rc == 1
    assert not pidfile.exists()
