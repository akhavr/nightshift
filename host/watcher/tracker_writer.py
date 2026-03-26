"""Single-writer tracker thread and Unix socket server.

Serializes all git-bug operations through one thread to eliminate lock
contention. The watcher's internal code uses QueueTrackerProxy (direct
queue submission). External CLI processes connect via the socket server.
"""

import logging
import os
import queue
import selectors
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from core.tracker_ipc import (
    TrackerRequest, TrackerResponse, TrackerIPCBase,
    execute_tracker_method,
    recv_json_line,
)
from host.constants import (
    TRACKER_SOCKET_FILENAME,
    TRACKER_WRITER_QUEUE_SIZE,
    TRACKER_SOCKET_MAX_WORKERS,
)

log = logging.getLogger(__name__)

# Sentinel to signal the writer thread to stop
_STOP = object()


class _PendingResult:
    """A future-like result slot for queue-based requests."""

    def __init__(self):
        self._event = threading.Event()
        self._response: TrackerResponse | None = None

    def set(self, response: TrackerResponse) -> None:
        self._response = response
        self._event.set()

    def wait(self, timeout: float | None = None) -> TrackerResponse | None:
        self._event.wait(timeout=timeout)
        return self._response


class TrackerWriter:
    """Processes tracker operations serially from a queue.

    All git-bug calls go through this single thread, eliminating lock
    contention between watcher internals, CLI commands, and outbox processing.
    """

    def __init__(self, tracker: Any, shutdown_event: threading.Event):
        self._tracker = tracker
        self._shutdown = shutdown_event
        self._queue: queue.Queue = queue.Queue(maxsize=TRACKER_WRITER_QUEUE_SIZE)
        self._thread = threading.Thread(target=self._run, name="tracker-writer",
                                        daemon=True)

    @property
    def tracker(self) -> Any:
        """The underlying tracker instance (for config reload swaps)."""
        return self._tracker

    @tracker.setter
    def tracker(self, new_tracker: Any) -> None:
        self._tracker = new_tracker

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal the writer thread to stop and wait for it to drain."""
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            log.warning("Writer queue full during stop, forcing stop")
            # Clear queue, resolving pending requests with errors
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    if item is not _STOP:
                        req, pending = item
                        pending.set(TrackerResponse(
                            id=req.id, ok=False,
                            error="Writer shutting down"))
                except queue.Empty:
                    break
            self._queue.put_nowait(_STOP)
        self._thread.join(timeout=10)

    def submit(self, request: TrackerRequest, timeout: float | None = None
               ) -> TrackerResponse:
        """Submit a request to the writer queue and wait for the result."""
        pending = _PendingResult()
        try:
            self._queue.put((request, pending), timeout=5)
        except queue.Full:
            return TrackerResponse(id=request.id, ok=False,
                                   error="Writer queue full")
        result = pending.wait(timeout=timeout)
        if result is None:
            return TrackerResponse(id=request.id, ok=False,
                                   error="Writer request timed out")
        return result

    def _run(self) -> None:
        """Worker loop: dequeue and execute one request at a time."""
        log.info("Tracker writer thread started")
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is _STOP:
                break

            request, pending = item
            try:
                response = execute_tracker_method(self._tracker, request)
            except Exception as e:
                log.error("Tracker writer unexpected error on %s: %s",
                          request.method, e)
                response = TrackerResponse(id=request.id, ok=False,
                                           error=str(e))
            pending.set(response)

        # Drain remaining items on shutdown
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            request, pending = item
            try:
                response = execute_tracker_method(self._tracker, request)
            except Exception as e:
                response = TrackerResponse(id=request.id, ok=False,
                                           error=str(e))
            pending.set(response)

        log.info("Tracker writer thread stopped")


class TrackerSocketServer:
    """Unix domain socket server that routes requests to TrackerWriter.

    Accepts JSON-line requests from CLI processes. Each connection is
    handled in a thread pool worker: read request, submit to writer
    queue, wait for result, send response.
    """

    def __init__(self, socket_path: Path, writer: TrackerWriter,
                 shutdown_event: threading.Event):
        self._socket_path = socket_path
        self._writer = writer
        self._shutdown = shutdown_event
        self._server_sock: socket.socket | None = None
        self._thread = threading.Thread(target=self._run,
                                        name="tracker-socket-server",
                                        daemon=True)
        self._pool = ThreadPoolExecutor(max_workers=TRACKER_SOCKET_MAX_WORKERS,
                                        thread_name_prefix="tracker-ipc")

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def start(self) -> None:
        # Remove stale socket file
        if self._socket_path.exists():
            log.info("Removing stale tracker socket: %s", self._socket_path)
            self._socket_path.unlink()

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self._socket_path))
        os.chmod(str(self._socket_path), 0o600)
        self._server_sock.listen(TRACKER_SOCKET_MAX_WORKERS)
        self._server_sock.setblocking(False)

        self._thread.start()
        log.info("Tracker socket server listening on %s", self._socket_path)

    def stop(self) -> None:
        """Shut down the socket server and clean up."""
        if self._server_sock:
            self._server_sock.close()
        self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)
        if self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)
        log.info("Tracker socket server stopped")

    def _run(self) -> None:
        """Accept loop using selectors for interruptible shutdown."""
        sel = selectors.DefaultSelector()
        try:
            sel.register(self._server_sock, selectors.EVENT_READ)
        except (ValueError, OSError):
            # Server socket already closed (race with stop())
            sel.close()
            return

        try:
            while not self._shutdown.is_set():
                try:
                    events = sel.select(timeout=0.2)
                except (ValueError, OSError):
                    # Socket closed during select (stop() called)
                    break
                for key, _ in events:
                    try:
                        conn, _ = self._server_sock.accept()
                        self._pool.submit(self._handle_connection, conn)
                    except OSError as e:
                        if not self._shutdown.is_set():
                            log.warning("Socket accept error: %s", e)
        finally:
            sel.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        """Handle a single client connection: read request, submit, respond."""
        try:
            conn.settimeout(30)
            data = recv_json_line(conn)
            if not data:
                return

            request = TrackerRequest.from_json(data)
            response = self._writer.submit(request, timeout=60)
            conn.sendall((response.to_json() + "\n").encode())
        except Exception as e:
            log.warning("Socket connection handler error: %s", e)
            try:
                err = TrackerResponse(id="unknown", ok=False, error=str(e))
                conn.sendall((err.to_json() + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()


class QueueTrackerProxy(TrackerIPCBase):
    """IssueTracker implementation that routes all calls through TrackerWriter.

    Used by the watcher's internal code so it shares the single writer
    without going through the Unix socket.
    """

    def __init__(self, writer: TrackerWriter):
        self._writer = writer

    def _call(self, method: str, **kwargs) -> TrackerResponse:
        request = TrackerRequest(method=method, args=kwargs)
        return self._writer.submit(request, timeout=60)
