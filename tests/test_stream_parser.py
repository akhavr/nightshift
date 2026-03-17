"""Test ClaudeCodeAgent._parse() against real stream-json output.

Uses fixtures captured from Claude Code 2.1.70 (OQ-2).
"""
import json
from pathlib import Path

from adapters.agents.claude_code import ClaudeCodeAgent
from core.protocols import AgentEventType


FIXTURES = Path(__file__).parent / "fixtures_stream_json.jsonl"


def make_agent():
    agent = ClaudeCodeAgent()
    return agent


def load_fixture_lines():
    return FIXTURES.read_text().strip().splitlines()


def test_parse_init_event():
    agent = make_agent()
    line = '{"type":"system","subtype":"init","session_id":"abc-123","tools":[],"model":"claude-opus-4-6"}'
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.SYSTEM
    assert ev.content == "init"
    assert agent._session_id == "abc-123"


def test_parse_assistant_text():
    agent = make_agent()
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Hello world"}],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TEXT
    assert ev.content == "Hello world"


def test_parse_assistant_tool_use():
    agent = make_agent()
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "ls"}, "id": "toolu_123"},
            ],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TOOL_CALL
    assert "Bash" in ev.content


def test_parse_assistant_mixed_content():
    """Assistant message with both tool_use and text yields multiple events."""
    agent = make_agent()
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}, "id": "t1"},
                {"type": "text", "text": "Searching..."},
            ],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TOOL_CALL
    # Second event buffered in _extra_events
    assert len(agent._extra_events) == 1
    assert agent._extra_events[0].type == AgentEventType.TEXT
    assert agent._extra_events[0].content == "Searching..."


def test_parse_assistant_thinking_ignored():
    agent = make_agent()
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "Let me think about this..."},
                {"type": "text", "text": "Here's my answer"},
            ],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TEXT
    assert ev.content == "Here's my answer"
    assert len(agent._extra_events) == 0  # thinking was skipped


def test_parse_user_tool_result():
    agent = make_agent()
    line = json.dumps({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_123",
                "content": "file1.py\nfile2.py",
            }],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TOOL_RESULT
    assert "file1.py" in ev.content


def test_parse_user_tool_result_list_content():
    """Tool result with content as list of text objects."""
    agent = make_agent()
    line = json.dumps({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_456",
                "content": [
                    {"type": "text", "text": "result line 1"},
                    {"type": "text", "text": "result line 2"},
                ],
            }],
        },
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.TOOL_RESULT
    assert "result line 1" in ev.content


def test_parse_result_success():
    agent = make_agent()
    line = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "Task completed successfully",
        "session_id": "abc-123",
    })
    ev = agent._parse(line)
    assert ev is not None
    assert ev.type == AgentEventType.SYSTEM
    assert "Task completed" in ev.content


def test_parse_rate_limit_ignored():
    agent = make_agent()
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed"},
    })
    ev = agent._parse(line)
    assert ev is None


def test_parse_real_fixture():
    """Parse the full captured stream-json fixture end-to-end."""
    agent = make_agent()
    lines = load_fixture_lines()
    events = []
    for line in lines:
        ev = agent._parse(line)
        if ev:
            events.append(ev)
        while agent._extra_events:
            events.append(agent._extra_events.pop(0))

    # Should have extracted session_id
    assert agent._session_id is not None

    types = [e.type for e in events]
    # Should contain at least: SYSTEM (init), TOOL_CALL, TOOL_RESULT, TEXT, SYSTEM (result)
    assert AgentEventType.SYSTEM in types
    assert AgentEventType.TOOL_CALL in types
    assert AgentEventType.TOOL_RESULT in types
    assert AgentEventType.TEXT in types

    # Text event should contain the actual response
    text_events = [e for e in events if e.type == AgentEventType.TEXT]
    assert any("adapters" in e.content.lower() for e in text_events)


def test_parse_empty_and_invalid():
    agent = make_agent()
    assert agent._parse("") is None
    assert agent._parse("   ") is None
    assert agent._parse("not json") is None
    assert agent._parse("{}") is not None  # unknown type


# ── Auth failure detection ─────────────────────────────────

class TestAuthFailureDetection:
    def test_error_event_with_invalid_api_key(self):
        agent = make_agent()
        line = json.dumps({
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE
        assert "invalid x-api-key" in ev.content

    def test_error_event_with_expired_token(self):
        agent = make_agent()
        line = json.dumps({
            "type": "error",
            "error": {"type": "authentication_error", "message": "Token has expired"},
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_error_event_string_format(self):
        """Error field as a plain string instead of dict."""
        agent = make_agent()
        line = json.dumps({
            "type": "error",
            "error": "authentication_error: invalid_api_key",
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_result_event_with_auth_error(self):
        agent = make_agent()
        line = json.dumps({
            "type": "result",
            "subtype": "error",
            "result": "Authentication error: invalid_api_key",
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_system_event_with_auth_error(self):
        agent = make_agent()
        line = json.dumps({
            "type": "system",
            "message": "Could not authenticate with the API",
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_non_auth_error_stays_system(self):
        """A regular error event should NOT be flagged as auth failure."""
        agent = make_agent()
        line = json.dumps({
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "Rate limit exceeded"},
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.SYSTEM
        assert "error:" in ev.content

    def test_non_auth_result_stays_system(self):
        agent = make_agent()
        line = json.dumps({
            "type": "result",
            "result": "Task completed successfully",
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.SYSTEM

    def test_permission_error_detected(self):
        agent = make_agent()
        line = json.dumps({
            "type": "error",
            "error": {"type": "permission_error", "message": "permission_error: access denied"},
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_unauthorized_detected(self):
        agent = make_agent()
        line = json.dumps({
            "type": "system",
            "message": "Unauthorized: please check your API credentials",
        })
        ev = agent._parse(line)
        assert ev is not None
        assert ev.type == AgentEventType.AUTH_FAILURE

    def test_is_auth_failure_static_method(self):
        from adapters.agents.claude_code import ClaudeCodeAgent
        assert ClaudeCodeAgent._is_auth_failure("invalid_api_key") is True
        assert ClaudeCodeAgent._is_auth_failure("AUTHENTICATION_ERROR") is True
        assert ClaudeCodeAgent._is_auth_failure("rate limit exceeded") is False
        assert ClaudeCodeAgent._is_auth_failure("") is False
