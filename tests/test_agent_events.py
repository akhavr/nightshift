from datetime import datetime, timezone

from core.agent_events import AgentEvent, AgentEventType


def test_event_creation():
    ts = datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    event = AgentEvent(
        type=AgentEventType.TEXT,
        timestamp=ts,
        content="hello",
        metadata={"source": "test"},
    )

    assert event.type is AgentEventType.TEXT
    assert event.timestamp == ts
    assert event.content == "hello"
    assert event.metadata == {"source": "test"}


def test_event_types_complete():
    assert [member.name for member in AgentEventType] == [
        "STARTED",
        "TEXT",
        "TOOL_CALL",
        "TOOL_RESULT",
        "QUESTION",
        "CHECKPOINT",
        "DONE",
        "ERROR",
        "AUTH_FAILURE",
    ]


def test_event_serialization():
    ts = datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    event = AgentEvent(
        type=AgentEventType.CHECKPOINT,
        timestamp=ts,
        content="step one",
        metadata={"step": 1},
    )

    assert event.to_dict() == {
        "type": "CHECKPOINT",
        "timestamp": "2026-04-26T12:30:00+00:00",
        "content": "step one",
        "metadata": {"step": 1},
    }


def test_event_from_dict():
    data = {
        "type": "DONE",
        "timestamp": "2026-04-26T12:30:00+00:00",
        "content": "finished",
        "metadata": {"status": "ok"},
    }

    event = AgentEvent.from_dict(data)

    assert event.type is AgentEventType.DONE
    assert event.timestamp == datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    assert event.content == "finished"
    assert event.metadata == {"status": "ok"}
