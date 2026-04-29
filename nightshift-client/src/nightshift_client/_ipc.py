"""Local tracker IPC primitives for the nightshift-client package.

These mirror the shared core IPC types so the client package can be installed
independently without importing repo-root modules.
"""

from __future__ import annotations

import json
import logging
import socket
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TRACKER_IPC_MAX_MESSAGE_BYTES = 1_048_576


@dataclass
class TrackerIssue:
    id: str
    identifier: str
    title: str
    body: str
    status: str
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    priority: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class TrackerComment:
    author: str
    body: str
    created_at: str | None = None


@dataclass
class TrackerRequest:
    method: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrackerRequest":
        parsed = json.loads(data)
        return cls(method=parsed["method"], args=parsed.get("args", {}), id=parsed["id"])


@dataclass
class TrackerResponse:
    id: str
    ok: bool
    result: Any = None
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrackerResponse":
        parsed = json.loads(data)
        return cls(
            id=parsed["id"],
            ok=parsed["ok"],
            result=parsed.get("result"),
            error=parsed.get("error", ""),
        )


def serialize_tracker_issue(issue: TrackerIssue | None) -> dict | None:
    if issue is None:
        return None
    return asdict(issue)


def serialize_tracker_comment(comment: TrackerComment) -> dict:
    return asdict(comment)


def recv_json_line(sock: socket.socket) -> str:
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return buf.decode() if buf else ""
        buf += chunk
        if b"\n" in buf:
            return buf.split(b"\n", 1)[0].decode()
        if len(buf) > TRACKER_IPC_MAX_MESSAGE_BYTES:
            log.error(
                "recv_json_line: buffer exceeded %d bytes without newline, discarding",
                TRACKER_IPC_MAX_MESSAGE_BYTES,
            )
            return ""


def execute_tracker_method(tracker: Any, request: TrackerRequest) -> TrackerResponse:
    method = request.method
    args = request.args

    try:
        if method == "create_issue":
            result = tracker.create_issue(args["title"], args["body"])
            return TrackerResponse(id=request.id, ok=True, result=result)
        if method == "get_issue":
            result = tracker.get_issue(args["issue_id"])
            return TrackerResponse(id=request.id, ok=True, result=serialize_tracker_issue(result))
        if method == "list_issues":
            result = tracker.list_issues(status=args.get("status"))
            return TrackerResponse(
                id=request.id,
                ok=True,
                result=[serialize_tracker_issue(issue) for issue in result],
            )
        if method == "get_comments":
            result = tracker.get_comments(args["issue_id"])
            return TrackerResponse(
                id=request.id,
                ok=True,
                result=[serialize_tracker_comment(comment) for comment in result],
            )
        if method == "add_comment":
            tracker.add_comment(args["issue_id"], args["body"])
            return TrackerResponse(id=request.id, ok=True)
        if method == "set_status":
            tracker.set_status(args["issue_id"], args["status"])
            return TrackerResponse(id=request.id, ok=True)
        if method == "add_label":
            tracker.add_label(args["issue_id"], args["label"])
            return TrackerResponse(id=request.id, ok=True)
        if method == "remove_label":
            tracker.remove_label(args["issue_id"], args["label"])
            return TrackerResponse(id=request.id, ok=True)
        if method == "sync":
            tracker.sync()
            return TrackerResponse(id=request.id, ok=True)
        if method == "run_raw":
            raw_args = args.get("raw_args", [])
            result = tracker.run_raw(*raw_args)
            return TrackerResponse(id=request.id, ok=True, result=result)
        return TrackerResponse(id=request.id, ok=False, error=f"Unknown method: {method}")
    except Exception as exc:
        log.warning("Tracker IPC method %s failed: %s", method, exc)
        return TrackerResponse(id=request.id, ok=False, error=str(exc))
