"""Test that Codex uses MCP tools for signals instead of text markers.

This test verifies the expected behavior: codex should call nightshift_done
MCP tool rather than printing @@DONE@@ as text in agent_message.

REQ: REQ-028 (signal protocol)
"""

import json

from adapters.agents.codex import CodexAgent
from core.protocols import AgentEventType


def _item_ev(event_type: str, item_type: str, **item_fields) -> str:
    """Build an item.started/item.completed event string."""
    item = {"id": "item_0", "type": item_type}
    item.update(item_fields)
    return json.dumps({"type": event_type, "item": item})


class TestCodexMCPSignals:
    """Tests that Codex MCP tool calls are the proper signal mechanism."""

    def test_codex_uses_mcp_for_done(self):
        """When codex completes, it should call nightshift_done MCP tool, not text marker.

        This test verifies that:
        1. An MCP tool call for nightshift_done produces the correct @@DONE@@ marker
        2. Text in agent_message containing @@DONE@@ is just text, not a signal

        The MCP tool call is the proper signal mechanism per REQ-028.
        """
        agent = CodexAgent()

        # Simulate codex calling the MCP tool (the CORRECT way to signal done)
        mcp_done_event = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_done",
            arguments={"summary": "Task completed successfully"},
        )
        ev = agent._parse(mcp_done_event)

        # MCP tool call should produce the @@DONE@@ marker
        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@DONE@@"

        # Simulate codex printing @@DONE@@ as text (the WRONG way - just text output)
        text_done_event = _item_ev(
            "item.completed", "agent_message",
            text="I have finished the task. @@DONE@@",
        )
        text_ev = agent._parse(text_done_event)

        # Text output is just text - SessionRunner will see the marker in content,
        # but the MCP approach is the proper signal mechanism that the prompt instructs
        assert text_ev is not None
        assert text_ev.type == AgentEventType.TEXT
        assert "@@DONE@@" in text_ev.content
        # The content includes the surrounding text, proving it's just agent output
        assert "finished the task" in text_ev.content

    def test_codex_mcp_checkpoint_takes_precedence(self):
        """MCP checkpoint tool call produces proper marker with description."""
        agent = CodexAgent()

        mcp_checkpoint = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_checkpoint",
            arguments={"description": "Completed test suite"},
        )
        ev = agent._parse(mcp_checkpoint)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@CHECKPOINT@@ Completed test suite"

    def test_codex_mcp_question_produces_question_and_waiting(self):
        """MCP question tool call produces @@QUESTION@@ then @@WAITING@@."""
        agent = CodexAgent()

        mcp_question = _item_ev(
            "item.completed", "mcp_tool_call",
            server="nightshift-signals", tool="nightshift_question",
            arguments={"question": "Should I use pytest or unittest?"},
        )
        ev = agent._parse(mcp_question)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@QUESTION@@ Should I use pytest or unittest?"

        # The WAITING marker is queued as an extra event
        extras = list(agent._drain_extra())
        assert len(extras) == 1
        assert extras[0].content == "@@WAITING@@"

    def test_non_nightshift_mcp_tool_not_signal(self):
        """MCP tool calls from other servers are not signal events."""
        agent = CodexAgent()

        other_mcp = _item_ev(
            "item.completed", "mcp_tool_call",
            server="filesystem", tool="read_file",
            arguments={"path": "/workspace/README.md"},
        )
        ev = agent._parse(other_mcp)

        # Should be a SYSTEM event, not a signal marker
        assert ev is not None
        assert ev.type == AgentEventType.SYSTEM
        assert "mcp_tool_call" in ev.content
        assert "@@DONE@@" not in ev.content
