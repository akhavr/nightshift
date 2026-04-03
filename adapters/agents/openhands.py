"""OpenHands agent adapter.

Headless JSON mode: fire-and-forget with --json output.
Events separated by "--JSON Event--" lines. Multi-turn Q&A
uses --resume <conversation_id> to restart with context preserved.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

EVENT_SEPARATOR = "--JSON Event--"
CONVERSATION_ID_RE = re.compile(r"Conversation ID:\s*(\S+)")


class OpenHandsAgent(HeadlessAgentBase):
    def __init__(
        self,
        command: str = "openhands",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
    ):
        super().__init__(command, stall_timeout_s, extra_args)

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        cmd = [
            self.command, "--headless", "--json", "--always-approve",
            "--override-with-envs",
            *self.extra_args,
            "-t", prompt,
        ]
        if self._session_id:
            cmd += ["--resume", self._session_id]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _on_process_exit(self) -> None:
        """Extract conversation ID from stderr for resume support."""
        self._extract_session_id()

    def _extract_session_id(self) -> None:
        """Read stderr and extract conversation ID if present."""
        if not self._process or not self._process.stderr:
            return
        try:
            stderr_text = self._process.stderr.read()
            if stderr_text:
                match = CONVERSATION_ID_RE.search(stderr_text)
                if match:
                    self._session_id = match.group(1)
                    log.info(f"Session ID: {self._session_id}")
        except Exception as e:
            log.warning(f"Failed to read stderr for session ID: {e}")

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a JSON event line into an AgentEvent."""
        stripped = raw.strip()
        if not stripped or stripped == EVENT_SEPARATOR:
            return None

        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

        kind = ev.get("kind", "")

        if kind == "ActionEvent":
            return self._parse_action(ev, raw)

        if kind == "ObservationEvent":
            content = str(ev.get("content", ""))[:TOOL_RESULT_PREVIEW_LEN]
            return AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                content=content,
                raw=raw,
            )

        if kind == "MessageEvent":
            content = ev.get("content", "")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=str(content),
                raw=raw,
            )

        # reasoning_content on unknown event kinds
        reasoning = ev.get("reasoning_content", "")
        if reasoning:
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@LOG@@ {reasoning}",
                raw=raw,
            )

        # Unknown kind
        return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

    def _parse_action(self, ev: dict, raw: str) -> AgentEvent:
        """Parse an ActionEvent into the appropriate AgentEvent.

        Marker actions (FinishAction, FileEditorAction, TerminalAction) are
        checked before reasoning_content so they are never shadowed by @@LOG@@.
        """
        action_type = ev.get("action_type", "")

        if action_type == "FinishAction":
            return AgentEvent(
                type=AgentEventType.TEXT,
                content="@@DONE@@",
                raw=raw,
            )

        if action_type == "FileEditorAction":
            summary = ev.get("summary", "file edit")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@CHECKPOINT@@ {summary}",
                raw=raw,
            )

        if action_type == "TerminalAction":
            command = ev.get("command", "")
            return AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content=f"TerminalAction: {command}",
                raw=raw,
            )

        # reasoning_content on non-marker action types
        reasoning = ev.get("reasoning_content", "")
        if reasoning:
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@LOG@@ {reasoning}",
                raw=raw,
            )

        # Unknown action type
        return AgentEvent(
            type=AgentEventType.TEXT,
            content=f"{action_type}: {json.dumps(ev)[:200]}",
            raw=raw,
        )
