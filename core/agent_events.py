"""Agent event types and serialization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    STARTED = "STARTED"
    TEXT = "TEXT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    QUESTION = "QUESTION"
    CHECKPOINT = "CHECKPOINT"
    DONE = "DONE"
    ERROR = "ERROR"
    AUTH_FAILURE = "AUTH_FAILURE"


def _coerce_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass
class AgentEvent:
    type: AgentEventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "content": self.content,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        return cls(
            type=AgentEventType(data["type"]),
            timestamp=_coerce_timestamp(data["timestamp"]),
            content=data.get("content", ""),
            metadata=dict(data.get("metadata", {})),
        )


__all__ = ["AgentEvent", "AgentEventType"]
