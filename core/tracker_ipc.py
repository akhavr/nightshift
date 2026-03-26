"""IPC protocol for single-writer tracker architecture.

Defines request/response encoding for Unix socket communication
between CLI processes and the watcher's tracker writer thread.

Protocol: JSON-lines over Unix domain stream socket.
Each request is a single JSON line; each response is a single JSON line.
Connections are per-request (short-lived).
"""

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from core.protocols import TrackerIssue, TrackerComment

log = logging.getLogger(__name__)


@dataclass
class TrackerRequest:
    """A request to execute a tracker method."""
    method: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrackerRequest":
        d = json.loads(data)
        return cls(method=d["method"], args=d.get("args", {}), id=d["id"])


@dataclass
class TrackerResponse:
    """A response from the tracker writer."""
    id: str
    ok: bool
    result: Any = None
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrackerResponse":
        d = json.loads(data)
        return cls(id=d["id"], ok=d["ok"], result=d.get("result"),
                   error=d.get("error", ""))


def serialize_tracker_issue(issue: TrackerIssue | None) -> dict | None:
    """Serialize a TrackerIssue to a JSON-safe dict."""
    if issue is None:
        return None
    return asdict(issue)


def deserialize_tracker_issue(data: dict | None) -> TrackerIssue | None:
    """Deserialize a dict back to a TrackerIssue."""
    if data is None:
        return None
    return TrackerIssue(**data)


def serialize_tracker_comment(comment: TrackerComment) -> dict:
    """Serialize a TrackerComment to a JSON-safe dict."""
    return asdict(comment)


def deserialize_tracker_comment(data: dict) -> TrackerComment:
    """Deserialize a dict back to a TrackerComment."""
    return TrackerComment(**data)


def execute_tracker_method(tracker: Any, request: TrackerRequest) -> TrackerResponse:
    """Execute a tracker method from a request and return the response.

    Routes request.method to the corresponding tracker method,
    serializing return values for JSON transport.
    """
    method = request.method
    args = request.args

    try:
        if method == "get_issue":
            result = tracker.get_issue(args["issue_id"])
            return TrackerResponse(id=request.id, ok=True,
                                   result=serialize_tracker_issue(result))

        elif method == "list_issues":
            result = tracker.list_issues(status=args.get("status"))
            return TrackerResponse(id=request.id, ok=True,
                                   result=[serialize_tracker_issue(i) for i in result])

        elif method == "get_comments":
            result = tracker.get_comments(args["issue_id"])
            return TrackerResponse(id=request.id, ok=True,
                                   result=[serialize_tracker_comment(c) for c in result])

        elif method == "add_comment":
            tracker.add_comment(args["issue_id"], args["body"])
            return TrackerResponse(id=request.id, ok=True)

        elif method == "set_status":
            tracker.set_status(args["issue_id"], args["status"])
            return TrackerResponse(id=request.id, ok=True)

        elif method == "add_label":
            tracker.add_label(args["issue_id"], args["label"])
            return TrackerResponse(id=request.id, ok=True)

        elif method == "remove_label":
            tracker.remove_label(args["issue_id"], args["label"])
            return TrackerResponse(id=request.id, ok=True)

        elif method == "sync":
            tracker.sync()
            return TrackerResponse(id=request.id, ok=True)

        elif method == "run_raw":
            raw_args = args.get("raw_args", [])
            result = tracker.run_raw(*raw_args)
            return TrackerResponse(id=request.id, ok=True, result=result)

        else:
            return TrackerResponse(id=request.id, ok=False,
                                   error=f"Unknown method: {method}")

    except Exception as e:
        log.warning("Tracker IPC method %s failed: %s", method, e)
        return TrackerResponse(id=request.id, ok=False, error=str(e))
