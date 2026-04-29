"""Tracker writer daemon for nightshift-client.

The daemon serializes git-bug operations through a single writer thread and
exposes them over a Unix domain socket. It mirrors the watcher-side tracker
writer architecture but stays self-contained inside the client package.
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import selectors
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.protocols import TrackerComment, TrackerIssue
from core.tracker_ipc import (
    TrackerRequest,
    TrackerResponse,
    execute_tracker_method,
    recv_json_line,
)

from nightshift_client._gitbug import GitBug

log = logging.getLogger(__name__)

CLIENT_DIRNAME = ".nightshift-client"
SOCKET_FILENAME = "tracker.sock"
PID_FILENAME = "tracker.pid"
QUEUE_SIZE = 100
SOCKET_WORKERS = 4

_STOP = object()


def daemon_dir_for(repo_path: str | Path) -> Path:
    return Path(repo_path) / CLIENT_DIRNAME


def socket_path_for(repo_path: str | Path) -> Path:
    return daemon_dir_for(repo_path) / SOCKET_FILENAME


def pidfile_path_for(repo_path: str | Path) -> Path:
    return daemon_dir_for(repo_path) / PID_FILENAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pidfile(path: str | Path) -> int | None:
    pidfile = Path(path)
    try:
        value = pidfile.read_text().strip()
    except FileNotFoundError:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def write_pidfile(path: str | Path, pid: int) -> None:
    pidfile = Path(path)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{pid}\n")


def remove_pidfile(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def remove_socket(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def pidfile_running(path: str | Path) -> bool:
    pid = read_pidfile(path)
    return bool(pid and _pid_alive(pid))


def _issue_from_dict(data: dict[str, Any]) -> TrackerIssue:
    return TrackerIssue(
        id=str(data.get("id", "")),
        identifier=str(data.get("identifier") or data.get("id", "")),
        title=str(data.get("title", "")),
        body=str(data.get("body", "")),
        status=str(data.get("status", "")),
        labels=list(data.get("labels") or []),
        url=data.get("url"),
        priority=data.get("priority"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


class _GitBugTrackerAdapter:
    """Adapter that presents the tracker IPC interface on top of GitBug."""

    def __init__(self, repo_path: str | Path):
        self._gitbug = GitBug(repo_path)

    def create_issue(self, title: str, body: str) -> str:
        labels = ["nightshift"]
        return self._gitbug.add(title, body, labels=labels)

    def get_issue(self, issue_id: str) -> TrackerIssue | None:
        try:
            return _issue_from_dict(self._gitbug.show(issue_id))
        except Exception:
            log.exception("git-bug show failed for %s", issue_id)
            return None

    def list_issues(self, status=None) -> list[TrackerIssue]:
        try:
            issues = [_issue_from_dict(item) for item in self._gitbug.list()]
        except Exception:
            log.exception("git-bug list failed")
            return []
        if status is None:
            return issues
        if isinstance(status, str):
            return [issue for issue in issues if issue.status == status]
        return [issue for issue in issues if issue.status in set(status)]

    def get_comments(self, issue_id: str) -> list[TrackerComment]:
        issue = self._gitbug.show(issue_id)
        comments = issue.get("comments", []) if issue else []
        return [
            TrackerComment(
                author=str(comment.get("author", {}).get("name", "")),
                body=str(comment.get("message", "")),
                created_at=comment.get("timestamp"),
            )
            for comment in comments
        ]

    def add_comment(self, issue_id: str, body: str) -> None:
        self._gitbug.comment(issue_id, body)

    def set_status(self, issue_id: str, status: str) -> None:
        if status == "closed":
            self._gitbug._run("bug", "status", "close", issue_id)
            return
        if status == "open":
            self._gitbug._run("bug", "status", "open", issue_id)
            return
        self._gitbug.label(issue_id, f"status:{status}")

    def add_label(self, issue_id: str, label: str) -> None:
        self._gitbug.label(issue_id, label)

    def remove_label(self, issue_id: str, label: str) -> None:
        self._gitbug._run("bug", "label", "rm", issue_id, label, ignore_rc={1})

    def sync(self) -> None:
        self._gitbug.pull()
        self._gitbug.push()

    def run_raw(self, *args: str) -> str:
        return self._gitbug._run(*args)


@dataclass
class _PendingResult:
    event: threading.Event
    response: TrackerResponse | None = None

    def set(self, response: TrackerResponse) -> None:
        self.response = response
        self.event.set()

    def wait(self, timeout: float | None = None) -> TrackerResponse | None:
        self.event.wait(timeout=timeout)
        return self.response


class TrackerWriterDaemon:
    """Single-writer daemon with a Unix socket front-end."""

    def __init__(
        self,
        repo_path: str | Path,
        tracker: Any | None = None,
        *,
        socket_path: str | Path | None = None,
        pidfile_path: str | Path | None = None,
    ):
        self.repo_path = Path(repo_path)
        self.socket_path = Path(socket_path) if socket_path else socket_path_for(repo_path)
        self.pidfile_path = Path(pidfile_path) if pidfile_path else pidfile_path_for(repo_path)
        self.tracker = tracker or _GitBugTrackerAdapter(self.repo_path)
        self._shutdown = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._writer_thread = threading.Thread(
            target=self._run_writer,
            name="nightshift-client-writer",
            daemon=True,
        )
        self._server_thread = threading.Thread(
            target=self._run_server,
            name="nightshift-client-socket",
            daemon=True,
        )
        self._server_sock: socket.socket | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=SOCKET_WORKERS,
            thread_name_prefix="nightshift-client-ipc",
        )

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        remove_socket(self.socket_path)
        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._server_sock.listen(SOCKET_WORKERS)
        self._server_sock.settimeout(0.2)
        write_pidfile(self.pidfile_path, os.getpid())
        self._writer_thread.start()
        self._server_thread.start()
        log.info("nightshift-client daemon started on %s", self.socket_path)

    def stop(self) -> None:
        self._shutdown.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is _STOP:
                    continue
                request, pending = item
                pending.set(TrackerResponse(
                    id=request.id,
                    ok=False,
                    error="daemon shutting down",
                ))
            self._queue.put_nowait(_STOP)

        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=10)
        if self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
        self._pool.shutdown(wait=True, cancel_futures=True)
        remove_socket(self.socket_path)
        remove_pidfile(self.pidfile_path)
        log.info("nightshift-client daemon stopped")

    def submit(self, request: TrackerRequest, timeout: float | None = None) -> TrackerResponse:
        pending = _PendingResult(event=threading.Event())
        try:
            self._queue.put((request, pending), timeout=5)
        except queue.Full:
            return TrackerResponse(id=request.id, ok=False, error="Writer queue full")
        result = pending.wait(timeout=timeout)
        if result is None:
            return TrackerResponse(id=request.id, ok=False, error="Writer request timed out")
        return result

    def is_running(self) -> bool:
        pid = read_pidfile(self.pidfile_path)
        return bool(pid and _pid_alive(pid) and self.socket_path.exists())

    def is_alive(self) -> bool:
        """Compatibility alias for watcher-style daemon checks."""
        return self.is_running()

    def _run_writer(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is _STOP:
                break
            request, pending = item
            try:
                response = execute_tracker_method(self.tracker, request)
            except Exception as exc:  # pragma: no cover - defensive
                response = TrackerResponse(id=request.id, ok=False, error=str(exc))
            pending.set(response)

        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            request, pending = item
            try:
                response = execute_tracker_method(self.tracker, request)
            except Exception as exc:  # pragma: no cover - defensive
                response = TrackerResponse(id=request.id, ok=False, error=str(exc))
            pending.set(response)

    def _run_server(self) -> None:
        if self._server_sock is None:
            return
        sel = selectors.DefaultSelector()
        try:
            sel.register(self._server_sock, selectors.EVENT_READ)
        except (OSError, ValueError):
            sel.close()
            return

        try:
            while not self._shutdown.is_set():
                try:
                    events = sel.select(timeout=0.2)
                except (OSError, ValueError):
                    break
                for key, _ in events:
                    try:
                        conn, _ = self._server_sock.accept()
                    except OSError as exc:
                        if exc.errno == errno.EBADF:
                            return
                        if not self._shutdown.is_set():
                            log.warning("daemon accept failed: %s", exc)
                        continue
                    self._pool.submit(self._handle_connection, conn)
        finally:
            sel.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2)
            data = recv_json_line(conn)
            if not data:
                return
            request = TrackerRequest.from_json(data)
            response = self.submit(request, timeout=60)
            conn.sendall((response.to_json() + "\n").encode())
        except Exception as exc:
            try:
                conn.sendall((TrackerResponse(
                    id="unknown",
                    ok=False,
                    error=str(exc),
                ).to_json() + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()


def run_foreground(repo_path: str | Path, tracker: Any | None = None) -> int:
    """Run the daemon in the foreground until SIGTERM/SIGINT."""
    shutdown = threading.Event()

    def _handle_signal(signum, frame):  # pragma: no cover - signal handling
        shutdown.set()

    previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    daemon = TrackerWriterDaemon(repo_path, tracker=tracker)
    daemon.start()
    try:
        while not shutdown.wait(timeout=0.2):
            pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        daemon.stop()
    return 0
