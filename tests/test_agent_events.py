"""Tests for AgentEvent dataclass and AgentEventType enum."""

from datetime import datetime, timezone

import pytest

from core.agent_events import AgentEvent, AgentEventType


class TestEventCreation:
    def test_event_creation(self):
        """AgentEvent can be instantiated with type, timestamp, content, metadata."""
        ts = datetime.now(timezone.utc)
        event = AgentEvent(
            type=AgentEventType.TEXT,
            timestamp=ts,
            content="hello world",
            metadata={"key": "value"},
        )
        assert event.type == AgentEventType.TEXT
        assert event.timestamp == ts
        assert event.content == "hello world"
        assert event.metadata == {"key": "value"}

    def test_event_defaults(self):
        """AgentEvent has sensible defaults for optional fields."""
        event = AgentEvent(type=AgentEventType.STARTED)
        assert event.content == ""
        assert event.metadata == {}
        assert isinstance(event.timestamp, datetime)


class TestEventTypes:
    def test_event_types_complete(self):
        """AgentEventType enum has all required event types."""
        expected = {
            "STARTED",
            "TEXT",
            "TOOL_CALL",
            "TOOL_RESULT",
            "QUESTION",
            "CHECKPOINT",
            "DONE",
            "ERROR",
            "AUTH_FAILURE",
        }
        actual = {e.name for e in AgentEventType}
        assert expected <= actual, f"Missing event types: {expected - actual}"


class TestEventSerialization:
    def test_event_serialization(self):
        """AgentEvent.to_dict() returns serializable dict."""
        ts = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
        event = AgentEvent(
            type=AgentEventType.TEXT,
            timestamp=ts,
            content="test content",
            metadata={"nested": {"value": 123}},
        )
        d = event.to_dict()
        assert d["type"] == "TEXT"
        assert d["timestamp"] == "2026-04-28T12:00:00+00:00"
        assert d["content"] == "test content"
        assert d["metadata"] == {"nested": {"value": 123}}

    def test_event_from_dict(self):
        """AgentEvent.from_dict() reconstructs event."""
        data = {
            "type": "CHECKPOINT",
            "timestamp": "2026-04-28T12:00:00+00:00",
            "content": "checkpoint reached",
            "metadata": {"step": 5},
        }
        event = AgentEvent.from_dict(data)
        assert event.type == AgentEventType.CHECKPOINT
        assert event.timestamp == datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
        assert event.content == "checkpoint reached"
        assert event.metadata == {"step": 5}

    def test_roundtrip(self):
        """to_dict/from_dict roundtrip preserves data."""
        ts = datetime.now(timezone.utc)
        original = AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            timestamp=ts,
            content="result data",
            metadata={"status": "ok", "count": 42},
        )
        reconstructed = AgentEvent.from_dict(original.to_dict())
        assert reconstructed.type == original.type
        assert reconstructed.timestamp == original.timestamp
        assert reconstructed.content == original.content
        assert reconstructed.metadata == original.metadata
