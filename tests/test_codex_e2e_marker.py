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

        This test verifies that an MCP tool call for nightshift_done produces
        the correct @@DONE@@ marker. MCP is the preferred signal mechanism per REQ-028.
        """
        agent = CodexAgent()

        # Simulate codex calling the MCP tool (the preferred way to signal done)
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

    def test_codex_text_marker_fallback_done(self):
        """Text markers in agent_message are detected as fallback (REQ-028).

        When the agent prints @@DONE@@ as text instead of using MCP tools,
        the adapter extracts and normalizes it to ensure consistent behavior.
        """
        agent = CodexAgent()

        # Simulate codex printing @@DONE@@ as text (fallback mechanism)
        text_done_event = _item_ev(
            "item.completed", "agent_message",
            text="I have finished the task. @@DONE@@",
        )
        text_ev = agent._parse(text_done_event)

        # Fallback detection extracts the marker
        assert text_ev is not None
        assert text_ev.type == AgentEventType.TEXT
        assert text_ev.content == "@@DONE@@"

    def test_codex_text_marker_fallback_checkpoint(self):
        """Text checkpoint markers in agent_message are detected as fallback."""
        agent = CodexAgent()

        text_checkpoint = _item_ev(
            "item.completed", "agent_message",
            text="Progress update: @@CHECKPOINT@@ Finished writing tests",
        )
        ev = agent._parse(text_checkpoint)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@CHECKPOINT@@ Finished writing tests"

    def test_codex_text_marker_fallback_question(self):
        """Text question markers in agent_message are detected as fallback."""
        agent = CodexAgent()

        text_question = _item_ev(
            "item.completed", "agent_message",
            text="I need help: @@QUESTION@@ Which database should I use?",
        )
        ev = agent._parse(text_question)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "@@QUESTION@@ Which database should I use?"

        # Should also queue @@WAITING@@
        extras = list(agent._drain_extra())
        assert len(extras) == 1
        assert extras[0].content == "@@WAITING@@"

    def test_codex_plain_text_no_markers(self):
        """Regular agent_message text without markers is returned as-is."""
        agent = CodexAgent()

        plain_text = _item_ev(
            "item.completed", "agent_message",
            text="Here is my analysis of the code...",
        )
        ev = agent._parse(plain_text)

        assert ev is not None
        assert ev.type == AgentEventType.TEXT
        assert ev.content == "Here is my analysis of the code..."

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
