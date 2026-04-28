"""Unified agent event types and serialization helpers.

Foundation for the unified event stream across all agent adapters (REQ-030).
This module is the single source of truth for AgentEventType and AgentEvent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    """Event types emitted by coding agents.

    Core events (from issue spec):
    - STARTED, TEXT, TOOL_CALL, TOOL_RESULT, QUESTION, CHECKPOINT, DONE, ERROR, AUTH_FAILURE

    System events (for agent lifecycle):
    - SYSTEM, PROCESS_EXIT, STALL, PROVIDER_OVERLOAD, UNKNOWN
    """
    # Core events from issue spec
    STARTED = "STARTED"
    TEXT = "TEXT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    QUESTION = "QUESTION"
    CHECKPOINT = "CHECKPOINT"
    DONE = "DONE"
    ERROR = "ERROR"
    AUTH_FAILURE = "AUTH_FAILURE"
    # System events (existing in protocols.py)
    SYSTEM = "SYSTEM"
    PROCESS_EXIT = "PROCESS_EXIT"
    STALL = "STALL"
    PROVIDER_OVERLOAD = "PROVIDER_OVERLOAD"
    UNKNOWN = "UNKNOWN"


def _coerce_timestamp(value: datetime | str) -> datetime:
    """Convert string timestamp to datetime, handling Z suffix."""
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass
class AgentEvent:
    """A single event from an agent's execution stream."""
    type: AgentEventType
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "content": self.content,
            "metadata": dict(self.metadata),
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        """Reconstruct an event from a dict."""
        return cls(
            type=AgentEventType(data["type"]),
            timestamp=_coerce_timestamp(data.get("timestamp", datetime.now(timezone.utc).isoformat())),
            content=data.get("content", ""),
            metadata=dict(data.get("metadata", {})),
            raw=data.get("raw", ""),
        )


__all__ = ["AgentEvent", "AgentEventType"]
