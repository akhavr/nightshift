"""Claude Code adapter.

OQ-1 RESOLVED: -p mode is fire-and-forget (exits after responding).
stdin follow-up does NOT work. Multi-turn Q&A uses --resume <session_id>
to restart with full conversation context preserved.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from adapters.agents.base import HeadlessAgentBase, TOOL_RESULT_PREVIEW_LEN
from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

TOOL_INPUT_PREVIEW_LEN = 300


class ClaudeCodeAgent(HeadlessAgentBase):
    AUTH_FAILURE_PATTERNS = (
        "invalid_api_key",
        "authentication_error",
        "authorization_error",
        "invalid x-api-key",
        "permission_error",
        "api key is invalid",
        "token has expired",
        "expired token",
        "unauthorized",
        "could not authenticate",
    )

    def __init__(
        self,
        command: str = "claude",
        stall_timeout_s: float = 300.0,
        extra_args: list[str] | None = None,
    ):
        super().__init__(command, stall_timeout_s, extra_args)
        self._extra_events: list[AgentEvent] = []

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        cmd = [
            self.command, "--dangerously-skip-permissions",
            "--verbose", "--output-format", "stream-json",
            "--max-turns", str(max_turns),
        ]
        # Resume previous session to preserve conversation context (OQ-1)
        if self._session_id:
            cmd += ["--resume", self._session_id]
        cmd += [*self.extra_args, "-p", prompt]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=str(workspace), bufsize=1,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def _before_stream(self) -> None:
        self._extra_events.clear()

    def _drain_extra(self) -> Iterator[AgentEvent]:
        while self._extra_events:
            yield self._extra_events.pop(0)

    def _parse_user_event(self, ev: dict, raw: str) -> Optional[AgentEvent]:
        """Parse a 'user' event containing tool results."""
        msg = ev.get("message", {})
        content_parts = msg.get("content", []) if isinstance(msg, dict) else []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                result_content = part.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        p.get("text", "") for p in result_content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                return AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    content=str(result_content)[:TOOL_RESULT_PREVIEW_LEN], raw=raw)
        return None

    def _parse(self, raw: str) -> Optional[AgentEvent]:
        """Parse a stream-json line into an AgentEvent."""
        if not raw.strip(): return None
        try: ev = json.loads(raw)
        except json.JSONDecodeError: return None
        t = ev.get("type", "")

        if t == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                self._session_id = sid
                log.info(f"Session ID: {sid}")
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content="init", raw=raw)

        if t == "assistant":
            msg = ev.get("message", {})
            content_parts = msg.get("content", []) if isinstance(msg, dict) else []
            events = list(self._parse_assistant_content(content_parts, raw))
            if events:
                self._extra_events.extend(events[1:])
                return events[0]
            return None

        if t == "user":
            return self._parse_user_event(ev, raw)

        if t == "result":
            result_text = ev.get("result", "")
            if self._is_auth_failure(result_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=result_text, raw=raw)
            if ev.get("subtype") == "success" and not ev.get("is_error"):
                self._extra_events.append(AgentEvent(
                    type=AgentEventType.TEXT, content="@@DONE@@", raw=raw))
            metadata = {}
            if "cost_usd" in ev or "input_tokens" in ev or "output_tokens" in ev:
                metadata["usage"] = {
                    "input_tokens": ev.get("input_tokens", 0),
                    "output_tokens": ev.get("output_tokens", 0),
                    "cost_usd": ev.get("cost_usd", 0.0),
                    "model": ev.get("model", ""),
                }
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=result_text, metadata=metadata, raw=raw)

        if t == "error":
            error_msg = ev.get("error", {})
            error_text = error_msg.get("message", "") if isinstance(error_msg, dict) else str(error_msg)
            if self._is_auth_failure(error_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=error_text, raw=raw)
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=f"error: {error_text}", raw=raw)

        if t == "rate_limit_event":
            return None

        if t == "system":
            msg_text = ev.get("message", "")
            if self._is_auth_failure(msg_text):
                return AgentEvent(type=AgentEventType.AUTH_FAILURE,
                                  content=msg_text, raw=raw)
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=msg_text, raw=raw)

        return AgentEvent(type=AgentEventType.UNKNOWN, raw=raw)

    def _parse_assistant_content(
        self, content_parts: list, raw: str,
    ) -> Iterator[AgentEvent]:
        """Parse the content array from an assistant message.

        A single assistant event can contain multiple content items:
        text, tool_use, and thinking blocks. We yield separate AgentEvents
        for each so the session runner can handle them independently.
        """
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            pt = part.get("type", "")
            if pt == "text":
                text = part.get("text", "")
                if text:
                    yield AgentEvent(
                        type=AgentEventType.TEXT, content=text, raw=raw)
            elif pt == "tool_use":
                name = part.get("name", "?")
                inp = str(part.get("input", ""))[:TOOL_INPUT_PREVIEW_LEN]
                yield AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    content=f"{name}: {inp}", raw=raw)
            elif pt == "thinking":
                pass  # internal reasoning, not surfaced
