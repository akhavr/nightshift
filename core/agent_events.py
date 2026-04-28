"""Unified event types for agent event streams.

Foundation for the unified event stream across all agent adapters (REQ-030).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any


class AgentEventType(Enum):
    """Event types emitted by coding agents."""

    STARTED = auto()
    TEXT = auto()
    TOOL_CALL = auto()
    TOOL_RESULT = auto()
    QUESTION = auto()
    CHECKPOINT = auto()
    DONE = auto()
    ERROR = auto()
    AUTH_FAILURE = auto()


@dataclass
class AgentEvent:
    """A single event from an agent's execution stream."""

    type: AgentEventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "type": self.type.name,
            "timestamp": self.timestamp.isoformat(),
            "content": self.content,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentEvent:
        """Reconstruct an event from a dict."""
        return cls(
            type=AgentEventType[data["type"]],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
        )
