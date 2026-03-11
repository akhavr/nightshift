"""Claude Code adapter.

OQ-1 RESOLVED: -p mode is fire-and-forget (exits after responding).
stdin follow-up does NOT work. Multi-turn Q&A uses --resume <session_id>
to restart with full conversation context preserved.
"""

import json
import logging
import select
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import CodingAgent, AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0


class ClaudeCodeAgent:
    def __init__(
        self,
        command: str = "claude",
        stall_timeout_s: float = STALL_TIMEOUT_S,
        extra_args: list[str] | None = None,
    ):
        self.command = command
        self.stall_timeout_s = stall_timeout_s
        self.extra_args = extra_args or []
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._last_event: float = 0
        self._session_id: str | None = None
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

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        self._extra_events.clear()
        stdout = self._process.stdout
        while True:
            # Drain any extra events from multi-part assistant messages
            while self._extra_events:
                yield self._extra_events.pop(0)

            if self._process.poll() is not None:
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev: yield ev
                    while self._extra_events:
                        yield self._extra_events.pop(0)
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT); return
                self._last_event = time.monotonic()
                ev = self._parse(line.rstrip("\n"))
                if ev: yield ev
            else:
                elapsed = time.monotonic() - self._last_event
                if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                    yield AgentEvent(type=AgentEventType.STALL,
                                     content=f"No output for {elapsed:.0f}s")
                    return

    def send_input(self, text: str) -> None:
        # OQ-1: -p mode does not read stdin. Callers should terminate()
        # then start() again — --resume preserves conversation context.
        raise RuntimeError(
            "send_input() not supported in -p mode. "
            "Use terminate() + start() with the answer as prompt."
        )

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try: self._process.wait(timeout=10)
            except Exception: self._process.kill(); self._process.wait()
        self._process = None; self._pid = None
        # Note: _session_id is preserved so next start() can --resume

    @property
    def pid(self) -> int | None:
        return self._pid

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
                    content=str(result_content)[:500], raw=raw)
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
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=ev.get("result", ""), raw=raw)

        if t == "rate_limit_event":
            return None

        if t == "system":
            return AgentEvent(type=AgentEventType.SYSTEM,
                              content=ev.get("message", ""), raw=raw)

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
                inp = str(part.get("input", ""))[:300]
                yield AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    content=f"{name}: {inp}", raw=raw)
            elif pt == "thinking":
                pass  # internal reasoning, not surfaced
