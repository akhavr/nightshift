from datetime import datetime, timezone

from core.agent_events import AgentEvent, AgentEventType


def test_event_creation():
    """Verify AgentEvent can be instantiated with type, timestamp, content, metadata, raw."""
    ts = datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    event = AgentEvent(
        type=AgentEventType.TEXT,
        content="hello",
        metadata={"source": "test"},
        raw='{"type":"text","content":"hello"}',
        timestamp=ts,
    )

    assert event.type is AgentEventType.TEXT
    assert event.timestamp == ts
    assert event.content == "hello"
    assert event.metadata == {"source": "test"}
    assert event.raw == '{"type":"text","content":"hello"}'


def test_event_types_complete():
    """Verify all event types exist (core + system events)."""
    expected = [
        # Core events from issue spec
        "STARTED",
        "TEXT",
        "TOOL_CALL",
        "TOOL_RESULT",
        "QUESTION",
        "CHECKPOINT",
        "DONE",
        "ERROR",
        "AUTH_FAILURE",
        # System events (existing in codebase)
        "SYSTEM",
        "PROCESS_EXIT",
        "STALL",
        "PROVIDER_OVERLOAD",
        "UNKNOWN",
    ]
    assert [member.name for member in AgentEventType] == expected


def test_event_serialization():
    """Verify AgentEvent.to_dict() returns serializable dict."""
    ts = datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    event = AgentEvent(
        type=AgentEventType.CHECKPOINT,
        content="step one",
        metadata={"step": 1},
        raw="@@CHECKPOINT@@ step one",
        timestamp=ts,
    )

    assert event.to_dict() == {
        "type": "CHECKPOINT",
        "timestamp": "2026-04-26T12:30:00+00:00",
        "content": "step one",
        "metadata": {"step": 1},
        "raw": "@@CHECKPOINT@@ step one",
    }


def test_event_from_dict():
    """Verify AgentEvent.from_dict() reconstructs event."""
    data = {
        "type": "DONE",
        "timestamp": "2026-04-26T12:30:00+00:00",
        "content": "finished",
        "metadata": {"status": "ok"},
        "raw": "@@DONE@@",
    }

    event = AgentEvent.from_dict(data)

    assert event.type is AgentEventType.DONE
    assert event.timestamp == datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    assert event.content == "finished"
    assert event.metadata == {"status": "ok"}
    assert event.raw == "@@DONE@@"


def test_event_from_dict_without_optional_fields():
    """Verify from_dict handles missing optional fields gracefully."""
    data = {"type": "TEXT"}

    event = AgentEvent.from_dict(data)

    assert event.type is AgentEventType.TEXT
    assert event.content == ""
    assert event.metadata == {}
    assert event.raw == ""
    assert event.timestamp is not None
