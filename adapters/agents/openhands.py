"""OpenHands agent adapter.

Headless JSON mode: fire-and-forget with --json output.
Events separated by "--JSON Event--" lines. Multi-turn Q&A
uses --resume <conversation_id> to restart with context preserved.
"""

import json
import logging
import os
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Iterator, Optional

from core.protocols import AgentEvent, AgentEventType

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 10.0
STALL_TIMEOUT_S = 300.0
PROCESS_TERMINATE_TIMEOUT_S = 10
TOOL_RESULT_PREVIEW_LEN = 500
EVENT_SEPARATOR = "--JSON Event--"
CONVERSATION_ID_RE = re.compile(r"Conversation ID:\s*(\S+)")

# Env vars forwarded to the subprocess
LLM_ENV_VARS = ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")


class OpenHandsAgent:
    def __init__(
        self,
        command: str = "openhands",
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

    def start(self, prompt: str, workspace: Path, max_turns: int = 50) -> None:
        cmd = [
            self.command, "--headless", "--json", "--always-approve",
            "--override-with-envs",
            *self.extra_args,
            "-t", prompt,
        ]
        if self._session_id:
            cmd += ["--resume", self._session_id]

        env = os.environ.copy()
        for var in LLM_ENV_VARS:
            val = os.environ.get(var)
            if val is not None:
                env[var] = val
            elif var in env:
                del env[var]

        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=str(workspace), bufsize=1, env=env,
        )
        self._pid = self._process.pid
        self._last_event = time.monotonic()

    def stream_events(self) -> Iterator[AgentEvent]:
        if not self._process:
            return
        stdout = self._process.stdout
        while True:
            if self._process.poll() is not None:
                # Drain remaining stdout
                for line in stdout:
                    ev = self._parse(line.rstrip("\n"))
                    if ev:
                        yield ev
                # Extract session ID from stderr
                self._extract_session_id()
                yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                return

            ready, _, _ = select.select([stdout], [], [], READ_TIMEOUT_S)
            if ready:
                line = stdout.readline()
                if not line:
                    self._extract_session_id()
                    yield AgentEvent(type=AgentEventType.PROCESS_EXIT)
                    return
                self._last_event = time.monotonic()
                ev = self._parse(line.rstrip("\n"))
                if ev:
                    yield ev
            else:
                elapsed = time.monotonic() - self._last_event
                if self.stall_timeout_s > 0 and elapsed > self.stall_timeout_s:
                    yield AgentEvent(
                        type=AgentEventType.STALL,
                        content=f"No output for {elapsed:.0f}s",
                    )
                    return

    def send_input(self, text: str) -> None:
        raise RuntimeError(
            "send_input() not supported in headless mode. "
            "Use terminate() + start() with the answer as prompt."
        )

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_S)
            except Exception as e:
                log.warning(f"Process did not terminate cleanly: {e}")
                self._process.kill()
                self._process.wait()
        self._process = None
        self._pid = None
        # _session_id preserved for resume

    @property
    def pid(self) -> int | None:
        return self._pid

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

        # Check for reasoning_content first (can appear on any event)
        reasoning = ev.get("reasoning_content", "")
        if reasoning:
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@LOG@@ {reasoning}",
                raw=raw,
            )

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

        # Unknown kind
        return AgentEvent(type=AgentEventType.TEXT, content=raw, raw=raw)

    def _parse_action(self, ev: dict, raw: str) -> AgentEvent:
        """Parse an ActionEvent into the appropriate AgentEvent."""
        action_type = ev.get("action_type", "")

        if action_type == "FileEditorAction":
            summary = ev.get("summary", "file edit")
            return AgentEvent(
                type=AgentEventType.TEXT,
                content=f"@@CHECKPOINT@@ {summary}",
                raw=raw,
            )

        if action_type == "FinishAction":
            return AgentEvent(
                type=AgentEventType.TEXT,
                content="@@DONE@@",
                raw=raw,
            )

        if action_type == "TerminalAction":
            command = ev.get("command", "")
            return AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content=f"TerminalAction: {command}",
                raw=raw,
            )

        # Unknown action type
        return AgentEvent(
            type=AgentEventType.TEXT,
            content=f"{action_type}: {json.dumps(ev)[:200]}",
            raw=raw,
        )
